"""Independent audit of the Dataset -> model-visible request projection.

This deliberately does not import or call the production RCA request sanitizer.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from typing import Any


CONTRACT_PATH = Path(__file__).with_name("model-visible-projection-v1.json")
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
PROJECTION_VERSION = CONTRACT["version"]
ALLOWED = CONTRACT["allowed_removals"]
EXACT_METADATA = {"".join(c.lower() for c in key if c.isalnum()) for key in ALLOWED["metadata_exact_keys"]}
METADATA_PREFIXES = tuple(ALLOWED["metadata_prefixes"])
NATIVE_SPAN_ATTRIBUTES = {key.casefold() for key in ALLOWED["native_span_attribute_keys"]}
REQUIRED = CONTRACT["required_preserved_fields"]


def _normalized_key(key: Any) -> str:
    return "".join(c.lower() for c in str(key) if c.isalnum())


def _metadata_allowed(key: Any) -> bool:
    text = str(key).casefold()
    return _normalized_key(key) in EXACT_METADATA or text.startswith(METADATA_PREFIXES)


def _drop_reason(key: Any, *, in_span_attributes: bool) -> str | None:
    if _normalized_key(key) == ALLOWED["treatment_marker_normalized_key"]:
        return "treatment_marker"
    if _metadata_allowed(key):
        return "common_view_metadata"
    if in_span_attributes and str(key).casefold() in NATIVE_SPAN_ATTRIBUTES:
        return "native_span_attribute"
    return None


def _independent_projection(dataset: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Project telemetry using only the separately versioned contract policy."""
    output = deepcopy({signal: dataset[signal] for signal in ("metrics", "logs", "traces")})
    removed: Counter[str] = Counter()

    def walk(value: Any, *, is_span: bool = False, in_span_attributes: bool = False) -> Any:
        if isinstance(value, list):
            return [walk(item, is_span=is_span, in_span_attributes=in_span_attributes) for item in value]
        if not isinstance(value, dict):
            return value
        span = is_span or ("span_id" in value and "operation" in value and "service" in value)
        projected: dict[str, Any] = {}
        for key, child in value.items():
            if span and key == "events" and ALLOWED["empty_span_events_only"] and child == []:
                # The builder removes empty event lists. No evidence payload is lost.
                continue
            reason = _drop_reason(key, in_span_attributes=in_span_attributes)
            # Empty span attribute bags have no native values to preserve. For a
            # non-empty bag only explicitly contracted native keys may be removed.
            if span and key == "attributes" and isinstance(child, dict):
                attrs: dict[str, Any] = {}
                for attr, attr_value in child.items():
                    attr_reason = _drop_reason(attr, in_span_attributes=True)
                    if attr_reason:
                        removed[f"{attr_reason}:{attr}"] += 1
                    else:
                        attrs[attr] = walk(attr_value, is_span=True, in_span_attributes=False)
                projected[key] = attrs
                continue
            if reason:
                removed[f"{reason}:{key}"] += 1
                continue
            projected[key] = walk(child, is_span=span, in_span_attributes=False)
        return projected

    output = walk(output)
    return output, dict(sorted(removed.items()))


def _diff(expected: Any, actual: Any, path: str = "$") -> tuple[list[str], list[str], list[str]]:
    removals: list[str] = []
    additions: list[str] = []
    mutations: list[str] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for key in sorted(expected.keys() - actual.keys()):
            removals.append(f"{path}.{key}")
        for key in sorted(actual.keys() - expected.keys()):
            additions.append(f"{path}.{key}")
        for key in sorted(expected.keys() & actual.keys()):
            r, a, m = _diff(expected[key], actual[key], f"{path}.{key}")
            removals.extend(r); additions.extend(a); mutations.extend(m)
    elif isinstance(expected, list) and isinstance(actual, list):
        common = min(len(expected), len(actual))
        for index in range(common):
            r, a, m = _diff(expected[index], actual[index], f"{path}[{index}]")
            removals.extend(r); additions.extend(a); mutations.extend(m)
        removals.extend(f"{path}[{i}]" for i in range(common, len(expected)))
        additions.extend(f"{path}[{i}]" for i in range(common, len(actual)))
    elif type(expected) is not type(actual) or expected != actual:
        mutations.append(path)
    return removals, additions, mutations


