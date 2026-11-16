#!/usr/bin/env python3
"""Create a model input envelope containing only Dataset evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


class ContractError(RuntimeError):
    pass


FORBIDDEN_KEYS = {
    "ground_truth", "groundtruth", "fault_id", "faultid", "fault_type", "faulttype",
    "scenario", "scenario_id", "scenarioid", "run_id", "runid", "experiment_id",
    "experimentid", "chaos", "fault_mechanism", "faultmechanism", "control_plane",
    "controlplane", "injection_mechanism", "injectionmechanism",
}
FORBIDDEN_VALUE = re.compile(
    r"(?i)(?:\bf[1-5]\b|fault[_ -]?id|ground[_ -]?truth|scenario[_ -]?[ab12]|"
    r"chaos(?:mesh)?|fault[_ -]?injection|experiment[_ -]?id)"
)
EVIDENCE_ID = re.compile(r"^ev_[0-9a-f]{20}$")
TRACE_ID = re.compile(r"^[0-9a-fA-F]{32}$")
SPAN_ID = re.compile(r"^[0-9a-fA-F]{16}$")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> tuple[Any, bytes]:
    try:
        raw = path.read_bytes()
        return json.loads(raw), raw
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"cannot read JSON {path}: {exc}") from exc


def validate_no_experimental_metadata(value: Any, pointer: str = "") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9_]", "", str(key).lower())
            if normalized in FORBIDDEN_KEYS:
                raise ContractError(f"experimental metadata field is not model-visible: {pointer}/{key}")
            validate_no_experimental_metadata(nested, f"{pointer}/{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            validate_no_experimental_metadata(nested, f"{pointer}/{index}")
    elif isinstance(value, str) and FORBIDDEN_VALUE.search(value):
        raise ContractError(f"experimental/control-plane text is not model-visible: {pointer}")


def validate_fact(fact: Any, index: int) -> None:
    if not isinstance(fact, dict):
        raise ContractError(f"evidence[{index}] must be an object")
    signal = fact.get("signal")
    required = {
        "metric": {"id", "signal", "metric", "service", "value"},
        "log": {"id", "signal", "service", "timestamp", "severity", "message"},
        "trace": {"id", "signal", "service", "operation", "kind", "timestamp",
                  "duration_ms", "outcome", "trace_id", "span_id"},
    }.get(signal)
    if required is None:
        raise ContractError(f"evidence[{index}] has unsupported signal {signal!r}")
    allowed = {
        "metric": {"id", "signal", "metric", "service", "value", "availability", "reason",
                   "unit", "operation", "outcome", "target", "interval_start", "interval_end"},
        "log": {"id", "signal", "service", "timestamp", "severity", "message", "trace_id",
                "span_id", "operation", "outcome", "target", "attributes"},
        "trace": {"id", "signal", "service", "operation", "kind", "timestamp", "duration_ms",
                  "outcome", "trace_id", "span_id", "parent_span_id", "target", "attributes",
                  "error_message"},
    }[signal]
    unexpected = set(fact) - allowed
    if unexpected:
        raise ContractError(f"evidence[{index}] has unsupported fields: {sorted(unexpected)}")
    if not required.issubset(fact):
        raise ContractError(f"evidence[{index}] missing required fields: {sorted(required - set(fact))}")
    if not EVIDENCE_ID.fullmatch(str(fact["id"])):
        raise ContractError(f"evidence[{index}] has invalid evidence ID")
    if signal == "log":
        if not isinstance(fact["message"], str) or not fact["message"]:
            raise ContractError(f"evidence[{index}] log message must be non-empty")
        for key, pattern in (("trace_id", TRACE_ID), ("span_id", SPAN_ID)):
            if key in fact and not pattern.fullmatch(str(fact[key])):
                raise ContractError(f"evidence[{index}] has invalid {key}")
    if signal == "trace":
        if not TRACE_ID.fullmatch(str(fact["trace_id"])) or not SPAN_ID.fullmatch(str(fact["span_id"])):
            raise ContractError(f"evidence[{index}] has invalid trace/span IDs")


def build(dataset_path: Path, output_path: Path, provenance_path: Path) -> dict[str, Any]:
    dataset, dataset_bytes = read_json(dataset_path)
    if not isinstance(dataset, dict) or dataset.get("schema_version") != "1.0":
        raise ContractError("Dataset schema_version must be 1.0")
    facts = dataset.get("facts")
    if not isinstance(facts, list):
        raise ContractError("Dataset facts must be an array")
    if output_path.resolve() == provenance_path.resolve():
        raise ContractError("RCA input and provenance paths must differ")
    if output_path.exists() or provenance_path.exists():
        raise ContractError("refusing to overwrite an existing output")

    seen: set[str] = set()
    for index, fact in enumerate(facts):
        validate_fact(fact, index)
        if fact["id"] in seen:
            raise ContractError(f"duplicate evidence ID {fact['id']}")
        seen.add(fact["id"])
    validate_no_experimental_metadata(facts, "/evidence")

    # The model payload is an identity projection of Dataset facts. Provenance stays
    # in a separate audit sidecar and is never embedded in the model input.
    model_input = {"schema_version": "1.0", "evidence": facts}
    output_bytes = (json.dumps(model_input, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(output_bytes)
    evidence_sources = {
        fact["id"]: {
            "dataset_json_pointer": f"/facts/{index}",
            "fact_sha256": sha256((json.dumps(fact, sort_keys=True, separators=(",", ":"),
                                           ensure_ascii=False) + "\n").encode()),
        }
        for index, fact in enumerate(facts)
    }
    provenance = {
        "format": "tcc-rca-input-provenance-v1",
        "dataset_sha256": sha256(dataset_bytes),
        "rca_input_sha256": sha256(output_bytes),
        "evidence_sources": evidence_sources,
    }
    provenance_bytes = (json.dumps(provenance, indent=2, sort_keys=True) + "\n").encode()
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance_path.write_bytes(provenance_bytes)
    return {"status": "PASS", "evidence_count": len(facts),
            "dataset_sha256": provenance["dataset_sha256"],
            "rca_input_sha256": provenance["rca_input_sha256"],
            "provenance_sha256": sha256(provenance_bytes)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build(args.dataset, args.output, args.provenance)
    except (ContractError, OSError, ValueError, TypeError) as exc:
        print(f"RCA input contract failed closed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
