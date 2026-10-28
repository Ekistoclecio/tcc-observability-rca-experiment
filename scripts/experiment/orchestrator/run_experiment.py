#!/usr/bin/env python3
"""Host-side experiment lifecycle manager; it never instruments the SUT."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from scripts.experiment.faults.application import ApplicationFaultInjector
from scripts.experiment.faults.chaos_mesh import ChaosMeshInjector
from scripts.experiment.faults.kubernetes import KubernetesScaleInjector
from scripts.experiment.orchestrator.config import ConfigurationError, ExperimentConfig
from scripts.experiment.orchestrator.events import EventLog
from scripts.experiment.orchestrator.ground_truth import add_reason, initial_ground_truth, utc_now, write_json_atomic
from scripts.experiment.orchestrator.kube import deployment_status, run_kubectl
from scripts.experiment.orchestrator.preflight import preflight


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", help="single-run ID; for repetitions this is a prefix")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--artifact-root", type=Path, default=REPO_ROOT / "artifacts/experiments")
    parser.add_argument("--confirmation-timeout", type=float, default=90)
    parser.add_argument("--allow-unsupported-chaos-mesh", action="store_true")
    parser.add_argument("--validate-observability", action="store_true", help="run Phase 2 validator after workload recovery")
    return parser.parse_args(argv)


def make_injector(config):
    if config.fault_class in ("F1", "F2"):
        return ChaosMeshInjector(config.fault_class, config.run_id, config.fault["parameters"])
    if config.fault_class == "F3":
        return KubernetesScaleInjector("paymentservice")
    if config.fault_class == "F4":
        return ApplicationFaultInjector()
    if config.fault_class == "F5":
        return KubernetesScaleInjector("redis-cart")
    raise ConfigurationError(f"no injector for {config.fault_class}")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def event_wait(log: EventLog, process: subprocess.Popen, name: str, timeout: float = 180) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for event in log.read_workload_events():
            if event.get("event") == name:
                return event
        if process.poll() is not None:
            raise RuntimeError(f"workload exited ({process.returncode}) before event {name}")
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for workload event {name}")


def wait_monotonic(target: float, process: subprocess.Popen):
    while time.monotonic() < target:
        if process.poll() is not None:
            raise RuntimeError(f"workload exited ({process.returncode}) before scheduled fault action")
        time.sleep(min(0.1, max(0, target - time.monotonic())))


def validate_workload(run_dir: Path, config, events: EventLog, confirmed_start_mono=None, confirmed_end_mono=None, runner_clock_offset=0.0) -> tuple[bool, dict, list[dict]]:
    metadata_path = run_dir / f"{config.run_id}.json"
    if not metadata_path.exists():
        return False, {}, [{"code": "workload_metadata_missing", "message": "workload runner did not write its metadata"}]
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    reasons = []
    drops = int(metadata.get("control_plane_dropped_flows", 0))
    if drops:
        reasons.append({"code": "control_plane_dropped_flows", "message": f"runner dropped {drops} scheduled arrivals"})
    if int(metadata.get("started_flows", 0)) == 0:
        reasons.append({"code": "no_flows_started", "message": "no steady workload flow started"})
    if metadata.get("workload_model") != "open-loop":
        reasons.append({"code": "wrong_workload_model", "message": "metadata does not identify open-loop workload"})
    if metadata.get("result") == "failed" or metadata.get("fatal_error"):
        reasons.append({"code": "workload_runner_failed", "message": metadata.get("fatal_error", "runner reported failed")})
    workload_events = events.read_workload_events()
    if config.fault_class == "F4":
        gt_start = confirmed_start_mono
        gt_end = confirmed_end_mono
        failed_during = [e for e in workload_events if e.get("event") == "flow_failed" and e.get("phase") == "steady" and e.get("failure", {}).get("stage") == "checkout" and gt_start is not None and gt_start <= float(e.get("monotonic_seconds", -1)) + runner_clock_offset <= (gt_end or float("inf"))]
        completed_after = [e for e in workload_events if e.get("event") == "flow_completed" and e.get("phase") == "steady" and gt_end is not None and float(e.get("monotonic_seconds", -1)) + runner_clock_offset >= gt_end]
        if not failed_during:
            reasons.append({"code": "f4_effect_not_observed", "message": "no checkout flow failed while the Charge hook was confirmed active"})
        if not completed_after:
            reasons.append({"code": "f4_recovery_not_observed", "message": "no successful complete flow was observed after Charge recovery"})
        metadata["f4_fault_effect_observed"] = bool(failed_during)
        metadata["f4_post_recovery_success_observed"] = bool(completed_after)
    return not reasons, metadata, reasons


def classify_run_status(*, fault_class: str, injection_confirmed: bool, recovery_confirmed: bool,
                        workload_valid: bool, control_plane_valid: bool, workload_reasons: list[dict]) -> str:
    reason_codes = {item["code"] for item in workload_reasons}
    if not injection_confirmed:
        return "invalid_injection"
    if not recovery_confirmed:
        return "invalid_recovery"
    if not control_plane_valid:
        return "invalid_control_plane"
    if fault_class == "F4" and "f4_effect_not_observed" in reason_codes:
        return "invalid_injection"
    if fault_class == "F4" and "f4_recovery_not_observed" in reason_codes:
        return "invalid_recovery"
    return "valid" if workload_valid else "invalid_workload"


def classify_exception(operation: str) -> str:
    return {
        "preflight": "invalid_precondition",
        "workload_start": "invalid_workload",
        "schedule_fault": "invalid_workload",
        "inject_fault": "invalid_injection",
        "fault_window": "invalid_workload",
        "recover_fault": "invalid_recovery",
        "validate_workload": "invalid_workload",
    }.get(operation, "invalid_control_plane")


def run_one(config, args) -> int:
    run_dir = (args.artifact_root / config.run_id).resolve()
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(f"run artifact directory already exists and is not empty: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    event_path = run_dir / "orchestrator-events.jsonl"
    events = EventLog(event_path, config.run_id)
    ground_truth = initial_ground_truth(config)
    gt_path = run_dir / "ground-truth.json"
    write_json_atomic(gt_path, ground_truth)
    injector = make_injector(config)
    prepared = False
    workload: subprocess.Popen | None = None
    confirmed_start_mono = None
    confirmed_end_mono = None
    runner_clock_offset = 0.0
    workload_log = (run_dir / "workload-runner.log").open("w", encoding="utf-8")
    status = "aborted"
    operation = "preflight"
    events.emit("preflight_started", fault_class=config.fault_class)
    try:
        compatibility = preflight(config.fault_class, args.allow_unsupported_chaos_mesh)
        ground_truth["validation"]["compatibility"] = compatibility
        events.emit("preflight_passed", evidence=compatibility)
        ground_truth["validation"]["preflight"] = {"passed": True}
        prep_evidence = injector.prepare()
        prepared = True
        operation = "workload_start"
        events.emit("injector_prepared", evidence=prep_evidence)
        ground_truth["validation"]["preparation"] = prep_evidence

        env = os.environ.copy()
        env.update(config.workload_env)
        env.update(config.workload_overrides)
        env.update({
            "TCC_RUN_ID": config.run_id,
            "TCC_ARTIFACT_DIR": str(run_dir),
            "TCC_EVENT_FILE": str(event_path),
            "TCC_EXECUTOR": "host-wsl",
        })
        for obsolete in ("TCC_USERS", "TCC_SPAWN_RATE", "TCC_WAIT_SECONDS"):
            env.pop(obsolete, None)
        workload = subprocess.Popen(
            [str(REPO_ROOT / "scripts/experiment/run-workload.sh")], cwd=REPO_ROOT,
            env=env, stdout=workload_log, stderr=subprocess.STDOUT, text=True,
        )
        events.emit("workload_started", pid=workload.pid)
        steady = event_wait(events, workload, "steady_started", timeout=300)
        operation = "schedule_fault"
        ground_truth["timing"]["steady_started_at"] = steady["timestamp"]
        # Use this process's monotonic clock. The workload runner applies
        # gevent monkey-patching, so its clock value is not a safe scheduler
        # deadline for the independent host orchestrator.
        steady_observed_mono = time.monotonic()
        runner_clock_offset = steady_observed_mono - float(steady.get("monotonic_seconds", steady_observed_mono))
        planned_start_utc = parse_time(steady["timestamp"]) + timedelta(seconds=config.schedule["start_after_steady_seconds"])
        remaining_to_planned_start = max(0.0, (planned_start_utc - datetime.now(timezone.utc)).total_seconds())
        scheduled_start_mono = steady_observed_mono + remaining_to_planned_start
        ground_truth["timing"]["scheduled_start"] = (parse_time(steady["timestamp"]) + timedelta(seconds=config.schedule["start_after_steady_seconds"])).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        ground_truth["timing"]["scheduled_end"] = (parse_time(ground_truth["timing"]["scheduled_start"]) + timedelta(seconds=config.schedule["duration_seconds"])).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        ground_truth["timing"]["steady_started_observed_monotonic"] = steady_observed_mono
        ground_truth["timing"]["scheduled_start_monotonic"] = scheduled_start_mono
        ground_truth["timing"]["scheduled_end_monotonic"] = scheduled_start_mono + config.schedule["duration_seconds"]
        ground_truth["validation"]["runner_monotonic_offset_seconds"] = runner_clock_offset
        events.emit("fault_scheduled", scheduled_start=ground_truth["timing"]["scheduled_start"], start_after_steady_seconds=config.schedule["start_after_steady_seconds"])
        wait_monotonic(scheduled_start_mono, workload)
        ground_truth["timing"]["requested_at"] = utc_now()
        ground_truth["timing"]["requested_at_monotonic"] = time.monotonic()
        ground_truth["validation"]["scheduling"] = {
            "event_detected_monotonic": steady_observed_mono,
            "scheduled_monotonic": scheduled_start_mono,
            "request_lag_ms": round(max(0.0, time.monotonic() - scheduled_start_mono) * 1000, 3),
        }
        events.emit("fault_requested", scheduled_start=ground_truth["timing"]["scheduled_start"])
        operation = "inject_fault"
        ground_truth["validation"]["injection_requested"] = True
        ground_truth["validation"]["mechanism_confirmation"]["apply_request"] = injector.apply()
        active_evidence = injector.confirm_active(args.confirmation_timeout)
        ground_truth["timing"]["confirmed_start"] = utc_now()
        ground_truth["timing"]["confirmed_start_monotonic"] = time.monotonic()
        ground_truth["validation"]["injection_confirmed"] = True
        ground_truth["validation"]["mechanism_confirmation"]["active"] = active_evidence
        confirmed_start_mono = time.monotonic()
        events.emit("fault_confirmed", evidence=active_evidence)

        operation = "fault_window"
        recovery_mono = confirmed_start_mono + config.schedule["duration_seconds"]
        wait_monotonic(recovery_mono, workload)
        operation = "recover_fault"
        ground_truth["timing"]["recovery_requested_at"] = utc_now()
        ground_truth["timing"]["recovery_requested_at_monotonic"] = time.monotonic()
        events.emit("recovery_requested", scheduled_end=ground_truth["timing"]["scheduled_end"])
        ground_truth["validation"]["recovery_confirmation"]["request"] = injector.recover()
        recovered_evidence = injector.confirm_recovered(args.confirmation_timeout)
        ground_truth["timing"]["confirmed_end"] = utc_now()
        ground_truth["timing"]["confirmed_end_monotonic"] = time.monotonic()
        ground_truth["validation"]["recovery_confirmed"] = True
        ground_truth["validation"]["recovery_confirmation"]["confirmed"] = recovered_evidence
        confirmed_end_mono = time.monotonic()
        events.emit("recovery_confirmed", evidence=recovered_evidence)

        operation = "validate_workload"
        return_code = workload.wait()
        workload_ok, metadata, workload_reasons = validate_workload(
            run_dir, config, events, confirmed_start_mono, confirmed_end_mono, runner_clock_offset,
        )
        if return_code != 0:
            workload_ok = False
            workload_reasons.append({"code": "workload_exit_nonzero", "message": f"workload runner exited with status {return_code}"})
        ground_truth["validation"]["workload_valid"] = workload_ok
        ground_truth["validation"]["control_plane_valid"] = not any(r["code"] == "control_plane_dropped_flows" for r in workload_reasons)
        for reason in workload_reasons:
            add_reason(ground_truth, reason["code"], reason["message"])
        ground_truth["validation"]["fault_effect_observed"] = metadata.get("f4_fault_effect_observed") if config.fault_class == "F4" else None
        ground_truth["validation"]["post_recovery_success_observed"] = metadata.get("f4_post_recovery_success_observed") if config.fault_class == "F4" else None
        status = classify_run_status(
            fault_class=config.fault_class,
            injection_confirmed=ground_truth["validation"]["injection_confirmed"],
            recovery_confirmed=ground_truth["validation"]["recovery_confirmed"],
            workload_valid=workload_ok,
            control_plane_valid=ground_truth["validation"]["control_plane_valid"],
            workload_reasons=workload_reasons,
        )

        metadata["experiment"] = {
            "run_id": config.run_id, "fault_id": config.fault_id,
            "fault_class": config.fault_class, "ground_truth_file": "ground-truth.json",
        }
        write_json_atomic(run_dir / "run-metadata.json", metadata)
        workload_metadata = run_dir / f"{config.run_id}.json"
        if workload_metadata.exists():
            workload_metadata.unlink()
        events.emit("workload_finalized", exit_code=return_code, valid=workload_ok)

    except KeyboardInterrupt:
        status = "aborted"
        add_reason(ground_truth, "interrupted", "orchestrator interrupted by operator")
        events.emit("orchestrator_interrupted")
    except Exception as exc:
        status = classify_exception(operation)
        add_reason(ground_truth, "orchestrator_error", f"{type(exc).__name__}: {exc}")
        events.emit("orchestrator_error", error=f"{type(exc).__name__}: {exc}")
    finally:
        if prepared:
            try:
                cleanup_evidence = injector.cleanup()
                ground_truth["validation"]["cleanup"] = cleanup_evidence
                events.emit("cleanup_completed", evidence=cleanup_evidence)
            except Exception as exc:
                add_reason(ground_truth, "cleanup_failed", f"{type(exc).__name__}: {exc}")
                events.emit("cleanup_failed", error=f"{type(exc).__name__}: {exc}")
                if ground_truth["validation"].get("injection_requested"):
                    ground_truth["validation"]["recovery_confirmed"] = False
                    status = "invalid_recovery"
        if workload is not None and workload.poll() is None:
            workload.terminate()
            try:
                workload.wait(timeout=10)
            except subprocess.TimeoutExpired:
                workload.kill()
                workload.wait()
        workload_log.close()
        if ground_truth["validation"]["injection_confirmed"] and ground_truth["validation"]["recovery_confirmed"]:
            try:
                status_snapshot = {name: deployment_status(name) for name in ("frontend", "checkoutservice", "cartservice", "paymentservice", "redis-cart")}
                if all(s["ready"] for s in status_snapshot.values()):
                    ground_truth["validation"]["final_sut_ready"] = status_snapshot
                else:
                    add_reason(ground_truth, "final_sut_not_ready", "one or more core deployments are not Ready after recovery")
                    status = "invalid_recovery"
            except Exception as exc:
                add_reason(ground_truth, "final_validation_failed", str(exc))
                status = "invalid_recovery"
        if args.validate_observability and prepared:
            validator = REPO_ROOT / "scripts/experiment/validate-observability.sh"
            try:
                result = subprocess.run([str(validator)], cwd=REPO_ROOT, text=True, capture_output=True, timeout=300)
                ground_truth["validation"]["phase2_validator"] = {"exit_code": result.returncode, "stdout": result.stdout[-5000:], "stderr": result.stderr[-5000:]}
                if result.returncode:
                    add_reason(ground_truth, "phase2_validation_failed", "Phase 2 observability validation failed after fault recovery")
                    if status == "valid":
                        status = "invalid_recovery"
            except Exception as exc:
                add_reason(ground_truth, "phase2_validation_error", str(exc))
                if status == "valid":
                    status = "invalid_recovery"
        ground_truth["status"] = status
        ground_truth["completed_at"] = utc_now()
        write_json_atomic(gt_path, ground_truth)
        events.emit("ground_truth_written", status=status, path=str(gt_path))
    print(f"run_id={config.run_id} status={status} artifacts={run_dir}")
    return 0 if status == "valid" else 1


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.repetitions < 1:
        print("--repetitions must be >= 1", file=sys.stderr)
        return 2
    raw_config = json.loads(args.config.read_text(encoding="utf-8"))
    fault_class = raw_config.get("fault", {}).get("class", "fault").lower()
    base_id = args.run_id or fault_class
    run_ids = [base_id] if args.repetitions == 1 else [f"{base_id}-r{i:03d}" for i in range(1, args.repetitions + 1)]
    exit_code = 0

    def terminate_as_interrupt(_signum, _frame):
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, terminate_as_interrupt)
    try:
        for run_id in run_ids:
            try:
                config = ExperimentConfig.load(
                    args.config.resolve(), run_id, REPO_ROOT,
                    allow_unsupported_chaos_mesh=args.allow_unsupported_chaos_mesh,
                )
                if config.schedule["start_after_steady_seconds"] + config.schedule["duration_seconds"] + config.schedule["post_fault_steady_seconds"] >= config.steady_duration:
                    raise ConfigurationError("fault schedule must fit before steady workload admission ends")
                exit_code |= run_one(config, args)
            except Exception as exc:
                print(f"configuration/run setup failed for {run_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
                exit_code = 1
                break
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
