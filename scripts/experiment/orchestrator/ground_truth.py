from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_json_atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = stream.name
    os.replace(temporary, path)


def initial_ground_truth(config, compatibility: dict | None = None) -> dict:
    return {
        "schema_version": "1.0",
        "run_id": config.run_id,
        "fault_id": config.fault_id,
        "status": "aborted",
        "fault": {
            **config.fault,
            "mechanism": {"name": {"F1": "Chaos Mesh StressChaos", "F2": "Chaos Mesh NetworkChaos", "F3": "Kubernetes scale-to-zero", "F4": "paymentservice file control hook", "F5": "Kubernetes scale-to-zero"}[config.fault_class]},
        },
        "schedule": {
            "start_after_steady_seconds": config.schedule["start_after_steady_seconds"],
            "duration_seconds": config.schedule["duration_seconds"],
            "post_fault_steady_seconds": config.schedule["post_fault_steady_seconds"],
        },
        "timing": {
            "scheduled_start": None, "requested_at": None, "confirmed_start": None,
            "scheduled_end": None, "recovery_requested_at": None, "confirmed_end": None,
            "steady_started_at": None, "steady_started_observed_monotonic": None,
            "scheduled_start_monotonic": None, "scheduled_end_monotonic": None,
            "requested_at_monotonic": None, "confirmed_start_monotonic": None,
            "recovery_requested_at_monotonic": None, "confirmed_end_monotonic": None,
        },
        "validation": {
            "injection_confirmed": False, "recovery_confirmed": False,
            "workload_valid": False, "control_plane_valid": False,
            "mechanism_confirmation": {}, "recovery_confirmation": {},
            "compatibility": compatibility or {}, "fault_effect_observed": None,
            "post_recovery_success_observed": None,
        },
        "reasons": [], "created_at": utc_now(), "completed_at": None,
    }


def add_reason(document: dict, code: str, message: str) -> None:
    document["reasons"].append({"code": code, "message": message})
