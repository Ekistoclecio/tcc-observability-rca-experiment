#!/usr/bin/env python3
"""Build a small, deterministic model-visible dataset from one immutable RAW bundle."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sys
from typing import Any


SERVICES = {
    "frontend", "checkoutservice", "cartservice", "productcatalogservice",
    "currencyservice", "paymentservice", "shippingservice", "emailservice",
    "recommendationservice", "adservice", "redis-cart",
}
APP_SERVICES = SERVICES - {"redis-cart"}
COUNTER_METRICS_A = {
    "tcc_conventional_requests_total": ("request_count",),
    "tcc_conventional_request_duration_seconds_count": ("duration_count",),
    "tcc_conventional_request_duration_seconds_sum": ("duration_sum",),
}
COUNTER_METRICS_B = {
    "otel_rpc_client_call_duration_seconds_count": ("request_count", "duration_count"),
    "otel_rpc_client_call_duration_seconds_sum": ("duration_sum",),
    "otel_rpc_server_call_duration_seconds_count": ("request_count", "duration_count"),
    "otel_http_server_request_duration_seconds_count": ("request_count", "duration_count"),
    "otel_rpc_server_call_duration_seconds_sum": ("duration_sum",),
    "otel_http_server_request_duration_seconds_sum": ("duration_sum",),
}
CPU_METRIC = "container_cpu_usage_seconds_total"
TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")
LOG_METADATA_KEYS = {
    "detectedlevel", "filename", "namespace", "stream", "scope_name", "scopename",
    "service_name", "servicename", "service_instance_id", "serviceinstanceid",
    "severity_number", "severitynumber", "severity_text", "severitytext",
    "telemetry_sdk_language", "telemetrysdklanguage", "telemetry_sdk_name",
    "telemetrysdkname", "telemetry_sdk_version", "telemetrysdkversion",
    "observed_timestamp", "observedtimestamp", "flags", "stack", "stacktrace",
    "exception_stacktrace", "exceptionstacktrace", "codefilepath", "codelinenumber",
}


class BuildError(RuntimeError):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read {path}: {exc}") from exc


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_bundle(root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    manifest_path = root / "manifest.json"
    hashes_path = root / "hashes.sha256"
    manifest = read_json(manifest_path)
    if not hashes_path.is_file():
        raise BuildError(f"RAW bundle has no hashes.sha256: {root}")
    expected: dict[str, str] = {}
    for line in hashes_path.read_text(encoding="utf-8").splitlines():
        try:
            digest, relative = line.split("  ", 1)
        except ValueError as exc:
            raise BuildError(f"invalid hash line in {hashes_path}: {line!r}") from exc
        expected[relative] = digest
    actual = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*") if item.is_file() and item != hashes_path
    }
    if actual != set(expected):
        raise BuildError(
            f"RAW inventory mismatch: unlisted={sorted(actual - set(expected))}; "
            f"missing={sorted(set(expected) - actual)}"
        )
    for relative, digest in expected.items():
        content = (root / relative).read_bytes()
        if sha256(content) != digest:
            raise BuildError(f"RAW hash mismatch: {root / relative}")
        if (root / relative).stat().st_mode & 0o222:
            raise BuildError(f"RAW file is writable: {root / relative}")
    for directory in (root, *(item for item in root.rglob("*") if item.is_dir())):
        if directory.stat().st_mode & 0o222:
            raise BuildError(f"RAW directory is writable: {directory}")
    return manifest, expected


def iso_time(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def source_ref(relative: str, digest: str, pointer: str) -> dict[str, str]:
    return {"raw_file": relative, "raw_sha256": digest, "json_pointer": pointer}


def evidence_id(kind: str, sources: list[dict[str, str]]) -> str:
    payload = json.dumps([kind, sorted(sources, key=lambda item: (item["raw_file"], item["json_pointer"]))],
                         sort_keys=True, separators=(",", ":"))
    return "ev_" + sha256(payload.encode("utf-8"))[:20]


def normalize_operation(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.removeprefix("grpc.").removeprefix("/")


def service_from_labels(labels: dict[str, Any]) -> str | None:
    for key in ("service_name", "exported_service_name", "exported_job"):
        service = labels.get(key)
        if service in SERVICES:
            return str(service)
    return None


def outcome_from_labels(labels: dict[str, Any]) -> str | None:
    for key in ("rpc_grpc_status_code", "rpc_response_status_code", "rpc_status_code",
                "rpc.grpc.status_code", "status_code",
                "http_response_status_code", "http.response.status_code", "result"):
        value = labels.get(key)
        if value is None:
            continue
        text = str(value).upper()
        if text in {"OK", "SUCCESS", "0"}:
            return "SUCCESS"
        if text in {"ERROR", "FAILURE", "FAILED"}:
            return "ERROR"
        if text.isdigit():
            number = int(text)
            if 100 <= number <= 599:
                return "SUCCESS" if 200 <= number < 400 else "ERROR"
            return "SUCCESS" if number == 0 else "ERROR"
        return "ERROR"
    return None


def operation_from_labels(labels: dict[str, Any]) -> str | None:
    method = labels.get("http_request_method") or labels.get("http_method")
    route = labels.get("http_route") or labels.get("url_path") or labels.get("route")
    if method and route:
        # Some HTTP server instrumentations expose gRPC method paths as routes.
        # Preserve the logical RPC operation instead of its HTTP transport form.
        normalized_route = normalize_operation(str(route))
        if normalized_route and normalized_route.startswith("hipstershop."):
            return normalized_route
        return normalize_operation(f"{method} {route}")
    value = (labels.get("operation") or labels.get("rpc_method") or labels.get("http_route")
             or labels.get("url_path") or labels.get("route"))
    return normalize_operation(value)


def target_from_labels(labels: dict[str, Any], operation: str | None) -> str | None:
    target = (labels.get("target") or labels.get("server_address") or labels.get("peer_service")
              or labels.get("net_peer_name") or labels.get("db_name"))
    if target:
        try:
            ipaddress.ip_address(str(target))
            target = None
        except ValueError:
            pass
    if target:
        return str(target)
    if operation and operation.startswith("redis-cart "):
        return "redis-cart"
    return None


def omit_health_metric(labels: dict[str, Any], operation: str | None) -> bool:
    path = str(labels.get("http_route") or labels.get("url_path") or operation or "")
    normalized = path.removeprefix("/").removeprefix("grpc.")
    return (normalized.startswith("health.v1.Health/") or path in {"/metrics", "/_healthz"}
            or operation in {"GET /metrics", "GET /_healthz"})


def counter_delta(values: list[list[Any]], window_start: float) -> tuple[float | None, str | None, float | None, float | None]:
    parsed: list[tuple[float, float]] = []
    for item in values:
        if not isinstance(item, list) or len(item) != 2:
            continue
        try:
            parsed.append((float(item[0]), float(item[1])))
        except (TypeError, ValueError):
            continue
    parsed.sort()
    if len(parsed) < 2:
        return None, "missing_baseline", parsed[0][0] if parsed else None, parsed[-1][0] if parsed else None
    first_time, first_value = parsed[0]
    last_time, last_value = parsed[-1]
    # With the collector's shared 1 s query grid, the first evaluation is the baseline.
    # If the series begins later, its pre-window value is unknown and must not become zero.
    if first_time > window_start + 1.25:
        return None, "missing_baseline", first_time, last_time
    if last_value < first_value:
        return None, "counter_reset", first_time, last_time
    return last_value - first_value, None, first_time, last_time


def metric_dimensions(labels: dict[str, Any]) -> tuple[str, str | None, str | None, str | None] | None:
    service = service_from_labels(labels)
    if service not in APP_SERVICES:
        return None
    operation = operation_from_labels(labels)
    if omit_health_metric(labels, operation):
        return None
    return service, operation, outcome_from_labels(labels), target_from_labels(labels, operation)


def build_metrics(root: Path, side: str, manifest: dict[str, Any], hash_map: dict[str, str],
                  provenance: dict[str, list[dict[str, str]]], excluded: dict[str, Any]) -> list[dict[str, Any]]:
    relative = f"dataset{side}/metrics.json"
    path = root / relative
    response = read_json(path)
    if response.get("status") not in (None, "success"):
        raise BuildError(f"Prometheus {side} query failed: {response.get('status')}")
    try:
        window_start = datetime.fromisoformat(manifest["window"]["start"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, TypeError) as exc:
        raise BuildError("RAW manifest has no valid window.start") from exc
    query_names = COUNTER_METRICS_A if side == "A" else COUNTER_METRICS_B
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    cpu_groups: dict[str, dict[str, Any]] = {}
    for row_index, row in enumerate(response.get("data", {}).get("result", [])):
        labels = row.get("metric", {})
        native_name = labels.get("__name__", "")
        raw_pointer = f"/data/result/{row_index}"
        raw_source = source_ref(relative, hash_map[relative], raw_pointer)
        if native_name == CPU_METRIC:
            container = labels.get("container")
            service = labels.get("service_name")
            if container not in {"server", "redis"} or service not in SERVICES:
                excluded["cpu_series"] = excluded.get("cpu_series", 0) + 1
                continue
            delta, reason, first, last = counter_delta(row.get("values", []), window_start)
            group = cpu_groups.setdefault(str(service), {"series": [], "sources": [], "reasons": []})
            group["series"].append({"delta": delta, "first": first, "last": last})
            group["sources"].append(raw_source)
            if reason:
                group["reasons"].append(reason)
            continue

        concepts = query_names.get(str(native_name))
        if concepts is None:
            excluded["metric_families"] = excluded.get("metric_families", {})
            excluded["metric_families"][str(native_name)] = excluded["metric_families"].get(str(native_name), 0) + 1
            continue
        dimensions = metric_dimensions(labels)
        if dimensions is None:
            excluded["health_metric_series"] = excluded.get("health_metric_series", 0) + 1
            continue
        delta, reason, first, last = counter_delta(row.get("values", []), window_start)
        for concept in concepts:
            key = (concept, *dimensions)
            group = groups.setdefault(key, {"series": [], "sources": [], "reasons": []})
            group["series"].append({"delta": delta, "first": first, "last": last})
            group["sources"].append(raw_source)
            if reason:
                group["reasons"].append(reason)

    output: list[dict[str, Any]] = []
    grouped_counters: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = defaultdict(dict)
    for key, group in groups.items():
        concept, service, operation, outcome, target = key
        if group["reasons"]:
            measurement = {"availability": "UNKNOWN", "value": None,
                           "reason": sorted(set(group["reasons"]))[0]}
        else:
            delta = sum(item["delta"] for item in group["series"] if item["delta"] is not None)
            if delta == 0:
                excluded["zero_counter_groups"] = excluded.get("zero_counter_groups", 0) + 1
                continue
            starts = [item["first"] for item in group["series"] if item["first"] is not None]
            ends = [item["last"] for item in group["series"] if item["last"] is not None]
            measurement = {"availability": "OBSERVED", "value": delta,
                           "interval_start": iso_time(min(starts)), "interval_end": iso_time(max(ends))}
        grouped_counters[(service, operation, outcome, target)][concept] = {
            **measurement, "sources": group["sources"]
        }

    for (service, operation, outcome, target), concepts in sorted(grouped_counters.items(), key=lambda item: str(item[0])):
        count = concepts.get("duration_count")
        total = concepts.get("duration_sum")
        if count is not None or total is not None:
            sources = (count or {}).get("sources", []) + (total or {}).get("sources", [])
            unknown = next((item for item in (count, total) if item and item["availability"] == "UNKNOWN"), None)
            if unknown or count is None or total is None:
                duration = {"availability": "UNKNOWN", "count": None, "sum_seconds": None,
                            "mean_seconds": None, "reason": (unknown or {}).get("reason", "missing_histogram_component")}
            else:
                count_value = count["value"]
                sum_value = total["value"]
                duration = {"availability": "OBSERVED", "count": count_value,
                            "sum_seconds": sum_value,
                            "mean_seconds": sum_value / count_value if count_value > 0 else None,
                            "interval_start": count["interval_start"], "interval_end": count["interval_end"]}
            record = {"signal": "metric", "metric": "request_duration", "service": service,
                      "value": duration, "unit": "seconds"}
            if operation is not None:
                record["operation"] = operation
            if outcome is not None:
                record["outcome"] = outcome
            if target is not None:
                record["target"] = target
            eid = evidence_id("metric", sources)
            record["id"] = eid
            provenance[eid] = sources
            output.append(record)

        request_count = concepts.get("request_count")
        if request_count is not None:
            record = {"signal": "metric", "metric": "request_count", "service": service,
                      "value": request_count["value"], "availability": request_count["availability"],
                      "unit": "requests"}
            if request_count["availability"] == "UNKNOWN":
                record["reason"] = request_count["reason"]
            else:
                record["interval_start"] = request_count["interval_start"]
                record["interval_end"] = request_count["interval_end"]
            if operation is not None:
                record["operation"] = operation
            if outcome is not None:
                record["outcome"] = outcome
            if target is not None:
                record["target"] = target
            eid = evidence_id("metric", request_count["sources"])
            record["id"] = eid
            provenance[eid] = request_count["sources"]
            output.append(record)

    for service, group in sorted(cpu_groups.items()):
        if group["reasons"]:
            value = None
            availability = "UNKNOWN"
            reason = sorted(set(group["reasons"]))[0]
            starts = ends = []
        else:
            seconds = sum(item["delta"] for item in group["series"] if item["delta"] is not None)
            first = min(item["first"] for item in group["series"] if item["first"] is not None)
            last = max(item["last"] for item in group["series"] if item["last"] is not None)
            value = seconds / (last - first) if last > first else 0.0
            availability = "OBSERVED"
            starts, ends = [first], [last]
            reason = None
        record = {"signal": "metric", "metric": "cpu_utilization", "service": service,
                  "value": value, "availability": availability, "unit": "cores"}
        if reason:
            record["reason"] = reason
        if starts and ends:
            record["interval_start"] = iso_time(min(starts))
            record["interval_end"] = iso_time(max(ends))
        eid = evidence_id("metric", group["sources"])
        record["id"] = eid
        provenance[eid] = group["sources"]
        output.append(record)
    return output


def extract_message(line: str) -> str:
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return redact_embedded_json(line)
    if isinstance(parsed, dict):
        for key in ("message", "msg", "body"):
            if isinstance(parsed.get(key), str):
                return redact_embedded_json(parsed[key])
        error = parsed.get("err") or parsed.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return redact_embedded_json(error["message"])
    return redact_embedded_json(line)


def is_stack_trace_line(line: str) -> bool:
    """Recognize standalone stack frames and stack delimiters without matching messages."""
    value = line.strip()
    if not value:
        return True
    lowered = value.lower()
    return (
        value.startswith(("at ", "--->", "--- End of inner exception stack trace"))
        or lowered.startswith(("file \"", "file '", "goroutine "))
        or bool(re.match(r"^in\s+/[^ ]+\.(?:cs|go|js|py|java|rb|php):\d+", value, re.IGNORECASE))
        or bool(re.match(r"^\s+---+ end of .*stack trace", value, re.IGNORECASE))
    )


def remove_stack_trace(message: str) -> str | None:
    """Keep the factual log message while excluding source frames and stack-only records."""
    retained: list[str] = []
    for line in message.splitlines():
        if is_stack_trace_line(line):
            continue
        # C# inner-exception details are commonly appended to the first line.
        # Retain the exception summary before the stack delimiter.
        line = re.split(r"\s+--->\s+", line, maxsplit=1)[0]
        if line.strip():
            retained.append(line.strip())
    return "\n".join(retained) or None


def redact_sensitive_values(value: Any) -> Any:
    """Redact credential/payment fields by key while preserving other log facts."""
    if isinstance(value, dict):
        result = {}
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            sensitive = normalized in {
                "password", "passwd", "secret", "token", "authorization", "apikey",
                "accesskey", "cardnumber", "creditcardnumber", "cvv", "cvc",
                "creditcardcvv", "securitycode", "ssn", "socialsecuritynumber",
            } or ("creditcard" in normalized and "expiration" in normalized)
            result[key] = "[REDACTED]" if sensitive else redact_sensitive_values(nested)
        return result
    if isinstance(value, list):
        return [redact_sensitive_values(item) for item in value]
    return value


def redact_embedded_json(message: str) -> str:
    """Redact sensitive keys in JSON objects embedded in a natural log message."""
    decoder = json.JSONDecoder()
    for start, character in enumerate(message):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(message[start:])
        except json.JSONDecodeError:
            continue
        redacted = redact_sensitive_values(value)
        if redacted == value:
            return message
        encoded = json.dumps(redacted, separators=(",", ":"), ensure_ascii=False)
        return message[:start] + encoded + message[start + end:]
    return message


def build_logs(root: Path, side: str, hash_map: dict[str, str],
               provenance: dict[str, list[dict[str, str]]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    log_dir = root / f"dataset{side}" / "logs"
    for path in sorted(log_dir.glob("*.json")):
        relative = path.relative_to(root).as_posix()
        response = read_json(path)
        if response.get("status") not in (None, "success"):
            raise BuildError(f"Loki {side} query failed: {path}")
        for stream_index, stream in enumerate(response.get("data", {}).get("result", [])):
            labels = stream.get("stream", {})
            service = service_from_labels(labels)
            if service not in APP_SERVICES:
                continue
            for value_index, pair in enumerate(stream.get("values", [])):
                if not isinstance(pair, list) or len(pair) != 2:
                    raise BuildError(f"invalid Loki entry at {relative} stream {stream_index}")
                timestamp, line = str(pair[0]), str(pair[1])
                try:
                    timestamp_iso = iso_time(int(timestamp) / 1_000_000_000)
                except ValueError as exc:
                    raise BuildError(f"invalid Loki timestamp {timestamp!r} in {relative}") from exc
                message = extract_message(line)
                message = remove_stack_trace(message)
                if message is None:
                    continue
                severity_raw = labels.get("severity_text") or labels.get("severity") or labels.get("detected_level")
                severity = normalize_severity(str(severity_raw or ""))
                record = {"signal": "log", "service": service, "timestamp": timestamp_iso,
                          "severity": severity, "message": message}
                trace_id = labels.get("trace_id") or labels.get("traceId")
                span_id = labels.get("span_id") or labels.get("spanId")
                if trace_id and TRACE_ID_RE.fullmatch(str(trace_id)):
                    record["trace_id"] = str(trace_id)
                if span_id and SPAN_ID_RE.fullmatch(str(span_id)):
                    record["span_id"] = str(span_id)
                attributes = {}
                for key, value in labels.items():
                    normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
                    if normalized_key in LOG_METADATA_KEYS or normalized_key in {
                        "traceid", "spanid", "operation", "rpcmethod", "outcome", "result",
                        "target", "serveraddress",
                    }:
                        continue
                    cleaned = (remove_stack_trace(str(value))
                               if isinstance(value, str) and normalized_key in {
                                   "detail", "errormessage", "exceptionmessage",
                               } else value)
                    if cleaned is None:
                        continue
                    attributes[key] = redact_sensitive_values(cleaned)
                if attributes:
                    record["attributes"] = attributes
                operation = labels.get("operation") or labels.get("rpc_method")
                if operation:
                    record["operation"] = normalize_operation(operation)
                outcome = labels.get("outcome") or labels.get("result")
                if outcome:
                    record["outcome"] = canonical_outcome(outcome)
                target = labels.get("target") or labels.get("server_address")
                if target:
                    record["target"] = str(target)
                source = source_ref(relative, hash_map[relative],
                                    f"/data/result/{stream_index}/values/{value_index}")
                eid = evidence_id("log", [source])
                record["id"] = eid
                provenance[eid] = [source]
                output.append(record)
    return output


def normalize_severity(value: str) -> str:
    value = value.upper()
    if value in {"WARN", "WARNING"}:
        return "WARN"
    if value in {"ERROR", "ERR"}:
        return "ERROR"
    if value in {"INFO", "INFORMATION"}:
        return "INFO"
    if value == "DEBUG":
        return "DEBUG"
    return "UNKNOWN"


def canonical_outcome(value: Any) -> str:
    text = str(value).upper()
    if text in {"OK", "SUCCESS", "0"}:
        return "SUCCESS"
    return "ERROR"


def span_outcome(tags: dict[str, Any]) -> str:
    if str(tags.get("error", "")).lower() == "true":
        return "ERROR"
    for key in ("otel.status_code", "status.code"):
        if str(tags.get(key, "")).upper() in {"ERROR", "2"}:
            return "ERROR"
        if str(tags.get(key, "")).upper() in {"OK", "SUCCESS", "1"}:
            return "SUCCESS"
    for key in ("rpc.grpc.status_code", "grpc.status_code", "result", "http.response.status_code", "http.status_code"):
        if key in tags:
            return canonical_outcome(tags[key])
    return "UNKNOWN"


def raw_only_span(operation: str, kind: str, tags: dict[str, Any]) -> str | None:
    path = str(tags.get("http.route") or tags.get("url.path") or "")
    if kind == "SERVER" and (path in {"/metrics", "/_healthz"}
                              or operation in {"GET /metrics", "GET /_healthz"}):
        return "metrics_scrape"
    if kind == "SERVER" and (operation.removeprefix("/").startswith("grpc.health.v1.Health/")
                              or path.removeprefix("/").startswith("grpc.health.v1.Health/")):
        return "health_check"
    url = str(tags.get("url.full") or "")
    scope = str(tags.get("otel.scope.name") or "")
    if kind == "CLIENT" and "java-http-client" in scope and "/api/v2/spans" in url:
        return "telemetry_export"
    return None


def build_traces(root: Path, side: str, hash_map: dict[str, str],
                 provenance: dict[str, list[dict[str, str]]], excluded: dict[str, Any]) -> list[dict[str, Any]]:
    records: dict[tuple[str, str], tuple[dict[str, Any], list[dict[str, str]]]] = {}
    trace_dir = root / f"dataset{side}" / "traces"
    for path in sorted(trace_dir.glob("*.json")):
        if path.name == "services.json":
            continue
        relative = path.relative_to(root).as_posix()
        response = read_json(path)
        if response.get("errors"):
            raise BuildError(f"Jaeger query returned errors: {relative}")
        for trace_index, trace in enumerate(response.get("data", [])):
            processes = trace.get("processes", {})
            for span_index, span in enumerate(trace.get("spans", [])):
                process = processes.get(span.get("processID"), {})
                service = process.get("serviceName") if isinstance(process, dict) else None
                if service not in APP_SERVICES:
                    excluded["spans_missing_or_unknown_service"] = excluded.get("spans_missing_or_unknown_service", 0) + 1
                    continue
                tags = {item.get("key"): item.get("value") for item in span.get("tags", [])
                        if isinstance(item, dict) and item.get("key")}
                kind = str(tags.get("span.kind", "")).upper()
                if kind not in {"SERVER", "CLIENT", "PRODUCER", "CONSUMER"}:
                    excluded["spans_without_diagnostic_boundary"] = excluded.get("spans_without_diagnostic_boundary", 0) + 1
                    continue
                operation_native = str(span.get("operationName") or "")
                reason = raw_only_span(operation_native, kind, tags)
                pointer = f"/data/{trace_index}/spans/{span_index}"
                source = source_ref(relative, hash_map[relative], pointer)
                if reason:
                    excluded["spans_raw_only"] = excluded.get("spans_raw_only", {})
                    excluded["spans_raw_only"][reason] = excluded["spans_raw_only"].get(reason, 0) + 1
                    continue
                trace_id = str(span.get("traceID") or trace.get("traceID") or "")
                span_id = str(span.get("spanID") or "")
                if not TRACE_ID_RE.fullmatch(trace_id) or not SPAN_ID_RE.fullmatch(span_id):
                    raise BuildError(f"invalid trace/span ID in {relative}{pointer}")
                start_time = float(span.get("startTime", 0)) / 1_000_000
                duration_ms = float(span.get("duration", 0)) / 1_000
                operation = normalize_operation(operation_native) or "unknown"
                target = (tags.get("server.address") or tags.get("peer.service") or tags.get("net.peer.name")
                          or tags.get("db.name"))
                if target:
                    try:
                        ipaddress.ip_address(str(target))
                        target = None
                    except ValueError:
                        pass
                if target is None and operation.startswith("redis-cart "):
                    target = "redis-cart"
                attributes: dict[str, Any] = {}
                canonical_attrs = {
                    "http.request.method": "http_method", "http.route": "http_route",
                    "url.path": "url_path", "http.response.status_code": "http_status",
                    "rpc.system": "rpc_system", "rpc.system.name": "rpc_system",
                    "rpc.service": "rpc_service", "rpc.method": "rpc_method",
                    "rpc.grpc.status_code": "rpc_status", "rpc.response.status_code": "rpc_status",
                    "db.system": "db_system", "db.system.name": "db_system",
                    "db.operation.name": "db_operation", "error.type": "error_type",
                }
                for source_key, output_key in canonical_attrs.items():
                    if source_key in tags and output_key not in attributes:
                        attributes[output_key] = tags[source_key]
                outcome = span_outcome(tags)
                record = {
                    "signal": "trace", "service": service, "operation": operation,
                    "kind": kind, "timestamp": iso_time(start_time),
                    "duration_ms": duration_ms, "outcome": outcome,
                    "trace_id": trace_id, "span_id": span_id,
                }
                if target:
                    record["target"] = str(target)
                if attributes:
                    record["attributes"] = attributes
                error_message = tags.get("grpc.error_message") or tags.get("error.message")
                if error_message:
                    record["error_message"] = str(error_message)
                references = span.get("references", [])
                parent = next((ref.get("spanID") for ref in references
                               if str(ref.get("refType", "")).lower() in {"childof", "followsfrom"}), None)
                if parent:
                    record["parent_span_id"] = str(parent)
                key = (trace_id, span_id)
                previous = records.get(key)
                if previous and previous[0] != record:
                    raise BuildError(f"conflicting RAW snapshots for span {trace_id}/{span_id}")
                if previous:
                    if source not in previous[1]:
                        previous[1].append(source)
                else:
                    records[key] = (record, [source])
    output = []
    for record, sources in records.values():
        eid = evidence_id("trace", sources)
        record["id"] = eid
        provenance[eid] = sorted(sources, key=lambda item: (item["raw_file"], item["json_pointer"]))
        output.append(record)
    return output


def build(args: argparse.Namespace) -> dict[str, Any]:
    root = args.raw.resolve()
    manifest, hash_map = verify_bundle(root)
    output_dir = args.output_dir
    if output_dir.exists() and any(output_dir.iterdir()):
        raise BuildError(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if not isinstance(manifest.get("window"), dict):
        raise BuildError("RAW manifest is missing its query window")

    provenance: dict[str, dict[str, list[dict[str, str]]]] = {"A": {}, "B": {}}
    exclusions: dict[str, dict[str, Any]] = {"A": {}, "B": {}}
    datasets: dict[str, dict[str, Any]] = {}
    for side in ("A", "B"):
        facts = []
        facts.extend(build_metrics(root, side, manifest, hash_map, provenance[side], exclusions[side]))
        facts.extend(build_logs(root, side, hash_map, provenance[side]))
        facts.extend(build_traces(root, side, hash_map, provenance[side], exclusions[side]))
        facts.sort(key=lambda item: (item["signal"], item.get("timestamp", item.get("interval_start", "")),
                                     item.get("service", ""), item.get("operation", ""), item["id"]))
        datasets[side] = {"schema_version": "1.0", "facts": facts}

    for side in ("A", "B"):
        path = output_dir / f"dataset-{side}.json"
        path.write_text(json.dumps(datasets[side], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    prov = {
        "format": "tcc-dataset-provenance-v1",
        "raw_bundle": {
            "manifest_sha256": sha256((root / "manifest.json").read_bytes()),
            "hash_list_sha256": sha256((root / "hashes.sha256").read_bytes()),
            "window": manifest["window"],
            "application_image_source_fingerprint": manifest.get("application_image_source_fingerprint"),
            "collector_git_commit": manifest.get("collector_git_commit"),
            "collector_script_sha256": manifest.get("collector_script_sha256"),
        },
        "evidence_sources": provenance,
        "excluded_raw": exclusions,
    }
    (output_dir / "provenance.json").write_text(json.dumps(prov, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output_manifest = {
        "format": "tcc-dataset-build-v1",
        "raw_bundle_manifest_sha256": prov["raw_bundle"]["manifest_sha256"],
        "builder_script_sha256": sha256(Path(__file__).read_bytes()),
        "outputs": {f"dataset-{side}.json": sha256((output_dir / f"dataset-{side}.json").read_bytes())
                    for side in ("A", "B")},
        "provenance_sha256": sha256((output_dir / "provenance.json").read_bytes()),
        "fact_counts": {side: len(data["facts"]) for side, data in datasets.items()},
    }
    (output_dir / "build-manifest.json").write_text(json.dumps(output_manifest, indent=2, sort_keys=True) + "\n",
                                                    encoding="utf-8")
    return {"status": "PASS", "output_dir": str(output_dir), "fact_counts": output_manifest["fact_counts"],
            "raw_manifest_sha256": prov["raw_bundle"]["manifest_sha256"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, required=True, help="immutable native RAW bundle")
    parser.add_argument("--output-dir", type=Path, required=True, help="new or empty output directory")
    args = parser.parse_args()
    try:
        result = build(args)
    except (BuildError, OSError, ValueError, KeyError) as exc:
        print(f"Dataset build failed closed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
