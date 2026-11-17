"""Deterministic blind case planning and request identity."""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Mapping

from .protocol import ROOT, canonical_json_bytes, sha256, _input_hash_entries, _load_protocol

PLAN_PATH = ROOT / "docs/experiment/llm/execution-plan-v1.json"


def request_identity_fields(input_sha256: str) -> dict[str, Any]:
    protocol = _load_protocol()
    request_config = {
        "protocol_version": protocol["protocol_version"],
        "model": protocol["model"],
        "reasoning_mode": protocol["reasoning_mode"],
        "reasoning_effort": protocol["reasoning_effort"],
        "service_tier": protocol["service_tier"],
        "store": protocol["store"],
        "stream": protocol["stream"],
        "text_verbosity": protocol["text_verbosity"],
        "max_output_tokens": protocol["max_output_tokens"],
        "prompt_sha256": protocol["prompt_sha256"],
        "schema_sha256": protocol["schema_sha256"],
        "input_sha256": input_sha256,
        "tools": [],
        "temperature": "OMITTED",
        "top_p": "OMITTED",
        "previous_response_id": "NONE",
    }
    return {**request_config,
            "request_fingerprint": sha256(canonical_json_bytes(request_config))}


def build_execution_plan() -> dict[str, Any]:
    protocol = _load_protocol()
    input_entries = _input_hash_entries()
    records = []
    for path, input_sha in input_entries.items():
        order_key = hashlib.sha256(
            ("llm-rca-pilot-v1-order:" + input_sha).encode("ascii")
        ).hexdigest()
        opaque_case_id = "case-" + hashlib.sha256(
            ("llm-rca-pilot-v1-case:" + input_sha).encode("ascii")
        ).hexdigest()[:12]
        records.append({"opaque_case_id": opaque_case_id,
                        "input_sha256": input_sha,
                        "order_key": order_key,
                        "_private_input_path": path})
    records.sort(key=lambda item: item["order_key"])
    if len({item["opaque_case_id"] for item in records}) != len(records):
        raise ValueError("opaque case ID collision")
    call_order = [
        {"ordinal": ordinal, "opaque_case_id": item["opaque_case_id"],
         "input_sha256": item["input_sha256"], "order_key": item["order_key"]}
        for ordinal, item in enumerate(records, 1)
    ]
    plan = {
        "protocol_version": protocol["protocol_version"],
        "prompt_sha256": protocol["prompt_sha256"],
        "schema_sha256": protocol["schema_sha256"],
        "logical_call_count": len(records),
        "ordering": "sha256(input bytes) deterministic",
        "order_algorithm": "sha256('llm-rca-pilot-v1-order:' + input_sha256), lexicographic",
        "cases": call_order,
    }
    return plan


def write_or_verify_execution_plan(path=PLAN_PATH) -> tuple[dict[str, Any], str]:
    plan = build_execution_plan()
    serialized = json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    expected = serialized.encode("utf-8")
    if path.exists():
        existing = path.read_bytes()
        if existing != expected:
            raise ValueError("frozen execution plan differs from deterministic plan")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)
    return plan, sha256(expected)


def input_path_by_hash() -> dict[str, str]:
    return {digest: path for path, digest in _input_hash_entries().items()}


def load_case_input(plan_case: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    import json as json_module
    from .protocol import RESULTS_ROOT, extract_evidence_ids

    path_map = input_path_by_hash()
    relative = path_map.get(plan_case["input_sha256"])
    if relative is None or PurePosixPath(relative).is_absolute():
        raise ValueError("execution plan input hash is not part of frozen RCA input manifest")
    source = (ROOT / relative).resolve(strict=True)
    if not source.is_relative_to(RESULTS_ROOT.resolve()):
        raise ValueError("planned RCA Input escapes frozen results")
    raw = source.read_bytes()
    if sha256(raw) != plan_case["input_sha256"]:
        raise ValueError("RCA Input hash changed since execution plan creation")
    rca_input = json_module.loads(raw)
    extract_evidence_ids(rca_input)
    return rca_input, relative
