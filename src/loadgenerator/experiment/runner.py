"""Open-loop host runner; control-plane IDs and statistics stay out of the SUT."""

from __future__ import annotations

from gevent import monkey
monkey.patch_all()

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import gevent

from .config import WorkloadConfig
from .control import WorkloadControl
from .locustfile import FlowFailure, execute_checkout_flow


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def emit_control_event(event: str, run_id: str, **details) -> None:
    """Append a host-only event for an external experiment orchestrator."""
    destination = os.getenv("TCC_EVENT_FILE")
    if not destination:
        return
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "source": "workload",
        "event": event,
        "run_id": run_id,
        "timestamp": utc_now(),
        "monotonic_seconds": time.monotonic(),
        **details,
    }
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, separators=(",", ":")) + "\n")


def git_commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return os.getenv("TCC_BENCHMARK_COMMIT")


def parse_snapshot(variable: str) -> list[dict]:
    try:
        value = json.loads(os.getenv(variable, "[]"))
        return value if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


def preflight_frontend(config: WorkloadConfig) -> None:
    try:
        with urlopen(f"{config.frontend_url}/", timeout=config.request_timeout_seconds) as response:
            if response.status != 200:
                raise RuntimeError(f"frontend health check returned HTTP {response.status}")
    except (OSError, URLError) as exc:
        raise RuntimeError(f"frontend health check failed at {config.frontend_url}: {exc}") from exc


def checkout_profile_fingerprint(checkout: dict[str, str]) -> str:
    encoded = json.dumps(checkout, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 3)


class RequestMetrics:
    def __init__(self):
        self.total = 0
        self.failed = 0
        self.latencies: list[float] = []
        self.by_stage: dict[str, dict] = {}

    def record(self, stage: str, elapsed_ms: float, success: bool, status: int | None) -> None:
        self.total += 1
        self.failed += int(not success)
        self.latencies.append(elapsed_ms)
        item = self.by_stage.setdefault(stage, {"total": 0, "successful": 0, "failed": 0, "latency_ms": []})
        item["total"] += 1
        item["successful" if success else "failed"] += 1
        item["latency_ms"].append(elapsed_ms)
        if status is not None:
            statuses = item.setdefault("http_status", {})
            statuses[str(status)] = statuses.get(str(status), 0) + 1

    @staticmethod
    def _summary(values: list[float]) -> dict:
        return {
            "count": len(values),
            "average": round(statistics.fmean(values), 3) if values else None,
            "p50": quantile(values, 0.50),
            "p90": quantile(values, 0.90),
            "p95": quantile(values, 0.95),
            "p99": quantile(values, 0.99),
            "max": round(max(values), 3) if values else None,
        }

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "successful": self.total - self.failed,
            "failed": self.failed,
            "latency_ms": self._summary(self.latencies),
            "by_stage": {
                stage: {
                    **{key: value for key, value in item.items() if key != "latency_ms"},
                    "latency_ms": self._summary(item["latency_ms"]),
                }
                for stage, item in self.by_stage.items()
            },
        }


def lag_summary(values: list[float]) -> dict:
    return {"count": len(values), **RequestMetrics._summary(values)}


