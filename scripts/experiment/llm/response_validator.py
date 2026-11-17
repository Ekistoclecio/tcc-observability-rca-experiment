"""Offline structural and semantic validation for the frozen RCA response contract."""

from __future__ import annotations

import math
from typing import Any, Mapping

from .protocol import extract_evidence_ids, verify_protocol_artifacts


class RCAResponseValidationError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _invalid(code: str, detail: str) -> None:
    raise RCAResponseValidationError(code, detail)


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _invalid("INVALID_LOCATION", f"{field} must be a non-empty string")
    return value


def validate_rca_response(response: Any, rca_input: Mapping[str, Any]) -> dict[str, Any]:
    """Return the same response when valid; never repair or reorder model output."""
    known_ids = extract_evidence_ids(rca_input)
    _, schema = verify_protocol_artifacts()
    hypothesis_array = schema["properties"]["hypotheses"]
    hypothesis_schema = hypothesis_array["items"]
    hypothesis_properties = hypothesis_schema["properties"]
    hypothesis_keys = set(hypothesis_properties)
    location_schema = hypothesis_properties["location"]
    location_properties = location_schema["properties"]
    location_keys = set(location_properties)
    fault_types = set(hypothesis_properties["fault_type"]["enum"])
    location_kinds = set(location_properties["kind"]["enum"])
    rank_values = hypothesis_properties["rank"]["enum"]
    expected_count = hypothesis_array["minItems"]
    if expected_count != hypothesis_array["maxItems"] or expected_count != len(rank_values):
        _invalid("INVALID_SCHEMA", "frozen schema rank/count contract is inconsistent")
    if not isinstance(response, dict):
        _invalid("INVALID_SCHEMA", "response must be an object")
    if set(response) != {"hypotheses"}:
        _invalid("INVALID_SCHEMA", "response must contain only hypotheses")
    hypotheses = response["hypotheses"]
    if not isinstance(hypotheses, list) or len(hypotheses) != expected_count:
        _invalid("INVALID_SCHEMA", "hypotheses must contain exactly three items")

    seen_diagnoses: set[tuple[Any, ...]] = set()
    confidences: list[float] = []
    for index, hypothesis in enumerate(hypotheses):
        if not isinstance(hypothesis, dict) or set(hypothesis) != hypothesis_keys:
            _invalid("INVALID_SCHEMA", f"hypotheses[{index}] has missing or additional fields")
        if type(hypothesis["rank"]) is not int or hypothesis["rank"] != rank_values[index]:
            _invalid("INVALID_RANK_ORDER", "ranks must appear exactly as [1, 2, 3]")
        if not isinstance(hypothesis["fault_type"], str) or hypothesis["fault_type"] not in fault_types:
            _invalid("INVALID_FAULT_TYPE", f"unsupported fault_type at rank {index + 1}")
        confidence = hypothesis["confidence"]
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence) or not 0 <= confidence <= 1):
            _invalid("INVALID_CONFIDENCE", f"confidence at rank {index + 1} must be finite in [0,1]")
        confidences.append(float(confidence))
        if not isinstance(hypothesis["explanation"], str) or not hypothesis["explanation"].strip():
            _invalid("INVALID_SCHEMA", f"explanation at rank {index + 1} must be a non-empty string")
        evidence_ids = hypothesis["evidence_ids"]
        if not isinstance(evidence_ids, list) or any(not isinstance(item, str) for item in evidence_ids):
            _invalid("INVALID_SCHEMA", f"evidence_ids at rank {index + 1} must be an array of strings")
        unknown = [item for item in evidence_ids if item not in known_ids]
        if unknown:
            _invalid("UNKNOWN_EVIDENCE_ID", f"rank {index + 1} references unknown IDs: {unknown}")

        location = hypothesis["location"]
        if not isinstance(location, dict) or set(location) != location_keys:
            _invalid("INVALID_LOCATION", f"location at rank {index + 1} has missing or additional fields")
        kind = location["kind"]
        if not isinstance(kind, str) or kind not in location_kinds:
            _invalid("INVALID_LOCATION", f"unsupported location kind at rank {index + 1}")
        values = {name: location[name] for name in location_keys - {"kind"}}
        if any(value is not None and not isinstance(value, str) for value in values.values()):
            _invalid("INVALID_LOCATION", f"location fields at rank {index + 1} must be strings or null")
        required_by_kind = {
            "service": {"service"},
            "service_edge": {"service", "peer_service"},
            "operation": {"service", "operation"},
            "datastore": {"datastore"},
        }[kind]
        for field in required_by_kind:
            _nonempty_string(location[field], field)
        for field in values.keys() - required_by_kind:
            if location[field] is not None:
                _invalid("INVALID_LOCATION", f"{field} must be null for location kind {kind}")
        diagnosis = (kind, location["service"], location["peer_service"],
                     location["operation"], location["datastore"], hypothesis["fault_type"])
        if diagnosis in seen_diagnoses:
            _invalid("DUPLICATE_HYPOTHESIS", f"duplicate location and fault_type at rank {index + 1}")
        seen_diagnoses.add(diagnosis)

    if not (confidences[0] >= confidences[1] >= confidences[2]):
        _invalid("INVALID_CONFIDENCE_ORDER", "confidence must be non-increasing by rank")
    return response
