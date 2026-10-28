"""Versioned fault matrix and run-configuration validation."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shlex


FAULT_MATRIX = {
    "F1": ("cpu_saturation", {"kind": "service", "component": "checkoutservice"}),
    "F2": ("network_latency", {
        "kind": "service_edge", "source": "checkoutservice", "target": "paymentservice",
    }),
    "F3": ("service_unavailable", {"kind": "service", "component": "paymentservice"}),
    "F4": ("application_exception", {
        "kind": "operation", "component": "paymentservice", "operation": "Charge",
    }),
    "F5": ("datastore_unavailable", {"kind": "datastore", "component": "redis-cart"}),
}


class ConfigurationError(ValueError):
    pass


def read_env_file(path: Path) -> dict[str, str]:
    """Parse the simple KEY=value env files used by the Phase 3 workload."""
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text().splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = shlex.split(raw, comments=True)
        if len(parts) != 1 or "=" not in parts[0]:
            raise ConfigurationError(f"invalid workload env line {line_number} in {path}")
        key, value = parts[0].split("=", 1)
        values[key] = value
    return values


@dataclass(frozen=True)
class ExperimentConfig:
    path: Path
    run_id: str
    fault: dict
    schedule: dict[str, float]
    workload_env_file: Path
    workload_env: dict[str, str]
    workload_overrides: dict[str, str]
    chaos_mesh_version: str
    chaos_supported_kubernetes_minors: tuple[int, ...]
    allow_unsupported_chaos_mesh: bool = False

    @property
    def fault_id(self) -> str:
        return f"fault-{self.run_id}"

    @property
    def fault_class(self) -> str:
        return self.fault["class"]

    @property
    def steady_duration(self) -> float:
        return float(self.workload_env["TCC_DURATION"])

    @classmethod
    def load(
        cls,
        path: Path,
        run_id: str,
        repo_root: Path,
        *,
        allow_unsupported_chaos_mesh: bool = False,
        chaos_mesh_version: str = "2.8.6",
        chaos_supported_kubernetes_minors: tuple[int, ...] = (30, 31, 32, 33, 34, 35),
    ) -> "ExperimentConfig":
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,50}", run_id):
            raise ConfigurationError("run_id must be 1-51 lowercase DNS-safe characters")
        raw = json.loads(path.read_text())
        if raw.get("schema_version") != "1.0":
            raise ConfigurationError("fault config schema_version must be 1.0")
        fault = raw.get("fault", {})
        fault_class = fault.get("class")
        if fault_class not in FAULT_MATRIX:
            raise ConfigurationError(f"unsupported fault class: {fault_class}")
        expected_type, expected_location = FAULT_MATRIX[fault_class]
        if fault.get("type") != expected_type:
            raise ConfigurationError(f"{fault_class} type must be {expected_type}")
        if fault.get("location") != expected_location:
            raise ConfigurationError(f"{fault_class} location is fixed: {expected_location}")
        parameters = fault.get("parameters", {})
        if fault_class == "F1":
            if int(parameters.get("workers", 0)) < 1 or not 1 <= int(parameters.get("load_percent", 0)) <= 100:
                raise ConfigurationError("F1 requires workers >= 1 and load_percent in 1..100")
        elif fault_class == "F2":
            if int(parameters.get("latency_ms", 0)) <= 0:
                raise ConfigurationError("F2 latency_ms must be positive")
            if not 0 <= int(parameters.get("correlation_percent", -1)) <= 100:
                raise ConfigurationError("F2 correlation_percent must be in 0..100")
            if int(parameters.get("jitter_ms", -1)) < 0:
                raise ConfigurationError("F2 jitter_ms cannot be negative")
        elif fault_class in ("F3", "F5"):
            if int(parameters.get("replicas_during_fault", -1)) != 0:
                raise ConfigurationError(f"{fault_class} must use scale-to-zero")
            if "latency_ms" in parameters or "jitter_ms" in parameters:
                raise ConfigurationError(f"{fault_class} is binary unavailability; latency/degradation is forbidden")
        elif fault_class == "F4" and parameters.get("mode") != "controlled_exception":
            raise ConfigurationError("F4 mode must be controlled_exception")

        schedule_raw = raw.get("schedule", {})
        schedule = {
            key: float(schedule_raw.get(key, -1))
            for key in ("start_after_steady_seconds", "duration_seconds", "post_fault_steady_seconds")
        }
        if schedule["start_after_steady_seconds"] <= 0:
            raise ConfigurationError("fault must have a positive steady pre-fault window")
        if schedule["duration_seconds"] <= 0:
            raise ConfigurationError("fault duration_seconds must be positive")
        if schedule["post_fault_steady_seconds"] <= 0:
            raise ConfigurationError("fault must leave a positive steady post-fault window")

        env_file = (repo_root / raw["workload"]["env_file"]).resolve()
        if not env_file.is_relative_to(repo_root.resolve()) or not env_file.is_file():
            raise ConfigurationError("workload env_file must be an existing repository file")
        workload_env = read_env_file(env_file)
        overrides = {str(key): str(value) for key, value in raw["workload"].get("overrides", {}).items()}
        if any(not key.startswith("TCC_") for key in overrides):
            raise ConfigurationError("workload overrides may only set TCC_* environment values")
        workload_env.update(overrides)
        try:
            steady_duration = float(workload_env["TCC_DURATION"])
        except (KeyError, ValueError) as exc:
            raise ConfigurationError("workload requires a numeric TCC_DURATION") from exc
        timeline_end = (
            schedule["start_after_steady_seconds"]
            + schedule["duration_seconds"]
            + schedule["post_fault_steady_seconds"]
        )
        if timeline_end >= steady_duration:
            raise ConfigurationError(
                f"fault schedule ends at {timeline_end}s, not before steady duration {steady_duration}s"
            )

        return cls(
            path=path.resolve(),
            run_id=run_id,
            fault=fault,
            schedule=schedule,
            workload_env_file=env_file,
            workload_env=workload_env,
            workload_overrides=overrides,
            chaos_mesh_version=chaos_mesh_version,
            chaos_supported_kubernetes_minors=chaos_supported_kubernetes_minors,
            allow_unsupported_chaos_mesh=allow_unsupported_chaos_mesh,
        )
