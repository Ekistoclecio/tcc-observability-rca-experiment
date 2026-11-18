"""Typed, secret-free configuration and deterministic run planning."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv

from scripts.experiment.llm.credentials import verify_env_git_safety


ROOT = Path(__file__).resolve().parents[2]
FAULT_VARIABLES = {
    "F1": ("EXPERIMENT_F1_CPU_SATURATION", "cpu_saturation"),
    "F2": ("EXPERIMENT_F2_NETWORK_LATENCY", "network_latency"),
    "F3": ("EXPERIMENT_F3_SERVICE_UNAVAILABLE", "service_unavailable"),
    "F4": ("EXPERIMENT_F4_APPLICATION_EXCEPTION", "application_exception"),
    "F5": ("EXPERIMENT_F5_DATASTORE_UNAVAILABLE", "datastore_unavailable"),
}
SUPPORTED_MODELS = {
    "gpt-6-luna": "scripts/experiment/llm/resources/pricing-luna-default-v1.json",
}
SUPPORTED_REASONING_MODES = {"standard"}
SUPPORTED_REASONING_EFFORTS = {"low", "medium", "high"}
SUPPORTED_SERVICE_TIERS = {"default"}


class ConfigurationError(ValueError):
    """Invalid or unsupported experiment configuration."""


@dataclass(frozen=True)
class RCAConfig:
    enabled: bool
    model: str
    reasoning_mode: str
    reasoning_effort: str
    max_output_tokens: int
    service_tier: str
    api_key_available: bool


@dataclass(frozen=True)
class ExperimentConfig:
    fault_repetitions: dict[str, int]
    rca: RCAConfig
    evaluation_enabled: bool
    env_file: str

    @property
    def physical_fault_instances(self) -> int:
        return sum(self.fault_repetitions.values())

    @property
    def treatment_cases(self) -> int:
        return self.physical_fault_instances * 2

    @property
    def expected_rca_inputs(self) -> int:
        return self.treatment_cases

    @property
    def instances(self) -> list[str]:
        return [f"{fault}-R{index:02d}"
                for fault in FAULT_VARIABLES
                for index in range(1, self.fault_repetitions[fault] + 1)]

    def manifest_configuration(self) -> dict:
        return {
            "faults": {
                fault: {"name": fault_name, "repetitions": self.fault_repetitions[fault]}
                for fault, (_, fault_name) in FAULT_VARIABLES.items()
            },
            "physical_fault_instances": self.physical_fault_instances,
            "treatment_cases": self.treatment_cases,
            "expected_rca_inputs": self.expected_rca_inputs,
            "rca": {
                "enabled": self.rca.enabled,
                "model": self.rca.model,
                "reasoning_mode": self.rca.reasoning_mode,
                "reasoning_effort": self.rca.reasoning_effort,
                "max_output_tokens": self.rca.max_output_tokens,
                "service_tier": self.rca.service_tier,
            },
            "evaluation": {"enabled": self.evaluation_enabled},
        }


def _integer(values: Mapping[str, str | None], name: str, default: str) -> int:
    raw = values.get(name, default)
    if raw is None or not raw.strip():
        raise ConfigurationError(f"{name} must be an integer >= 0")
    try:
        result = int(raw, 10)
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name} must be an integer >= 0") from None
    if result < 0:
        raise ConfigurationError(f"{name} must be an integer >= 0")
    return result


def _positive_integer(values: Mapping[str, str | None], name: str, default: str) -> int:
    raw = values.get(name, default)
    if raw is None or not raw.strip():
        raise ConfigurationError(f"{name} must be a positive integer")
    try:
        result = int(raw, 10)
    except (TypeError, ValueError):
        raise ConfigurationError(f"{name} must be a positive integer") from None
    if result < 1:
        raise ConfigurationError(f"{name} must be a positive integer")
    return result


def _boolean(values: Mapping[str, str | None], name: str, default: str) -> bool:
    raw = values.get(name, default)
    if raw is None:
        raise ConfigurationError(f"{name} must be true or false")
    normalized = raw.strip().lower()
    if normalized not in {"true", "false"}:
        raise ConfigurationError(f"{name} must be true or false")
    return normalized == "true"


def resolve_config(root: Path = ROOT, env_file: Path | None = None,
                   environ: Mapping[str, str] | None = None) -> ExperimentConfig:
    root = root.resolve()
    safety = verify_env_git_safety(root)
    selected_file = (env_file or root / ".env").resolve()
    if not selected_file.is_relative_to(root):
        raise ConfigurationError("configuration file must be inside the repository root")
    if environ is None and selected_file.is_file():
        try:
            load_dotenv(selected_file, override=False, encoding="utf-8")
        except Exception as exc:
            raise ConfigurationError(f"could not parse {selected_file.name} ({type(exc).__name__})") from None
    # When a mapping is supplied it is already the resolved process environment.
    # Otherwise load_dotenv applies override=False and the process environment wins.
    values: dict[str, str | None] = dict(os.environ if environ is None else environ)
    faults = {fault: _integer(values, variable, "1")
              for fault, (variable, _) in FAULT_VARIABLES.items()}
    if not any(faults.values()):
        raise ConfigurationError(
            "No fault repetitions configured. At least one EXPERIMENT_F*_... value must be greater than zero."
        )
    rca_enabled = _boolean(values, "RCA_ENABLED", "true")
    evaluation_enabled = _boolean(values, "EVALUATION_ENABLED", "true")
    if evaluation_enabled and not rca_enabled:
        raise ConfigurationError("EVALUATION_ENABLED=true requires RCA_ENABLED=true")
    model = (values.get("RCA_MODEL") or "gpt-6-luna").strip()
    if model not in SUPPORTED_MODELS:
        raise ConfigurationError("RCA model is not supported by the configured pricing/protocol registry.")
    reasoning_mode = (values.get("RCA_REASONING_MODE") or "standard").strip().lower()
    if reasoning_mode not in SUPPORTED_REASONING_MODES:
        raise ConfigurationError("RCA_REASONING_MODE must be one of: standard")
    reasoning_effort = (values.get("RCA_REASONING_EFFORT") or "medium").strip().lower()
    if reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
        raise ConfigurationError("RCA_REASONING_EFFORT must be one of: high, low, medium")
    service_tier = (values.get("RCA_SERVICE_TIER") or "default").strip().lower()
    if service_tier not in SUPPORTED_SERVICE_TIERS:
        raise ConfigurationError("RCA_SERVICE_TIER must be one of: default")
    max_output_tokens = _positive_integer(values, "RCA_MAX_OUTPUT_TOKENS", "16384")
    api_key_available = bool((values.get("OPENAI_API_KEY") or "").strip())
    if rca_enabled and not api_key_available:
        raise ConfigurationError("OPENAI_API_KEY is required when RCA_ENABLED=true")
    # Keep the git-safety result intentionally secret-free; no key enters config.
    if safety["env_tracked"] or not safety["env_ignored"]:
        raise ConfigurationError("repository root .env must be ignored and untracked")
    return ExperimentConfig(
        fault_repetitions=faults,
        rca=RCAConfig(rca_enabled, model, reasoning_mode, reasoning_effort,
                      max_output_tokens, service_tier, api_key_available),
        evaluation_enabled=evaluation_enabled,
        env_file=str(selected_file),
    )


def configuration_plan(config: ExperimentConfig) -> dict:
    return {
        "fault_repetitions": dict(config.fault_repetitions),
        "physical_fault_instances": config.physical_fault_instances,
        "treatment_cases": config.treatment_cases,
        "expected_rca_inputs": config.expected_rca_inputs,
        "configuration": config.manifest_configuration(),
    }


def format_plan(config: ExperimentConfig) -> str:
    names = {
        "F1": "CPU saturation", "F2": "network latency",
        "F3": "service unavailable", "F4": "application exception",
        "F5": "datastore unavailable",
    }
    rows = ["EXPERIMENT CONFIGURATION", "", "Fault repetitions", "-----------------"]
    rows.extend(f"{fault} {names[fault]}: {config.fault_repetitions[fault]}"
                for fault in FAULT_VARIABLES)
    rows += ["", f"Physical fault instances: {config.physical_fault_instances}",
             "Treatments per instance:  2", f"Expected RCA inputs:       {config.expected_rca_inputs}",
             "", "RCA", "---",
             f"Enabled:                   {'yes' if config.rca.enabled else 'no'}",
             f"Model:                     {config.rca.model}",
             f"Reasoning mode:            {config.rca.reasoning_mode}",
             f"Reasoning effort:          {config.rca.reasoning_effort}",
             f"Max output tokens:         {config.rca.max_output_tokens}",
             f"Service tier:              {config.rca.service_tier}", "", "Evaluation", "----------",
             f"Enabled:                   {'yes' if config.evaluation_enabled else 'no'}"]
    return "\n".join(rows)
