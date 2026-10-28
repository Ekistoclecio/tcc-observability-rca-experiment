"""Run a bounded, controlled Locust workload and write out-of-band metadata."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import gevent
from locust.env import Environment

from .config import WorkloadConfig
from .control import WorkloadControl
from .locustfile import CheckoutFlowUser


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def git_commit() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return os.getenv("TCC_BENCHMARK_COMMIT")


def parse_snapshot(variable: str) -> list[dict]:
    raw = os.getenv(variable, "[]")
    try:
        value = json.loads(raw)
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


def wait_phase(seconds: float, control: WorkloadControl, runner) -> None:
    deadline = time.monotonic() + seconds
    while True:
        control.observe_users(runner.user_count)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        gevent.sleep(min(0.25, remaining))


def drain_flows(control: WorkloadControl, runner, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while control.active_flows and time.monotonic() < deadline:
        control.observe_users(runner.user_count)
        gevent.sleep(0.05)
    if control.active_flows:
        raise TimeoutError(f"{control.active_flows} flows did not drain in {timeout_seconds}s")


def percentile(stats, fraction: float) -> float | None:
    if not stats.num_requests:
        return None
    return round(float(stats.get_response_time_percentile(fraction)), 3)


def checkout_profile_fingerprint(checkout: dict[str, str]) -> str:
    encoded = json.dumps(checkout, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    setup_started = utc_now()
    config = WorkloadConfig.from_env()
    output_path = config.artifact_dir / f"{config.run_id}.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite run metadata: {output_path}")

    # The deterministic flow currently has no random choices. Seeding Python's
    # PRNG keeps any future residual Python randomness reproducible.
    random.seed(config.seed)
    preflight_frontend(config)

    control = WorkloadControl(config.run_id)
    control.config = config
    CheckoutFlowUser.network_timeout = config.request_timeout_seconds
    CheckoutFlowUser.connection_timeout = config.request_timeout_seconds
    environment = Environment(user_classes=[CheckoutFlowUser], host=config.frontend_url)
    environment.tcc_control = control
    user_errors: list[dict[str, str]] = []

    def record_user_error(user_instance, exception, tb):
        user_errors.append({
            "user_id": getattr(user_instance, "tcc_user_id", "unassigned"),
            "error": f"{type(exception).__name__}: {exception}",
        })

    environment.events.user_error.add_listener(record_user_error)
    runner = environment.create_local_runner()

    metadata = {
        "schema_version": "1.0",
        "run_id": config.run_id,
        "benchmark": "Google Online Boutique",
        "benchmark_version": "v0.10.7",
        "benchmark_commit": git_commit(),
        "workload_model": "closed-loop",
        "seed": config.seed,
        "product_id": config.product_id,
        "checkout_profile": "tcc-test-profile-v1",
        "checkout_profile_sha256": checkout_profile_fingerprint(config.checkout),
        "configured_users": config.users,
        "configured_user_ids": [f"user-{index:04d}" for index in range(1, config.users + 1)],
        "user_identity_scheme": "deterministic control-plane label plus isolated Locust cookie jar",
        "user_labels_propagated_to_sut": False,
        "observed_peak_active_users": 0,
        "spawn_rate_users_per_second": config.spawn_rate,
        "target_flow_rate_per_second": None,
        "think_time_seconds": config.wait_seconds,
        "warmup_seconds_configured": config.warmup_seconds,
        "duration_seconds_configured": config.duration_seconds,
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
        "workload_duration_seconds_actual": None,
        "cooldown_duration_seconds_actual": None,
        "warmup_flows": {"started": 0, "completed": 0, "failed": 0},
        "flows": {"started": 0, "completed": 0, "failed": 0},
        "requests": {},
        "latency_ms": {},
        "observed_requests_per_second": None,
        "observed_flows_per_second": None,
        "failures_by_stage": {},
        "failed_flow_samples": [],
        "locust_user_errors": user_errors,
        "pod_state_before": parse_snapshot("TCC_PODS_BEFORE_JSON"),
        "pod_state_after": [],
        "result": "running",
    }

    warmup_start_mono = time.monotonic()
    try:
        control.accepting_flows = True
        metadata["warmup_started_at"] = utc_now()
        runner.start(user_count=config.users, spawn_rate=config.spawn_rate)
        wait_phase(config.warmup_seconds, control, runner)

        control.accepting_flows = False
        drain_flows(control, runner, config.drain_timeout_seconds)
        metadata["warmup_ended_at"] = utc_now()
        metadata["warmup_duration_seconds_actual"] = round(time.monotonic() - warmup_start_mono, 3)
        metadata["warmup_flows"] = {
            "started": control.warmup_flows_started,
            "completed": control.warmup_flows_completed,
            "failed": control.warmup_flows_failed,
        }

        environment.stats.reset_all()
        workload_start_mono = time.monotonic()
        metadata["workload_started_at"] = utc_now()
        control.measurement_enabled = True
        control.accepting_flows = True
        wait_phase(config.duration_seconds, control, runner)

        control.accepting_flows = False
        metadata["admission_stopped_at"] = utc_now()
        drain_flows(control, runner, config.drain_timeout_seconds)
        metadata["workload_ended_at"] = utc_now()
        workload_duration = time.monotonic() - workload_start_mono
        metadata["workload_duration_seconds_actual"] = round(workload_duration, 3)
        metadata["cooldown_started_at"] = metadata["workload_ended_at"]

        total = environment.stats.total
        metadata["requests"] = {
            "total": total.num_requests,
            "failed": total.num_failures,
            "successful": total.num_requests - total.num_failures,
        }
        metadata["latency_ms"] = {
            "average": round(total.avg_response_time, 3) if total.num_requests else None,
            "median": round(total.median_response_time, 3) if total.num_requests else None,
            "p50": percentile(total, 0.50),
            "p90": percentile(total, 0.90),
            "p95": percentile(total, 0.95),
            "p99": percentile(total, 0.99),
        }
        metadata["flows"] = {
            "started": control.flows_started,
            "completed": control.flows_completed,
            "failed": control.flows_failed,
        }
        metadata["observed_peak_active_users"] = control.peak_active_users
        metadata["failures_by_stage"] = dict(control.failures_by_stage)
        metadata["failed_flow_samples"] = control.failed_flow_samples
        if workload_duration > 0:
            metadata["observed_requests_per_second"] = round(total.num_requests / workload_duration, 3)
            metadata["observed_flows_per_second"] = round(control.flows_completed / workload_duration, 3)

        cooldown_start_mono = time.monotonic()
        wait_phase(config.cooldown_seconds, control, runner)
        metadata["cooldown_duration_seconds_actual"] = round(time.monotonic() - cooldown_start_mono, 3)
        metadata["finished_at"] = utc_now()
        if user_errors or control.flows_started == 0:
            details = "; ".join(error["error"] for error in user_errors)
            metadata["result"] = "failed"
            metadata["fatal_error"] = details or "no measured flow started during the workload window"
        else:
            metadata["result"] = "passed" if control.flows_failed == 0 else "completed_with_flow_failures"
    except Exception as exc:
        control.accepting_flows = False
        metadata["finished_at"] = utc_now()
        metadata["result"] = "failed"
        metadata["fatal_error"] = f"{type(exc).__name__}: {exc}"
    finally:
        runner.quit()
        metadata["pod_state_after"] = parse_snapshot("TCC_PODS_AFTER_JSON")
        temp_path = output_path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
        temp_path.replace(output_path)
        print(json.dumps(metadata, indent=2, sort_keys=True))

    return 0 if metadata["result"] in ("passed", "completed_with_flow_failures") else 1


if __name__ == "__main__":
    sys.exit(main())
