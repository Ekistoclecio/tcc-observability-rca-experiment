#!/usr/bin/env python3
"""Inventory native A/B telemetry for an auditable contract review."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sys
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from scripts.experiment.dataset.contract import common_traces  # noqa: E402
from scripts.experiment.dataset.contract import common_logs, common_metrics  # noqa: E402
from scripts.experiment.dataset.core import SUT_SERVICES  # noqa: E402


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def body_fields(body: Any) -> list[str]:
    if isinstance(body, dict):
        return sorted(str(key) for key in body)
    if isinstance(body, str) and body.lstrip().startswith("{"):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return sorted(str(key) for key in parsed)
        except json.JSONDecodeError:
            pass
    return []


def body_object(body: Any) -> dict[str, Any]:
    if isinstance(body, dict):
        return body
    if isinstance(body, str) and body.lstrip().startswith("{"):
        try:
            parsed = json.loads(body)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass
    return {}


def infer_metric_type(name: str) -> str:
    if name.endswith("_bucket"):
        return "histogram_bucket"
    if name.endswith(("_sum", "_count", "_total")):
        return "counter_component"
    return "gauge_or_unknown"


def metric_inventory(raw: dict[str, Any]) -> dict[str, Any]:
    data = raw.get("response", {}).get("data", {})
    metadata = raw.get("metadata", {})
    names: dict[tuple[str, str], dict[str, Any]] = {}
    sample_count = 0
    for series in data.get("result", []):
        labels = dict(series.get("metric", {}))
        name = str(labels.pop("__name__", ""))
        service = labels.get("service_name", labels.get("service.name", labels.get("service")))
        if service not in SUT_SERVICES:
            service = next((svc for svc in SUT_SERVICES if str(labels.get("pod", "")).startswith(svc + "-")), None)
        samples = series.get("values", [series.get("value", [])])
        sample_count += len(samples)
        key = (str(service or "unresolved"), name)
        row = names.setdefault(key, {"service": key[0], "metric_name": name, "metric_type": None,
            "source": {"scrape_job": labels.get("job"),
                       "instrumentation_scope": labels.get("otel_scope_name") or labels.get("instrumentation_scope_name")},
            "label_keys": set(), "series": 0, "samples": 0})
        row["label_keys"].update(labels)
        row["series"] += 1
        row["samples"] += len(samples)
        meta = metadata.get(name, [])
        if meta:
            row["metric_type"] = meta[0].get("type")
        if not row["metric_type"]:
            row["metric_type"] = infer_metric_type(name)
    rows = []
    for row in names.values():
        row["label_keys"] = sorted(row["label_keys"])
        rows.append(row)
    rows.sort(key=lambda item: (item["service"], item["metric_name"]))
    return {"native_metric_series": rows, "metric_names": sorted({x["metric_name"] for x in rows}),
            "series": sum(x["series"] for x in rows), "samples": sample_count}


def log_inventory(raw: dict[str, Any]) -> dict[str, Any]:
    by_service: dict[str, dict[str, Any]] = {}
    for stream in raw.get("response", {}).get("data", {}).get("result", []):
        labels = dict(stream.get("stream", {}))
        service = labels.get("service_name", labels.get("service.name", labels.get("service", "unresolved")))
        if service not in SUT_SERVICES:
            continue
        row = by_service.setdefault(service, {"service": service, "records": 0, "logger_sources": Counter(),
            "severity_values": Counter(), "field_names": Counter(), "records_with_trace_id": 0,
            "records_with_span_id": 0, "body_formats": Counter()})
        structured = dict(stream.get("structuredMetadata", {}))
        row["logger_sources"].update({key: 1 for key in labels if any(part in key.casefold() for part in ("logger", "scope", "source"))})
        for timestamp, body in stream.get("values", []):
            del timestamp
            row["records"] += 1
            fields = body_fields(body)
            body_data = body_object(body)
            row["field_names"].update(fields)
            merged_keys = {str(k).casefold().replace(".", "_").replace("-", "_") for k in (*labels, *structured, *fields)}
            row["records_with_trace_id"] += int(any(k in merged_keys for k in ("trace_id", "traceid", "x_tcc_trace_id")))
            row["records_with_span_id"] += int(any(k in merged_keys for k in ("span_id", "spanid", "x_tcc_span_id")))
            row["severity_values"].update([str(labels.get("severity") or structured.get("severity") or body_data.get("severity") or body_data.get("level") or "body-dependent")])
            for source_field in ("logger", "logger_name", "loggerName", "name", "source"):
                if body_data.get(source_field):
                    row["logger_sources"].update([str(body_data[source_field])])
            row["body_formats"].update(["json" if fields else "text"])
    result = []
    for service, row in sorted(by_service.items()):
        for key in ("logger_sources", "severity_values", "field_names", "body_formats"):
            row[key] = dict(sorted(row[key].items()))
        result.append(row)
    return {"services": result, "records": sum(x["records"] for x in result),
            "records_with_trace_id": sum(x["records_with_trace_id"] for x in result),
            "records_with_span_id": sum(x["records_with_span_id"] for x in result)}


def trace_inventory(raw: dict[str, Any]) -> dict[str, Any]:
    traces_by_id: dict[str, dict[str, Any]] = {}
    spans_by_service: dict[str, Counter[str]] = defaultdict(Counter)
    span_details: dict[str, dict[str, Any]] = {}
    for search in raw.get("search_results", []):
        for trace in search.get("response", {}).get("data", []):
            trace_id = trace.get("traceID")
            if not trace_id:
                continue
            current = traces_by_id.setdefault(trace_id, {"spans": set(), "services": set()})
            processes = trace.get("processes", {})
            for span in trace.get("spans", []):
                span_id = span.get("spanID")
                if not span_id:
                    continue
                service = processes.get(span.get("processID"), {}).get("serviceName", "unresolved")
                tags = {item.get("key"): item.get("value") for item in span.get("tags", []) if item.get("key")}
                operation = str(span.get("operationName") or "")
                kind = str(tags.get("span.kind") or "unspecified")
                scope = tags.get("otel.scope.name") or tags.get("instrumentation.scope.name") or tags.get("otel.library.name")
                key = f"{service}|{operation}|{kind}"
                current["spans"].add(span_id)
                current["services"].add(service)
                spans_by_service[service][key] += 1
                detail = span_details.setdefault(key, {"service": service, "span_name": operation, "span_kind": kind,
                    "instrumentation_scope": set(), "attribute_keys": set(), "parented_spans": 0})
                if scope:
                    detail["instrumentation_scope"].add(str(scope))
                detail["attribute_keys"].update(tags)
                detail["parented_spans"] += int(any(ref.get("refType") == "CHILD_OF" for ref in span.get("references", [])))
    full_spans = sum(len(value["spans"]) for value in traces_by_id.values())
    rows = []
    for key, value in sorted(span_details.items()):
        rows.append({**value, "instrumentation_scope": sorted(value["instrumentation_scope"]),
                     "attribute_keys": sorted(value["attribute_keys"])})
    common = common_traces(raw)
    logical = sorted({f"{span['service']}|{span['operation']}|{span['kind']}|{span['target']}"
                      for trace in common for span in trace["spans"]})
    return {"traces": len(traces_by_id), "spans": full_spans, "span_concepts": rows,
            "logical_contract_concepts": logical,
            "services": sorted({service for trace in traces_by_id.values() for service in trace["services"]})}


def metric_concepts(raw: dict[str, Any], scenario: str) -> list[str]:
    rows = common_metrics(raw, scenario, {"start": "1970-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z"})
    return sorted({"|".join(str(row.get(key) or "") for key in
        ("service", "concept", "operation", "kind", "target", "outcome")) for row in rows})


def log_event_concepts(raw: dict[str, Any], scenario: str) -> list[str]:
    rows = common_logs(raw, scenario)
    counts = Counter()
    for row in rows:
        body = body_object(row.get("body"))
        message = body.get("message") or body.get("msg") or row.get("body")
        message = re.sub(r"\s+", " ", str(message)).strip()
        message = re.sub(r"\b\d+(?:\.\d+)?\b", "#", message)
        counts[(row.get("service"), str(row.get("severity") or ""), message)] += 1
    return sorted("|".join((service, severity, message)) for service, severity, message in counts)


def build_inventory(results_root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    metrics_raw: dict[str, dict[str, Any]] = {}
    logs_raw: dict[str, dict[str, Any]] = {}
    for scenario in ("A", "B"):
        root = results_root / f"dataset{scenario}"
        metrics_raw[scenario] = read(root / "metrics.json")
        logs_raw[scenario] = read(root / "logs.json")
        result[scenario] = {
            "metrics": metric_inventory(metrics_raw[scenario]),
            "logs": log_inventory(logs_raw[scenario]),
            "traces": trace_inventory(read(root / "traces.json")),
            "metric_contract_concepts": metric_concepts(metrics_raw[scenario], scenario),
            "log_event_concepts": log_event_concepts(logs_raw[scenario], scenario),
        }
    result["comparison"] = {
        "metric_names_a_only": sorted(set(result["A"]["metrics"]["metric_names"]) - set(result["B"]["metrics"]["metric_names"])),
        "metric_names_b_only": sorted(set(result["B"]["metrics"]["metric_names"]) - set(result["A"]["metrics"]["metric_names"])),
        "logical_spans_a_only": sorted(set(result["A"]["traces"]["logical_contract_concepts"]) - set(result["B"]["traces"]["logical_contract_concepts"])),
        "logical_spans_b_only": sorted(set(result["B"]["traces"]["logical_contract_concepts"]) - set(result["A"]["traces"]["logical_contract_concepts"])),
        "metric_contract_a_only": sorted(set(result["A"]["metric_contract_concepts"]) - set(result["B"]["metric_contract_concepts"])),
        "metric_contract_b_only": sorted(set(result["B"]["metric_contract_concepts"]) - set(result["A"]["metric_contract_concepts"])),
        "log_event_b_only": sorted(set(result["B"]["log_event_concepts"]) - set(result["A"]["log_event_concepts"]))[:100],
        "log_event_b_only_count": len(set(result["B"]["log_event_concepts"]) - set(result["A"]["log_event_concepts"])),
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=REPO / "audit/results")
    parser.add_argument("--output", type=Path, default=REPO / "audit/observability-contract")
    parser.add_argument("--label", choices=("before", "after"), required=True)
    args = parser.parse_args()
    inventory = build_inventory(args.results)
    args.output.mkdir(parents=True, exist_ok=True)
    for signal in ("metrics", "logs", "traces"):
        payload = {scenario: inventory[scenario][signal] for scenario in ("A", "B")}
        (args.output / f"{args.label}-{signal}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    (args.output / f"{args.label}-comparison.json").write_text(
        json.dumps(inventory["comparison"], indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"comparison": inventory["comparison"], "output": str(args.output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