def drain_flows(control: WorkloadControl, greenlets: set, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while control.active_flows and time.monotonic() < deadline:
        gevent.sleep(0.02)
    if control.active_flows:
        for greenlet in tuple(greenlets):
            if not greenlet.ready():
                greenlet.kill(block=False)
        raise TimeoutError(f"{control.active_flows} flows did not drain in {timeout_seconds}s")


def run_open_loop_phase(
    *, config: WorkloadConfig, control: WorkloadControl, phase: str,
    duration_seconds: float, metrics: RequestMetrics, greenlets: set,
) -> dict:
    """Schedule each arrival at an absolute deadline, without waiting for flows."""
    start = time.monotonic()
    end = start + duration_seconds
    interval = 1.0 / config.target_flow_rate
    slot = 0

    def execute(flow: dict) -> None:
        failure = None
        try:
            emit_control_event("flow_started", config.run_id, flow_id=flow["flow_id"], phase=flow["phase"])
            execute_checkout_flow(config, record_request=metrics.record)
        except FlowFailure as exc:
            failure = {"stage": exc.stage, "http_status": exc.http_status, "error": str(exc)}
        except Exception as exc:  # defensive classification for unexpected client errors
            failure = {"stage": "unknown", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            control.end_flow(flow, completed=failure is None, failure=failure)
            emit_control_event(
                "flow_completed" if failure is None else "flow_failed",
                config.run_id,
                flow_id=flow["flow_id"],
                phase=flow["phase"],
                failure=failure,
            )

    while True:
        scheduled_at = start + slot * interval
        if scheduled_at >= end:
            break
        delay = scheduled_at - time.monotonic()
        if delay > 0:
            gevent.sleep(delay)
        actual_at = time.monotonic()
        flow, reason = control.schedule(phase, scheduled_at, actual_at)
        if flow is not None:
            greenlet = gevent.spawn(execute, flow)
            greenlets.add(greenlet)
            greenlet.link(lambda done, active=greenlets: active.discard(done))
        else:
            emit_control_event(
                "flow_dropped", config.run_id, phase=phase, reason=reason,
                scheduled_monotonic=scheduled_at, actual_monotonic=actual_at,
            )
        slot += 1

    # Keep the admission window open through its configured end even when the
    # final nominal arrival occurred earlier than that boundary.
    remaining = end - time.monotonic()
    if remaining > 0:
        gevent.sleep(remaining)
    return {
        "started_monotonic": start,
        "admission_ended_monotonic": time.monotonic(),
        "scheduled_slots": slot,
    }


def main() -> int:
    setup_started = utc_now()
    config = WorkloadConfig.from_env()
    output_path = config.artifact_dir / f"{config.run_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite run metadata: {output_path}")

    random.seed(config.seed)
    emit_control_event("setup_started", config.run_id)
    control = WorkloadControl(config.run_id, config.max_in_flight_flows, config.target_flow_rate)
    greenlets: set = set()
    metadata = {
        "schema_version": "1.1",
        "run_id": config.run_id,
        "benchmark": "Google Online Boutique",
        "benchmark_version": "v0.10.7",
        "benchmark_commit": git_commit(),
        "executor": config.executor,
        "workload_model": "open-loop",
        "seed": config.seed,
        "product_id": config.product_id,
        "checkout_profile": "tcc-test-profile-v1",
        "checkout_profile_sha256": checkout_profile_fingerprint(config.checkout),
        "flow_identity_scheme": "sequential control-plane ID; not propagated to the SUT",
        "control_plane_ids_propagated_to_sut": False,
        "target_flow_rate_per_second": config.target_flow_rate,
        "max_in_flight_flows": config.max_in_flight_flows,
        "observed_peak_in_flight_flows": 0,
        "warmup_peak_in_flight_flows": 0,
        "duration_seconds_configured": config.duration_seconds,
        "warmup_seconds_configured": config.warmup_seconds,
        "cooldown_seconds_configured": config.cooldown_seconds,
        "request_timeout_seconds": config.request_timeout_seconds,
        "setup_started_at": setup_started,
        "warmup_started_at": None,
        "warmup_ended_at": None,
        "workload_started_at": None,
        "admission_stopped_at": None,
        "workload_ended_at": None,
        "cooldown_started_at": None,
        "finished_at": None,
        "warmup_duration_seconds_actual": None,
        "steady_admission_seconds_actual": None,
        "workload_duration_seconds_actual": None,
        "cooldown_duration_seconds_actual": None,
        "warmup_flows": {},
        "warmup_requests": {},
        "flows": {},
        "scheduled_flows": 0,
        "started_flows": 0,
        "completed_flows": 0,
        "failed_flows": 0,
        "control_plane_dropped_flows": 0,
        "control_plane_drops_by_reason": {},
        "start_lag_ms": {"warmup": lag_summary([]), "steady": lag_summary([])},
        "requests": {},
        "observed_flow_start_rate_per_second": None,
        "observed_flow_completion_rate_per_second": None,
        "observed_requests_per_second": None,
        "observed_flows_per_second": None,
        "failures_by_stage": {},
        "failed_flow_samples": [],
        "pod_state_before": parse_snapshot("TCC_PODS_BEFORE_JSON"),
        "pod_state_after": [],
        "result": "running",
    }

    try:
        preflight_frontend(config)
        metadata["warmup_started_at"] = utc_now()
        emit_control_event("warmup_started", config.run_id)
        warmup_started = time.monotonic()
        warmup_metrics = RequestMetrics()
        run_open_loop_phase(
            config=config, control=control, phase="warmup", duration_seconds=config.warmup_seconds,
            metrics=warmup_metrics, greenlets=greenlets,
        )
        drain_flows(control, greenlets, config.drain_timeout_seconds)
        metadata["warmup_ended_at"] = utc_now()
        emit_control_event("warmup_ended", config.run_id)
        metadata["warmup_duration_seconds_actual"] = round(time.monotonic() - warmup_started, 3)
        metadata["warmup_flows"] = control.snapshot("warmup")
        metadata["warmup_peak_in_flight_flows"] = control.peak_in_flight_by_phase["warmup"]
        metadata["warmup_requests"] = warmup_metrics.as_dict()

        # Warm-up data is deliberately excluded from steady workload request/latency stats.
        metrics = RequestMetrics()
        metadata["workload_started_at"] = utc_now()
        emit_control_event("steady_started", config.run_id)
        steady = run_open_loop_phase(
            config=config, control=control, phase="steady", duration_seconds=config.duration_seconds,
            metrics=metrics, greenlets=greenlets,
        )
        metadata["admission_stopped_at"] = utc_now()
        emit_control_event("admission_stopped", config.run_id)
        admission_seconds = max(0.0, steady["admission_ended_monotonic"] - steady["started_monotonic"])
        metadata["steady_admission_seconds_actual"] = round(admission_seconds, 3)
        drain_flows(control, greenlets, config.drain_timeout_seconds)
        metadata["workload_ended_at"] = utc_now()
        emit_control_event("workload_ended", config.run_id)
        workload_seconds = max(0.001, time.monotonic() - steady["started_monotonic"])
        metadata["workload_duration_seconds_actual"] = round(workload_seconds, 3)
        metadata["cooldown_started_at"] = metadata["workload_ended_at"]
        emit_control_event("cooldown_started", config.run_id)

        counts = control.snapshot("steady")
        metadata["flows"] = counts
        metadata["scheduled_flows"] = counts["scheduled"]
        metadata["started_flows"] = counts["started"]
        metadata["completed_flows"] = counts["completed"]
        metadata["failed_flows"] = counts["failed"]
        metadata["control_plane_dropped_flows"] = counts["control_plane_dropped"]
        metadata["control_plane_drops_by_reason"] = dict(control.dropped_by_reason["steady"])
        metadata["warmup_control_plane_drops_by_reason"] = dict(control.dropped_by_reason["warmup"])
        metadata["observed_peak_in_flight_flows"] = control.peak_in_flight_by_phase["steady"]
        metadata["start_lag_ms"] = {
            phase: lag_summary(values) for phase, values in control.start_lags_by_phase.items()
        }
        metadata["requests"] = metrics.as_dict()
        metadata["failures_by_stage"] = dict(control.failures_by_stage)
        metadata["failed_flow_samples"] = control.failed_flow_samples
        metadata["observed_flow_start_rate_per_second"] = round(counts["started"] / admission_seconds, 3) if admission_seconds else 0
        metadata["observed_flow_completion_rate_per_second"] = round(counts["completed"] / workload_seconds, 3)
        metadata["observed_requests_per_second"] = round(metrics.total / workload_seconds, 3)
        metadata["observed_flows_per_second"] = metadata["observed_flow_completion_rate_per_second"]

        cooldown_start = time.monotonic()
        gevent.sleep(config.cooldown_seconds)
        metadata["cooldown_duration_seconds_actual"] = round(time.monotonic() - cooldown_start, 3)
        metadata["finished_at"] = utc_now()
        if counts["started"] == 0:
            metadata["result"] = "failed"
            metadata["fatal_error"] = "no steady flow was started"
        elif counts["failed"]:
            metadata["result"] = "completed_with_flow_failures"
        elif counts["control_plane_dropped"]:
            metadata["result"] = "completed_with_control_plane_drops"
        else:
            metadata["result"] = "passed"
    except Exception as exc:
        metadata["finished_at"] = utc_now()
        metadata["result"] = "failed"
        metadata["fatal_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        emit_control_event("workload_finished", config.run_id, result=metadata["result"])
        metadata["pod_state_after"] = parse_snapshot("TCC_PODS_AFTER_JSON")
        temp_path = output_path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        temp_path.replace(output_path)
        print(json.dumps(metadata, indent=2, sort_keys=True))

    return 0 if metadata["result"] in (
        "passed", "completed_with_flow_failures", "completed_with_control_plane_drops"
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
