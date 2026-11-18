"""Blind OpenAI Responses API pilot runner; model calls are explicit CLI actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import jsonschema
import openai
from openai import OpenAI

if __package__ in (None, ""):
    # Allow `python /absolute/repo/path/scripts/experiment/llm/runner.py ...`
    # from arbitrary working directories while keeping root discovery anchored
    # to this source file rather than the caller's cwd.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    __package__ = "scripts.experiment.llm"

from .accounting import account_usage
from . import execution as execution_module, protocol as protocol_module
from .credentials import (CredentialPreflightError, require_openai_credential,
                          verify_env_git_safety)
from .execution import (build_execution_plan, load_case_input,
                        request_identity_fields, write_or_verify_execution_plan)
from .protocol import (ARTIFACTS, ROOT, build_openai_rca_request, configure_protocol,
                       canonical_json_bytes, sha256, verify_frozen_inputs,
                       verify_protocol_artifacts)
from .response_validator import RCAResponseValidationError, validate_rca_response

MAX_TRANSPORT_ATTEMPTS = 3
BACKOFF_SECONDS = (1.0, 2.0)
PILOT_ROOT = ROOT / "artifacts/experiment/llm/run-v1"
CONTROL_SMOKE_ROOT = ROOT / "artifacts/experiment/llm/control-smoke"
TRANSIENT_STATUS_CODES = {408, 409, 429}


class PilotStopError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_default(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if hasattr(value, "dict"):
        return value.dict(exclude_none=True)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, sort_keys=True, ensure_ascii=False,
                               indent=2, default=_json_default, allow_nan=False) + "\n",
                     encoding="utf-8")
    temp.replace(path)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise PilotStopError(f"expected JSON object in {path.name}")
    return value


def _response_value(response: Any, name: str, default=None):
    if isinstance(response, dict):
        return response.get(name, default)
    return getattr(response, name, default)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=True)
    if hasattr(value, "dict"):
        return value.dict(exclude_none=True)
    return {}


def _response_usage(response: Any) -> Any:
    return _response_value(response, "usage")


def _usage_metadata(response: Any) -> dict[str, Any]:
    usage = _response_usage(response)
    return account_usage(usage, _response_value(response, "service_tier"))


def _response_output(response: Any) -> tuple[str | None, str | None]:
    """Return (structured text, refusal text) without retaining hidden reasoning."""
    output = _response_value(response, "output", []) or []
    text_parts: list[str] = []
    refusal_parts: list[str] = []
    for item in output:
        item_dict = _as_dict(item)
        for content in item_dict.get("content", []) or []:
            content_dict = _as_dict(content)
            kind = content_dict.get("type")
            if kind == "refusal":
                refusal_parts.append(str(content_dict.get("refusal", content_dict.get("text", ""))))
            elif kind == "output_text":
                text_parts.append(str(content_dict.get("text", "")))
    direct = _response_value(response, "output_text")
    if direct and not text_parts:
        text_parts.append(str(direct))
    return ("".join(text_parts) or None, "".join(refusal_parts) or None)


def classify_model_outcome(response: Any, rca_input: Mapping[str, Any]) -> dict[str, Any]:
    status = _response_value(response, "status")
    response_meta = {
        "response_id": _response_value(response, "id"),
        "requested_model": protocol_module._load_protocol()["model"],
        "returned_model": _response_value(response, "model"),
        "service_tier_requested": protocol_module._load_protocol()["service_tier"],
        "service_tier_returned": _response_value(response, "service_tier"),
        "api_status": status,
        "created_at": _response_value(response, "created_at"),
        "incomplete_details": _as_dict(_response_value(response, "incomplete_details")) or None,
        "usage": _usage_metadata(response),
    }
    text_output, refusal = _response_output(response)
    if refusal is not None:
        response_meta["refusal"] = refusal
        return {"status": "REFUSAL", "response_metadata": response_meta,
                "prediction": None, "validation": {"status": "NOT_APPLICABLE"},
                "raw_output": refusal}
    if status == "incomplete":
        return {"status": "INCOMPLETE", "response_metadata": response_meta,
                "prediction": None, "validation": {"status": "NOT_APPLICABLE"},
                "raw_output": text_output}
    if status != "completed" or text_output is None:
        return {"status": "STRUCTURED_OUTPUT_INVALID", "response_metadata": response_meta,
                "prediction": None,
                "validation": {"status": "FAIL", "code": "MISSING_COMPLETED_STRUCTURED_OUTPUT"},
                "raw_output": text_output}
    try:
        parsed = json.loads(text_output)
        _, schema = verify_protocol_artifacts()
        jsonschema.Draft202012Validator(schema).validate(parsed)
    except (json.JSONDecodeError, jsonschema.ValidationError, jsonschema.SchemaError) as exc:
        return {"status": "STRUCTURED_OUTPUT_INVALID", "response_metadata": response_meta,
                "prediction": None,
                "validation": {"status": "FAIL", "code": "STRUCTURED_OUTPUT_INVALID",
                               "error_type": type(exc).__name__}, "raw_output": text_output}
    try:
        validate_rca_response(parsed, rca_input)
    except RCAResponseValidationError as exc:
        return {"status": "SEMANTICALLY_INVALID", "response_metadata": response_meta,
                "prediction": None,
                "validation": {"status": "FAIL", "code": exc.code, "detail": exc.detail},
                "raw_output": text_output}
    return {"status": "COMPLETED", "response_metadata": response_meta,
            "prediction": parsed, "validation": {"status": "PASS"},
            "raw_output": text_output}


def _is_transient(exc: BaseException, sdk: Any = openai) -> bool:
    if isinstance(exc, (sdk.APIConnectionError, sdk.APITimeoutError)):
        return True
    if isinstance(exc, sdk.APIStatusError):
        code = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None)
        error = body.get("error") if isinstance(body, dict) else None
        api_code = error.get("code") if isinstance(error, dict) else None
        if api_code in {"insufficient_quota", "billing_hard_limit_reached", "usage_limit_reached"}:
            return False
        return code in TRANSIENT_STATUS_CODES or (isinstance(code, int) and 500 <= code <= 599)
    return False


def _safe_error_metadata(exc: BaseException) -> dict[str, Any]:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    request_id = getattr(exc, "request_id", None)
    if request_id is None and response is not None:
        request_id = getattr(response, "headers", {}).get("x-request-id")
    body = getattr(exc, "body", None)
    error_code = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            error_code = error.get("code")
    return {"exception_type": type(exc).__name__, "http_status": status,
            "api_error_code": error_code, "request_id": request_id}


def request_fingerprint(request: Mapping[str, Any], input_sha256: str) -> str:
    identity = request_identity_fields(input_sha256)
    protocol = protocol_module._load_protocol()
    expected = {
        "model": protocol["model"],
        "reasoning": {"mode": protocol["reasoning_mode"], "effort": protocol["reasoning_effort"]},
        "service_tier": protocol["service_tier"], "store": protocol["store"],
        "stream": protocol["stream"], "max_output_tokens": protocol["max_output_tokens"],
        "text_verbosity": protocol["text_verbosity"], "tools": protocol["tools"],
    }
    actual = {"model": request["model"], "reasoning": request["reasoning"],
              "service_tier": request["service_tier"], "store": request["store"],
              "stream": request["stream"], "max_output_tokens": request["max_output_tokens"],
              "text_verbosity": request["text"]["verbosity"], "tools": request["tools"]}
    if actual != expected:
        raise PilotStopError("request configuration differs from frozen protocol")
    prompt_hash = sha256(str(request["instructions"]).encode("utf-8"))
    schema_hash = sha256(canonical_json_bytes(request["text"]["format"]["schema"]))
    if (prompt_hash != protocol["prompt_sha256"]
            or schema_hash != protocol["schema_sha256"]
            or request["text"]["format"].get("name") != protocol["structured_output_name"]
            or request["text"]["format"].get("strict") is not True):
        raise PilotStopError("request prompt/schema differs from frozen protocol")
    return identity["request_fingerprint"]


def _perform_logical_call(client: Any, request: Mapping[str, Any], rca_input: Mapping[str, Any],
                          sleeper: Callable[[float], None] = time.sleep,
                          max_attempts: int = MAX_TRANSPORT_ATTEMPTS,
                          first_attempt_number: int = 1) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    attempts: list[dict[str, Any]] = []
    logical_start = time.monotonic()
    for offset in range(max_attempts):
        number = first_attempt_number + offset
        wall_started = utc_now()
        attempt_start = time.monotonic()
        try:
            response = client.responses.create(**request)
        except Exception as exc:
            latency = (time.monotonic() - attempt_start) * 1000
            error = _safe_error_metadata(exc)
            attempts.append({"attempt": number, "started_at": wall_started,
                             "finished_at": utc_now(), "latency_ms": round(latency, 3), "outcome": "TRANSPORT_ERROR",
                             "error": error})
            if _is_transient(exc) and offset + 1 < max_attempts:
                sleeper(BACKOFF_SECONDS[min(offset, len(BACKOFF_SECONDS) - 1)])
                continue
            return ({"status": "TRANSPORT_FAILED", "response_metadata": None,
                     "prediction": None, "validation": {"status": "NOT_AVAILABLE"},
                     "logical_latency_ms": round((time.monotonic() - logical_start) * 1000, 3)}, attempts)
        latency = (time.monotonic() - attempt_start) * 1000
        attempts.append({"attempt": number, "started_at": wall_started,
                         "finished_at": utc_now(), "latency_ms": round(latency, 3), "outcome": "MODEL_RESPONSE"})
        outcome = classify_model_outcome(response, rca_input)
        outcome["logical_latency_ms"] = round((time.monotonic() - logical_start) * 1000, 3)
        outcome["attempt_latency_ms"] = round(latency, 3)
        outcome["latency_clean"] = number == 1
        return outcome, attempts
    raise AssertionError("bounded attempt loop exited unexpectedly")


def _run_id() -> str:
    version = protocol_module._load_protocol()["protocol_version"]
    return version + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def _case_path(run_dir: Path, case_id: str) -> Path:
    return run_dir / "cases" / case_id


def _load_run_manifest(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "run-manifest.json"
    if not manifest_path.exists():
        return {}
    return _read_json(manifest_path)


def _atomic_status(case_dir: Path, status: str, record: dict[str, Any]) -> None:
    record["status"] = status
    _write_json(case_dir / "request-metadata.json", record)


def _make_request_metadata(case_id: str, ordinal: int, input_hash: str,
                           relative_path: str, request_fp: str) -> dict[str, Any]:
    return {"opaque_case_id": case_id, "ordinal": ordinal,
            "input_sha256": input_hash, "private_source_path": relative_path,
            "request_fingerprint": request_fp,
            "runner_protocol_version": protocol_module._load_protocol()["protocol_version"]}


def write_hash_manifest(root: Path) -> list[tuple[str, str]]:
    entries = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and p.name != "hashes.sha256"):
        rel = path.relative_to(root).as_posix()
        entries.append((hashlib.sha256(path.read_bytes()).hexdigest(), rel))
    text = "".join(f"{digest}  {rel}\n" for digest, rel in entries)
    (root / "hashes.sha256").write_text(text, encoding="utf-8")
    return entries


def verify_hash_manifest(root: Path) -> None:
    expected = {}
    for line in (root / "hashes.sha256").read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        if relative in expected:
            raise PilotStopError("duplicate path in output integrity manifest")
        expected[relative] = digest
    actual_paths = {path.relative_to(root).as_posix() for path in root.rglob("*")
                    if path.is_file() and path.name != "hashes.sha256"}
    if actual_paths != set(expected):
        raise PilotStopError("output integrity manifest does not cover exactly the artifact files")
    for relative, digest in expected.items():
        if sha256((root / relative).read_bytes()) != digest:
            raise PilotStopError("output artifact SHA-256 mismatch")


def verify_completed_run(run_dir: Path, plan: Mapping[str, Any]) -> None:
    verify_hash_manifest(run_dir)
    expected_plan = (json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    if (run_dir / "execution-plan.json").read_bytes() != expected_plan:
        raise PilotStopError("persisted execution plan differs from frozen plan")
    manifest = _read_json(run_dir / "run-manifest.json")
    expected_protocol = protocol_module._load_protocol()
    if manifest.get("prompt_sha256") != expected_protocol["prompt_sha256"]:
        raise PilotStopError("run prompt hash differs from frozen protocol")
    if manifest.get("schema_sha256") != expected_protocol["schema_sha256"]:
        raise PilotStopError("run schema hash differs from frozen protocol")
    if manifest.get("protocol_manifest_sha256") != sha256(protocol_module.PROTOCOL_PATH.read_bytes()):
        raise PilotStopError("run protocol manifest hash differs from frozen protocol")
    cases_root = run_dir / "cases"
    actual_case_dirs = {path.name for path in cases_root.iterdir() if path.is_dir()}
    expected_case_ids = {case["opaque_case_id"] for case in plan["cases"]}
    if len(plan["cases"]) != 10 or actual_case_dirs != expected_case_ids:
        raise PilotStopError("completed run does not contain exactly ten planned case records")
    verified_inputs = verify_frozen_inputs()
    for case in plan["cases"]:
        case_dir = _case_path(run_dir, case["opaque_case_id"])
        record = _read_json(case_dir / "request-metadata.json")
        if record.get("status") not in {"COMPLETED", "REFUSAL", "INCOMPLETE",
                                        "STRUCTURED_OUTPUT_INVALID", "SEMANTICALLY_INVALID"}:
            raise PilotStopError("completed run contains a nonterminal case status")
        if record.get("input_sha256") != case["input_sha256"] or case["input_sha256"] not in verified_inputs.values():
            raise PilotStopError("case input hash differs from frozen input set")
        rca_input, relative = load_case_input(case)
        if record.get("private_source_path") != relative:
            raise PilotStopError("private case mapping differs from frozen input mapping")
        request = build_openai_rca_request(rca_input)
        if record.get("request_fingerprint") != request_fingerprint(request, case["input_sha256"]):
            raise PilotStopError("request fingerprint mismatch")
        response_meta_path = case_dir / "response-metadata.json"
        if not response_meta_path.is_file():
            raise PilotStopError("terminal model outcome has no response metadata")
        response_meta = _read_json(response_meta_path)
        if response_meta.get("response_id") is None:
            raise PilotStopError("model response ID is missing from audit metadata")
        prediction_path = case_dir / "prediction.json"
        if record["status"] == "COMPLETED":
            if not prediction_path.is_file():
                raise PilotStopError("valid completed outcome has no prediction artifact")
            validate_rca_response(_read_json(prediction_path), rca_input)
        elif prediction_path.exists():
            raise PilotStopError("invalid/refusal/incomplete outcome has a prediction artifact")


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = [record["status"] for record in records]
    usage_keys = ("input_tokens", "cached_input_tokens", "output_tokens",
                  "reasoning_tokens", "total_tokens")
    totals: dict[str, int | None] = {}
    for key in usage_keys:
        values = [(record.get("usage") or {}).get(key) for record in records]
        totals[key] = sum(values) if all(type(value) is int for value in values) else None
    from decimal import Decimal
    costs = [(record.get("usage") or {}).get("estimated_cost_usd") for record in records]
    total_cost = None if any(value is None for value in costs) else format(
        sum((Decimal(value) for value in costs), Decimal(0)), "f")
    clean_latency = [record.get("latency_ms") for record in records if record.get("latency_clean")
                     and isinstance(record.get("latency_ms"), (int, float))]
    ordered_latency = sorted(clean_latency)
    p95 = ordered_latency[max(0, math.ceil(0.95 * len(ordered_latency)) - 1)] if ordered_latency else None
    return {
        "expected_logical_cases": 10,
        "logical_cases": len(records),
        "completed_valid": statuses.count("COMPLETED"),
        "refused": statuses.count("REFUSAL"),
        "incomplete": statuses.count("INCOMPLETE"),
        "structured_invalid": statuses.count("STRUCTURED_OUTPUT_INVALID"),
        "semantic_invalid": statuses.count("SEMANTICALLY_INVALID"),
        "transport_failed": statuses.count("TRANSPORT_FAILED"),
        "total_transport_attempts": sum(len(record.get("attempts", [])) for record in records),
        "cases_with_retry": sum(1 for record in records if len(record.get("attempts", [])) > 1),
        **totals,
        "estimated_cost_usd": total_cost,
        "cost_status": "ESTIMATED" if total_cost is not None else "PARTIAL_OR_UNAVAILABLE",
        "pricing_version": protocol_module._load_protocol().get("pricing_version", "pricing-v1"),
        "clean_single_attempt_cases": len(clean_latency),
        "median_clean_latency_ms": statistics.median(clean_latency) if clean_latency else None,
        "p95_clean_latency_ms": p95,
        "parallelism": 1,
        "ground_truth_loaded": False,
        "a_b_evaluation_performed": False,
    }


def run_pilot(client: Any, run_dir: Path, plan: Mapping[str, Any], resume: bool = False,
              sleeper: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    verify_protocol_artifacts()
    frozen_hashes = verify_frozen_inputs()
    plan_bytes = (json.dumps(plan, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    plan_hash = sha256(plan_bytes)
    manifest_path = run_dir / "run-manifest.json"
    if run_dir.exists() and not resume and any(run_dir.iterdir()):
        raise PilotStopError("run directory already contains artifacts; resume explicitly")
    run_dir.mkdir(parents=True, exist_ok=True)
    if resume and (run_dir / "hashes.sha256").is_file():
        verify_hash_manifest(run_dir)
    execution_plan_path = run_dir / "execution-plan.json"
    if execution_plan_path.exists():
        if execution_plan_path.read_bytes() != plan_bytes:
            raise PilotStopError("run directory execution plan differs from frozen plan")
    else:
        _write_json(execution_plan_path, plan)
    run_manifest = _load_run_manifest(run_dir)
    protocol_manifest_bytes = protocol_module.PROTOCOL_PATH.read_bytes()
    protocol_manifest_sha = sha256(protocol_manifest_bytes)
    if run_manifest:
        if run_manifest.get("execution_plan_sha256") != plan_hash:
            raise PilotStopError("resume execution plan hash mismatch")
        if run_manifest.get("protocol_manifest_sha256") != protocol_manifest_sha:
            raise PilotStopError("resume protocol manifest hash mismatch")
        if run_manifest.get("runner_commit") != _git_head():
            raise PilotStopError("resume runner commit differs from the original execution")
        if run_manifest.get("openai_sdk_version") != openai.__version__:
            raise PilotStopError("resume OpenAI SDK version differs from the original execution")
    else:
        run_manifest = {
            "run_id": _run_id(),
            "protocol_version": protocol_module._load_protocol()["protocol_version"],
            "runner_commit": _git_head(),
            "openai_sdk_version": openai.__version__,
            "execution_plan_sha256": plan_hash,
            "prompt_sha256": protocol_module._load_protocol()["prompt_sha256"],
            "schema_sha256": protocol_module._load_protocol()["schema_sha256"],
            "protocol_manifest_sha256": protocol_manifest_sha,
            "request_parallelism": 1,
            "case_map_private": [],
            "created_at": utc_now(),
        }
    _write_json(manifest_path, run_manifest)
    write_hash_manifest(run_dir)
    verify_hash_manifest(run_dir)
    outcomes: list[dict[str, Any]] = []
    for case in plan["cases"]:
        verify_protocol_artifacts()
        current_hashes = verify_frozen_inputs()
        if current_hashes != frozen_hashes:
            raise PilotStopError("frozen RCA Input hashes changed during the pilot")
        if sha256(protocol_module.PROTOCOL_PATH.read_bytes()) != protocol_manifest_sha:
            raise PilotStopError("protocol manifest changed during the pilot")
        if build_execution_plan() != dict(plan):
            raise PilotStopError("execution plan changed during the pilot")
        case_id = case["opaque_case_id"]
        case_dir = _case_path(run_dir, case_id)
        request_path = case_dir / "request-metadata.json"
        prior_attempts: list[dict[str, Any]] = []
        if request_path.exists():
            existing = _read_json(request_path)
            status = existing["status"]
            if status not in ("NOT_STARTED", "TRANSPORT_FAILED"):
                if status == "IN_PROGRESS":
                    raise PilotStopError("case is IN_PROGRESS from an interrupted process; manual audit required")
                response_metadata_path = case_dir / "response-metadata.json"
                if response_metadata_path.exists():
                    existing["usage"] = _read_json(response_metadata_path).get("usage")
                outcomes.append(existing)
                continue
            if status == "TRANSPORT_FAILED" and not resume:
                raise PilotStopError("transport-failed case requires explicit resume")
            if status in ("TRANSPORT_FAILED", "NOT_STARTED"):
                prior_attempts = list(existing.get("attempts", []))
                if len(prior_attempts) >= MAX_TRANSPORT_ATTEMPTS:
                    raise PilotStopError("transport attempt budget exhausted for this logical call")
            if status == "TRANSPORT_FAILED":
                existing["status"] = "NOT_STARTED"
                _atomic_status(case_dir, "NOT_STARTED", existing)
                write_hash_manifest(run_dir)
                verify_hash_manifest(run_dir)
        else:
            existing = {"opaque_case_id": case_id, "ordinal": case["ordinal"],
                        "input_sha256": case["input_sha256"], "status": "NOT_STARTED"}
            _atomic_status(case_dir, "NOT_STARTED", existing)

        rca_input, relative_path = load_case_input(case)
        if case["input_sha256"] not in set(frozen_hashes.values()):
            raise PilotStopError("case input is absent from frozen hash set")
        request = build_openai_rca_request(rca_input)
        fingerprint = request_fingerprint(request, case["input_sha256"])
        record = _make_request_metadata(case_id, case["ordinal"], case["input_sha256"],
                                        relative_path, fingerprint)
        if prior_attempts:
            record["attempts"] = prior_attempts
        record["request_started_at"] = utc_now()
        _atomic_status(case_dir, "IN_PROGRESS", record)
        write_hash_manifest(run_dir)
        verify_hash_manifest(run_dir)
        outcome, attempts = _perform_logical_call(
            client, request, rca_input, sleeper=sleeper,
            max_attempts=MAX_TRANSPORT_ATTEMPTS - len(prior_attempts),
            first_attempt_number=len(prior_attempts) + 1)
        record["request_finished_at"] = utc_now()
        record["attempts"] = prior_attempts + attempts
        record["attempt_count"] = len(record["attempts"])
        record["transport_time_ms"] = round(sum(item.get("latency_ms", 0)
                                                   for item in record["attempts"]), 3)
        record["logical_total_latency_ms"] = outcome.get("logical_latency_ms")
        record["latency_ms"] = outcome.get("logical_latency_ms")
        record["latency_clean"] = len(record["attempts"]) == 1 and outcome.get("latency_clean", False)
        record["response_metadata"] = outcome.get("response_metadata")
        record["validation"] = outcome.get("validation")
        record["usage"] = (outcome.get("response_metadata") or {}).get("usage")
        if outcome.get("response_metadata") is not None:
            _write_json(case_dir / "response-metadata.json", outcome["response_metadata"])
        _write_json(case_dir / "validation.json", outcome.get("validation") or {"status": "NOT_AVAILABLE"})
        raw_output = outcome.get("raw_output")
        if raw_output is not None:
            (case_dir / "raw-output.txt").write_text(raw_output, encoding="utf-8")
        if outcome.get("prediction") is not None:
            _write_json(case_dir / "prediction.json", outcome["prediction"])
        _atomic_status(case_dir, outcome["status"], record)
        if relative_path not in {entry["input_path"] for entry in run_manifest["case_map_private"]}:
            run_manifest["case_map_private"].append({
                "opaque_case_id": case_id,
                "input_path": relative_path,
                "input_sha256": case["input_sha256"],
            })
        outcomes.append(record)
        _write_json(manifest_path, run_manifest)
        _write_json(run_dir / "summary.json", _summary(outcomes))
        write_hash_manifest(run_dir)
        verify_hash_manifest(run_dir)
        if record["status"] == "TRANSPORT_FAILED":
            raise PilotStopError(f"transport attempts exhausted for opaque case {case_id}; run stopped")
    run_manifest["case_map_private"] = sorted(run_manifest["case_map_private"],
                                                key=lambda item: item["opaque_case_id"])
    run_manifest["finished_at"] = utc_now()
    _write_json(manifest_path, run_manifest)
    _write_json(run_dir / "summary.json", _summary(outcomes))
    write_hash_manifest(run_dir)
    verify_completed_run(run_dir, plan)
    return _summary(outcomes)


def run_control_smoke(client: Any, output_root: Path | None = None) -> dict[str, Any]:
    output_root = output_root or CONTROL_SMOKE_ROOT
    synthetic_input = {
        "schema_version": "1.0",
        "evidence": [
            {"id": "evidence-001", "signal": "metric", "service": "service-alpha",
             "fact": "A synthetic service-level signal is above its usual range."},
            {"id": "evidence-002", "signal": "trace", "service": "service-beta",
             "fact": "A synthetic request has an extended downstream span."},
        ],
    }
    plan, plan_sha = write_or_verify_execution_plan()
    protocol_bytes = protocol_module.PROTOCOL_PATH.read_bytes()
    runner_commit = _git_head()
    output_root.mkdir(parents=True, exist_ok=True)
    protocol = json.loads(protocol_bytes)
    _write_json(output_root / "preflight.json", {
        "kind": "control_compatibility_smoke_preflight",
        "included_in_pilot_results": False,
        "protocol_version": protocol["protocol_version"],
        "runner_commit": runner_commit,
        "openai_sdk_version": openai.__version__,
        "execution_plan_sha256": plan_sha,
        "execution_plan_cases": len(plan["cases"]),
        "protocol_manifest_sha256": sha256(protocol_bytes),
        "prompt_sha256": protocol["prompt_sha256"],
        "schema_sha256": protocol["schema_sha256"],
        "control_input_sha256": sha256(canonical_json_bytes(synthetic_input)),
        "prepared_at": utc_now(),
    })
    request = build_openai_rca_request(synthetic_input)
    synthetic_input_hash = sha256(canonical_json_bytes(synthetic_input))
    fingerprint = request_fingerprint(request, synthetic_input_hash)
    start_wall = utc_now()
    classified, attempts = _perform_logical_call(client, request, synthetic_input)
    record = {
        "kind": "control_compatibility_smoke",
        "included_in_pilot_results": False,
        "protocol_version": protocol["protocol_version"],
        "runner_commit": runner_commit,
        "execution_plan_sha256": plan_sha,
        "input_sha256": synthetic_input_hash,
        "request_fingerprint": fingerprint,
        "started_at": start_wall,
        "finished_at": utc_now(),
        "latency_ms": classified.get("logical_latency_ms"),
        "attempts": attempts,
        "status": classified["status"],
        "response_metadata": classified.get("response_metadata"),
        "validation": classified.get("validation"),
        "prediction": classified.get("prediction"),
        "raw_output": classified.get("raw_output"),
    }
    _write_json(output_root / "control-smoke.json", record)
    _write_json(output_root / "manifest.json", {"included_in_pilot_results": False,
                                                   "status": record["status"],
                                                   "openai_sdk_version": openai.__version__,
                                                   "runner_commit": runner_commit,
                                                   "execution_plan_sha256": plan_sha,
                                                   "execution_plan_cases": len(plan["cases"])})
    write_hash_manifest(output_root)
    verify_hash_manifest(output_root)
    return {"status": record["status"], "included_in_pilot_results": False,
            "response_id": (record.get("response_metadata") or {}).get("response_id"),
            "usage": (record.get("response_metadata") or {}).get("usage"),
            "latency_ms": record["latency_ms"]}


def _git_head() -> str:
    import subprocess
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True)
    return result.stdout.strip()


def _verify_live_source_freeze(allow_runtime_outputs: bool,
                              allowed_runtime_roots: tuple[str, ...] | None = None) -> None:
    if allowed_runtime_roots is None:
        allowed_runtime_roots = (str(CONTROL_SMOKE_ROOT.relative_to(ROOT)) + "/",
                                 str(PILOT_ROOT.relative_to(ROOT)) + "/")
    import subprocess
    result = subprocess.run(["git", "status", "--porcelain=v1", "--untracked-files=all"],
                            cwd=ROOT, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=True)
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    unexpected = []
    for line in lines:
        path = line[3:].strip().strip('"')
        if (allow_runtime_outputs and line.startswith("?? ")
                and path.startswith(allowed_runtime_roots)):
            continue
        unexpected.append(path)
    if unexpected:
        raise PilotStopError("working tree contains changes outside permitted runtime output directories")


def _verify_control_smoke_commit() -> None:
    _verify_live_source_freeze(allow_runtime_outputs=True)
    manifest_path = CONTROL_SMOKE_ROOT / "manifest.json"
    if not manifest_path.is_file():
        raise PilotStopError("live compatibility control smoke is missing")
    verify_hash_manifest(CONTROL_SMOKE_ROOT)
    manifest = _read_json(manifest_path)
    attempts = manifest.get("attempts", [])
    successful = [item for item in attempts if item.get("status") == "COMPLETED"]
    if not successful or manifest.get("included_in_pilot_results") is not False:
        raise PilotStopError("live compatibility control smoke did not pass")
    selected = successful[-1]
    attempt_root = CONTROL_SMOKE_ROOT / selected["directory"]
    verify_hash_manifest(attempt_root)
    attempt_manifest = _read_json(attempt_root / "manifest.json")
    if attempt_manifest.get("runner_commit") != _git_head():
        raise PilotStopError("runner commit changed after control smoke")
    _, plan_hash = write_or_verify_execution_plan()
    if attempt_manifest.get("execution_plan_sha256") != plan_hash:
        raise PilotStopError("execution plan changed after control smoke")


def _next_control_smoke_attempt() -> tuple[Path, int]:
    CONTROL_SMOKE_ROOT.mkdir(parents=True, exist_ok=True)
    index_path = CONTROL_SMOKE_ROOT / "manifest.json"
    index = _read_json(index_path) if index_path.exists() else {"attempts": []}
    attempts = index.get("attempts", [])
    existing_dirs = sorted(CONTROL_SMOKE_ROOT.glob("attempt-[0-9][0-9][0-9]"))
    statuses = [item.get("status") for item in attempts]
    for attempt_dir in existing_dirs:
        attempt_manifest = attempt_dir / "manifest.json"
        if attempt_manifest.is_file():
            try:
                statuses.append(_read_json(attempt_manifest).get("status"))
            except Exception:
                statuses.append("UNKNOWN")
        outcome_file = attempt_dir / "control-smoke.json"
        if outcome_file.is_file():
            try:
                statuses.append(_read_json(outcome_file).get("status"))
            except Exception:
                statuses.append("UNKNOWN")
    if "COMPLETED" in statuses:
        raise PilotStopError("control smoke already passed; proceed to the frozen pilot run")
    if any(status in {"REFUSAL", "INCOMPLETE", "STRUCTURED_OUTPUT_INVALID", "SEMANTICALLY_INVALID"}
           for status in statuses):
        raise PilotStopError("control smoke has a final model outcome; do not repeat it")
    if any(status != "TRANSPORT_FAILED" for status in statuses):
        raise PilotStopError("control smoke attempt state is ambiguous; manual audit required")
    number = max([int(path.name.split("-")[-1]) for path in existing_dirs] + [0]) + 1
    return CONTROL_SMOKE_ROOT / f"attempt-{number:03d}", number


def _record_control_smoke_attempt(attempt_dir: Path, number: int, result: Mapping[str, Any]) -> None:
    attempts = []
    index_path = CONTROL_SMOKE_ROOT / "manifest.json"
    if index_path.exists():
        attempts = _read_json(index_path).get("attempts", [])
    attempts.append({"attempt": number, "directory": attempt_dir.name,
                     "status": result["status"], "included_in_pilot_results": False,
                     "runner_commit": _git_head()})
    _write_json(index_path, {"included_in_pilot_results": False, "attempts": attempts})
    write_hash_manifest(CONTROL_SMOKE_ROOT)
    verify_hash_manifest(CONTROL_SMOKE_ROOT)


def _create_client() -> OpenAI:
    require_openai_credential(ROOT)
    return OpenAI(base_url="https://api.openai.com/v1", max_retries=0)


def configure_runner(protocol_path: Path | str) -> dict[str, Any]:
    """Configure protocol-driven paths while preserving Luna defaults exactly."""
    global ARTIFACTS, PILOT_ROOT, CONTROL_SMOKE_ROOT
    selected = Path(protocol_path)
    protocol = configure_protocol(selected)
    ARTIFACTS = protocol_module.ARTIFACTS
    PILOT_ROOT = ROOT / protocol.get("run_output_root", "artifacts/experiment/llm/run-v1")
    CONTROL_SMOKE_ROOT = ROOT / protocol.get("control_output_root", "artifacts/experiment/llm/control-smoke")
    execution_module.PLAN_PATH = ROOT / protocol.get(
        "execution_plan_file", "artifacts/experiment/llm/execution-plan-v1.json")
    return protocol


def assert_no_secret_in_artifacts(paths: list[Path]) -> bool:
    """Scan outputs without ever rendering the secret or its location."""
    secret = os.environ.get("OPENAI_API_KEY")
    if not secret:
        raise CredentialPreflightError("secret scan requires credential availability")
    needle = secret.encode("utf-8")
    found = any(path.is_file() and needle in path.read_bytes()
                for root in paths if root.exists()
                for path in ([root] if root.is_file() else root.rglob("*")))
    if found:
        raise CredentialPreflightError("secret leakage detected in generated artifacts")
    return True


def _print_safe(label: str, **fields: Any) -> None:
    print(json.dumps({"event": label, **fields}, sort_keys=True, allow_nan=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path,
                        default=ROOT / "scripts/experiment/llm/resources/protocol-v1.json")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight")
    subparsers.add_parser("freeze-plan")
    subparsers.add_parser("control-smoke")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--run-dir", type=Path)
    run_parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        configure_runner(args.protocol)
        verify_env_git_safety(ROOT)
        if args.command == "preflight":
            require_openai_credential(ROOT)
            _print_safe("CREDENTIAL_PREFLIGHT_PASS", openai_api_key_available=True,
                        env_tracked=False, env_ignored=True)
            return 0
        verify_protocol_artifacts()
        if args.command == "freeze-plan":
            if not protocol_module.INPUT_HASHES_PATH.exists():
                protocol_module.freeze_current_inputs()
            verify_frozen_inputs()
            plan, digest = write_or_verify_execution_plan()
            _print_safe("EXECUTION_PLAN_FROZEN", cases=len(plan["cases"]), sha256=digest)
            return 0
        if args.command == "control-smoke":
            if CONTROL_SMOKE_ROOT.exists():
                _verify_live_source_freeze(allow_runtime_outputs=True,
                                           allowed_runtime_roots=(str(CONTROL_SMOKE_ROOT.relative_to(ROOT)) + "/",))
            else:
                _verify_live_source_freeze(allow_runtime_outputs=False)
            attempt_dir, attempt_number = _next_control_smoke_attempt()
            client = _create_client()
            outcome = run_control_smoke(client, attempt_dir)
            _record_control_smoke_attempt(attempt_dir, attempt_number, outcome)
            assert_no_secret_in_artifacts([attempt_dir])
            assert_no_secret_in_artifacts([CONTROL_SMOKE_ROOT])
            _print_safe("CONTROL_SMOKE_COMPLETE", **outcome)
            return 0 if outcome["status"] == "COMPLETED" else 2
        if args.command == "run":
            verify_frozen_inputs()
            _verify_control_smoke_commit()
            client = _create_client()
            plan, digest = write_or_verify_execution_plan()
            run_dir = args.run_dir or PILOT_ROOT
            summary = run_pilot(client, run_dir, plan, resume=args.resume)
            assert_no_secret_in_artifacts([run_dir])
            _print_safe("PILOT_COMPLETE", run_dir=run_dir.relative_to(ROOT).as_posix()
                        if run_dir.is_relative_to(ROOT) else "external-output",
                        plan_sha256=digest, **summary)
            return 0
    except Exception as exc:
        # Do not render SDK exception messages or any environment values.
        _print_safe("PILOT_STOP", exception_type=type(exc).__name__)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
