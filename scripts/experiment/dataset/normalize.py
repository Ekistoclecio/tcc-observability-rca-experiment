from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import math
import re
from typing import Any

from .core import DatasetError, SUT_SERVICES, Window, iso_utc, validate_dataset_shape


_CORRELATION_FIELDS = {
    "traceid", "spanid", "traceparent", "tracestate", "b3", "b3traceid", "b3spanid",
    "xb3traceid", "xb3spanid", "ubertraceid", "uberspanid",
    "xdatadogtraceid", "xdatadogspanid", "xtcctraceid", "xtccspanid",
}
_LOGFMT_FIELD = re.compile(r"(?<![\w.])([A-Za-z][A-Za-z0-9_.-]*)\s*[=:]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)")


def _canonical_field(key: Any) -> str:
    return "".join(char for char in str(key).casefold() if char.isalnum())


def _correlation_field_paths(value: Any, path: str = "$") -> list[str]:
    """Find explicit trace/span context in structured fields or structured log text."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if _canonical_field(key) in _CORRELATION_FIELDS:
                found.append(child_path)
            found.extend(_correlation_field_paths(child, child_path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(_correlation_field_paths(child, f"{path}[{index}]"))
    elif isinstance(value, str):
        stripped = value.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                found.extend(_correlation_field_paths(parsed, path))
        for match in _LOGFMT_FIELD.finditer(value):
            if _canonical_field(match.group(1)) in _CORRELATION_FIELDS:
                found.append(f"{path}<{match.group(1)}>")
    return found


def _validate_scenario_a_logs(raw: dict[str, Any]) -> None:
    raw_responses = [("response", raw.get("response", {}))]
    raw_responses.extend((f"pages[{page_index}]", page) for page_index, page in enumerate(raw.get("pages", [])))
    for response_name, response in raw_responses:
        streams = response.get("data", {}).get("result", [])
        for index, stream in enumerate(streams):
            locations = _correlation_field_paths(stream, f"$.{response_name}.data.result[{index}]")
            if locations:
                raise DatasetError(
                    "Scenario A RAW contains explicit trace/span correlation fields; "
                    f"normalization rejected without modifying RAW: {', '.join(locations[:5])}"
                )


def _timestamp(value: Any, unit: str = "seconds") -> str:
    scale = {"seconds": 1_000_000_000, "milliseconds": 1_000_000,
             "microseconds": 1_000, "nanoseconds": 1}[unit]
    total_ns = int(Decimal(str(value)) * scale)
    seconds, fraction_ns = divmod(total_ns, 1_000_000_000)
    base = datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{fraction_ns:09d}Z"


def _service(labels: dict[str, Any]) -> str | None:
    for key in ("service_name", "service.name", "service"):
        value = labels.get(key)
        if value in SUT_SERVICES:
            return value
    return None


def _prom_metrics(raw: dict[str, Any]) -> list[dict[str, Any]]:
    response = raw.get("response", {}).get("data", {})
    result_type = response.get("resultType")
    output: list[dict[str, Any]] = []
    metadata = raw.get("metadata", {})
    series_list = response.get("result", [])
    rows: list[tuple[str, str, str, dict[str, Any], Any]] = []
    for series in series_list:
        labels = dict(series.get("metric", {}))
        name = labels.pop("__name__", "")
        service = _service(labels)
        if service is None:
            continue
        unit = None
        meta_rows = metadata.get(name, [])
        if meta_rows:
            unit = meta_rows[0].get("unit") or None
        samples = series.get("values", []) if result_type == "matrix" else [series.get("value", [])]
        for sample in samples:
            if not isinstance(sample, (list, tuple)) or len(sample) < 2:
                continue
            rows.append((service, name, json.dumps(labels, sort_keys=True, separators=(",", ":")), labels, (sample[0], sample[1], unit)))
    rows.sort(key=lambda row: (row[0], row[1], row[2], float(row[4][0])))
    for index, (service, name, _, labels, sample) in enumerate(rows, 1):
        ts, raw_value, unit = sample
        try:
            value: float | str = float(raw_value)
            if not math.isfinite(value):
                value = str(raw_value)
        except (ValueError, TypeError):
            value = str(raw_value)
        output.append({"evidence_id": f"metric-{index:06d}", "service": service, "native_name": name,
                       "unit": unit, "timestamp": _timestamp(ts), "value": value, "labels": labels})
    return output


def _loki_logs(raw: dict[str, Any], scenario: str) -> list[dict[str, Any]]:
    streams = raw.get("response", {}).get("data", {}).get("result", [])
    rows: list[dict[str, Any]] = []
    for stream in streams:
        labels = dict(stream.get("stream", {}))
        service = _service(labels)
        if service is None:
            continue
        structured = stream.get("structuredMetadata", {})
        for timestamp_ns, body in stream.get("values", []):
            rows.append({"timestamp": _timestamp(timestamp_ns, "nanoseconds"), "service": service,
                         "body": body, "labels": labels, "structured_metadata": structured})
    rows.sort(key=lambda row: (row["timestamp"], row["service"], str(row["body"]),
                               json.dumps(row["labels"], sort_keys=True, ensure_ascii=False)))
    output = []
    for index, row in enumerate(rows, 1):
        labels, structured = dict(row["labels"]), row["structured_metadata"]
        merged = {**labels, **structured}
        severity = merged.get("severity", merged.get("level"))
        trace_id = merged.get("trace_id", merged.get("traceID")) if scenario == "B" else None
        span_id = merged.get("span_id", merged.get("spanID")) if scenario == "B" else None
        attributes = {key: value for key, value in merged.items()
                      if key not in {"service_name", "service.name", "severity", "level", "trace_id", "traceID",
                                     "span_id", "spanID", "traceparent", "trace_parent", "x-tcc-trace-id",
                                     "x-tcc-span-id", "x_tcc_trace_id", "x_tcc_span_id"}}
        output.append({"evidence_id": f"log-{index:06d}", "timestamp": row["timestamp"], "service": row["service"],
                       "severity": severity, "body": row["body"], "trace_id": trace_id, "span_id": span_id,
                       "attributes": attributes, "labels": labels})
    return output


def _jaeger_traces(raw: dict[str, Any]) -> list[dict[str, Any]]:
    by_trace: dict[str, dict[str, Any]] = {}
    for search in raw.get("search_results", []):
        for trace in search.get("response", {}).get("data", []):
            trace_id = trace.get("traceID")
            if not trace_id:
                continue
            current = by_trace.setdefault(trace_id, {"trace_id": trace_id, "spans": {}, "processes": {}})
            current["processes"].update(trace.get("processes", {}))
            for span in trace.get("spans", []):
                span_id = span.get("spanID")
                if span_id:
                    current["spans"][span_id] = span
    ordered_traces = sorted(by_trace.values(), key=lambda item: (
        min((int(s.get("startTime", 0)) for s in item["spans"].values()), default=0), item["trace_id"]))
    result = []
    span_index = 0
    for trace_index, item in enumerate(ordered_traces, 1):
        spans = []
        ordered_spans = sorted(item["spans"].values(), key=lambda span: (int(span.get("startTime", 0)), span.get("spanID", "")))
        for span in ordered_spans:
            process = item["processes"].get(span.get("processID"), {})
            service = process.get("serviceName")
            if service not in SUT_SERVICES:
                continue
            refs = span.get("references", [])
            parent = next((ref.get("spanID") for ref in refs if ref.get("refType") == "CHILD_OF"), None)
            start_us = int(span.get("startTime", 0))
            duration_us = int(span.get("duration", 0))
            tags = {tag.get("key"): tag.get("value") for tag in span.get("tags", []) if tag.get("key") is not None}
            status = tags.get("otel.status_code") or tags.get("status.code") or tags.get("error")
            span_index += 1
            spans.append({"evidence_id": f"span-{span_index:06d}", "span_id": span.get("spanID"),
                          "parent_span_id": parent, "service": service, "operation": span.get("operationName"),
                          "start": _timestamp(start_us, "microseconds"), "duration_ms": duration_us / 1000.0,
                          "status": status, "attributes": tags,
                          "events": span.get("logs", [])})
        spans.sort(key=lambda item: (item["start"], item["span_id"] or ""))
        services = sorted({span["service"] for span in spans})
        if not spans:
            continue
        starts = [datetime.fromisoformat(span["start"].replace("Z", "+00:00")) for span in spans]
        start = min(starts)
        end = max(datetime.fromtimestamp(
            starts[i].timestamp() + float(spans[i]["duration_ms"]) / 1000, timezone.utc)
                  for i in range(len(spans)))
        result.append({"evidence_id": f"trace-{trace_index:06d}", "trace_id": item["trace_id"],
                       "start": iso_utc(start), "duration_ms": max(0.0, (end - start).total_seconds() * 1000),
                       "services": services, "spans": spans})
    return result


def normalize_scenario(run_id: str, scenario: str, window: Window, raw_signals: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if set(raw_signals) != {"metrics", "logs", "traces"}:
        raise DatasetError("normalization requires all three raw signals")
    if scenario == "A":
        _validate_scenario_a_logs(raw_signals["logs"])
    metrics = _prom_metrics(raw_signals["metrics"])
    logs = _loki_logs(raw_signals["logs"], scenario)
    traces = _jaeger_traces(raw_signals["traces"])
    dataset = {"schema_version": "1.0", "run_id": run_id, "scenario": scenario,
               "window": {"start": iso_utc(window.start), "end": iso_utc(window.end)},
               "services": list(SUT_SERVICES), "metrics": metrics, "logs": logs, "traces": traces,
               "summary": {"metrics_samples": len(metrics), "logs": len(logs), "traces": len(traces),
                           "spans": sum(len(trace["spans"]) for trace in traces)}}
    validate_dataset_shape(dataset)
    return dataset


def scenario_stats(dataset: dict[str, Any], raw_bytes: dict[str, int], normalized_bytes: int) -> dict[str, Any]:
    metric_services = sorted({item["service"] for item in dataset["metrics"]})
    metric_names = sorted({item["native_name"] for item in dataset["metrics"]})
    log_services = sorted({item["service"] for item in dataset["logs"]})
    traces = dataset["traces"]
    trace_services = sorted({service for trace in traces for service in trace["services"]})
    total_raw = sum(raw_bytes.values())
    return {
        "schema_version": "1.0", "run_id": dataset["run_id"], "scenario": dataset["scenario"],
        "metrics": {"metric_names": metric_names, "series": len({(item["service"], item["native_name"], json.dumps(item["labels"], sort_keys=True)) for item in dataset["metrics"]}),
                    "samples": len(dataset["metrics"]), "services": metric_services, "raw_bytes": raw_bytes["metrics"],
                    "normalized_bytes": len(json.dumps(dataset["metrics"], ensure_ascii=False, separators=(",", ":")).encode())},
        "logs": {"records": len(dataset["logs"]), "services": log_services,
                 "records_with_trace_id": sum(bool(item.get("trace_id")) for item in dataset["logs"]),
                 "records_with_span_id": sum(bool(item.get("span_id")) for item in dataset["logs"]),
                 "raw_bytes": raw_bytes["logs"],
                 "normalized_bytes": len(json.dumps(dataset["logs"], ensure_ascii=False, separators=(",", ":")).encode())},
        "traces": {"traces": len(traces), "spans": sum(len(trace["spans"]) for trace in traces),
                   "services": trace_services,
                   "error_spans": sum(1 for trace in traces for span in trace["spans"] if span.get("status") in ("ERROR", True, "true")),
                   "raw_bytes": raw_bytes["traces"],
                   "normalized_bytes": len(json.dumps(traces, ensure_ascii=False, separators=(",", ":")).encode())},
        "total_raw_bytes": total_raw, "total_normalized_bytes": normalized_bytes,
    }