def _required_field_report(dataset: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    checked: Counter[str] = Counter()
    groups = (("metrics", REQUIRED["metrics"]), ("logs", REQUIRED["logs"]),
              ("traces", REQUIRED["traces"]), ("spans", REQUIRED["spans"]))
    for signal, fields in groups:
        source_items = dataset.get(signal, []) if signal != "spans" else [
            span for trace in dataset.get("traces", []) for span in trace.get("spans", [])
        ]
        target_items = actual.get(signal, []) if signal != "spans" else [
            span for trace in actual.get("traces", []) for span in trace.get("spans", [])
        ]
        for index, source in enumerate(source_items):
            target = target_items[index] if index < len(target_items) else {}
            for field in fields:
                if field in source:
                    checked[f"{signal}.{field}"] += 1
                    if field not in target or (field not in {"attributes", "spans"} and target[field] != source[field]):
                        missing.append(f"{signal}[{index}].{field}")
    return {"checked": dict(sorted(checked.items())), "missing": missing}


def _item_counts(value: dict[str, Any]) -> dict[str, int]:
    traces = value.get("traces", [])
    return {"metrics": len(value.get("metrics", [])), "logs": len(value.get("logs", [])),
            "traces": len(traces), "spans": sum(len(trace.get("spans", [])) for trace in traces)}


def audit_projection(dataset: dict[str, Any], request: dict[str, Any], scenario: str,
                     request_manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    expected, removed = _independent_projection(dataset)
    actual = request.get("telemetry") if isinstance(request, dict) else None
    if not isinstance(actual, dict):
        actual = {}
    unexpected_removals, unexpected_additions, unexpected_mutations = _diff(expected, actual)
    required = _required_field_report(dataset, actual)
    source_ids = _collect_evidence_ids(dataset)
    actual_ids = _collect_evidence_ids(actual)
    source_log_correlation = [
        {key: item.get(key) for key in ("trace_id", "span_id") if item.get(key) not in (None, "")}
        for item in dataset.get("logs", [])
    ]
    actual_log_correlation = [
        {key: item.get(key) for key in ("trace_id", "span_id") if item.get(key) not in (None, "")}
        for item in actual.get("logs", [])
    ]
    synthetic_a_correlation = scenario == "A" and any(actual_log_correlation)
    manifest_removed = (request_manifest or {}).get("model_visible_sanitization", {}).get("removed_fields", {})
    manifest_version = (request_manifest or {}).get("model_visible_projection_version")
    # Existing immutable F4 artifacts predate the explicit version field. The
    # audited policy version is still recorded here; new artifacts must declare it.
    expected_manifest_counts = _production_count_view(removed)
    normalize_counts = lambda values: {key: int(count) for key, count in values.items() if int(count) != 0}
    manifest_counts_match = normalize_counts(manifest_removed) == normalize_counts(expected_manifest_counts)
    version_matches = manifest_version in (None, PROJECTION_VERSION)
    counts = {"dataset": _item_counts(dataset), "request": _item_counts(actual)}
    item_counts_match = counts["dataset"] == counts["request"]
    evidence_match = source_ids == actual_ids
    passed = not (unexpected_removals or unexpected_additions or unexpected_mutations or required["missing"]
                  or not evidence_match or not item_counts_match or not manifest_counts_match or not version_matches)
    passed = passed and not synthetic_a_correlation
    return {
        "projection_version": PROJECTION_VERSION,
        "manifest_projection_version": manifest_version,
        "manifest_projection_version_matches": version_matches,
        "dataset_items": counts["dataset"], "request_items": counts["request"],
        "item_counts_match": item_counts_match,
        "allowed_removals": removed,
        "allowed_removal_counts_match_manifest": manifest_counts_match,
        "required_fields": required,
        "unexpected_removals": unexpected_removals,
        "unexpected_additions": unexpected_additions,
        "unexpected_mutations": unexpected_mutations,
        "evidence_ids": {"dataset_count": len(source_ids), "request_count": len(actual_ids),
                         "dataset_only": sorted(source_ids - actual_ids), "request_only": sorted(actual_ids - source_ids),
                         "match": evidence_match},
        "log_correlation": {"source_nonempty_records": sum(bool(item) for item in source_log_correlation),
                            "request_nonempty_records": sum(bool(item) for item in actual_log_correlation),
                            "A_synthetic_correlation_detected": synthetic_a_correlation},
        "canonical_transformations_applied": CONTRACT["projection_transformations_from_dataset"],
        "passed": passed,
    }


def _production_count_view(removed: dict[str, int]) -> dict[str, int]:
    """Translate the independent audit's reason labels to manifest categories."""
    out: Counter[str] = Counter()
    for label, count in removed.items():
        reason, _, field = label.partition(":")
        if reason == "treatment_marker":
            out[field] += count
        elif reason in {"common_view_metadata", "native_span_attribute"}:
            category = "common_view_metadata" if reason == "common_view_metadata" else "span_attributes"
            out[f"{category}:{field}"] += count
    return dict(out)


def _collect_evidence_ids(value: dict[str, Any]) -> set[str]:
    found: set[str] = set()
    for signal in ("metrics", "logs", "traces"):
        for record in value.get(signal, []):
            if isinstance(record.get("evidence_id"), str):
                found.add(record["evidence_id"])
            if signal == "traces":
                found.update(span["evidence_id"] for span in record.get("spans", [])
                             if isinstance(span.get("evidence_id"), str))
    return found
