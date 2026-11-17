#!/usr/bin/env python3
"""Validate semantic coverage directly over immutable native RAW bundles."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

from collect_raw import (trace_chunk_paths, validate_trace_artifact_references,
                         validate_trace_window_coverage)


SERVICES = (
    "frontend",
    "checkoutservice",
    "cartservice",
    "productcatalogservice",
    "currencyservice",
    "paymentservice",
    "shippingservice",
    "emailservice",
    "recommendationservice",
    "adservice",
)
DATASTORES = ("redis-cart",)
TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")
RFC3339_RE = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"[Tt](?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d+))?(?P<offset>[Zz]|[+-]\d{2}:\d{2})$"
)
NANOSECONDS_PER_SECOND = 1_000_000_000


class ValidationError(RuntimeError):
    pass


class Checks:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, name: str, passed: bool, observed: Any, expected: Any) -> None:
        self.items.append({
            "check": name,
            "status": "PASS" if passed else "FAIL",
            "observed": observed,
            "expected": expected,
        })

    @property
    def passed(self) -> bool:
        return all(item["status"] == "PASS" for item in self.items)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"cannot read JSON artifact {path}: {exc}") from exc


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rfc3339_epoch_ns(value: str) -> int:
    """Parse an RFC3339 instant exactly, without float or sub-ns rounding."""
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    match = RFC3339_RE.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid RFC3339 timestamp: {value!r}")
    fraction = match.group("fraction") or ""
    if len(fraction) > 9:
        raise ValueError(f"timestamp precision exceeds nanoseconds: {value!r}")

    raw_offset = match.group("offset")
    if raw_offset.lower() == "z":
        offset = timezone.utc
    else:
        sign = 1 if raw_offset[0] == "+" else -1
        offset_hour = int(raw_offset[1:3])
        offset_minute = int(raw_offset[4:6])
        if offset_hour > 23 or offset_minute > 59:
            raise ValueError(f"invalid RFC3339 UTC offset: {value!r}")
        offset = timezone(sign * timedelta(hours=offset_hour, minutes=offset_minute))

    try:
        local = datetime(
            int(match.group("year")), int(match.group("month")), int(match.group("day")),
            int(match.group("hour")), int(match.group("minute")), int(match.group("second")),
            tzinfo=offset,
        )
    except ValueError as exc:
        raise ValueError(f"invalid RFC3339 timestamp: {value!r}: {exc}") from exc

    utc_delta = local.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    whole_seconds = utc_delta.days * 86_400 + utc_delta.seconds
    fractional_ns = int(fraction.ljust(9, "0")) if fraction else 0
    return whole_seconds * NANOSECONDS_PER_SECOND + fractional_ns


def verify_raw_bundle(root: Path) -> dict[str, Any]:
    """Independently verify bundle hashes, inventory and read-only state."""
    manifest = read_json(root / "manifest.json")
    hashes_path = root / "hashes.sha256"
    if not hashes_path.is_file():
        raise ValidationError(f"missing hashes.sha256 in {root}")
    expected: dict[str, str] = {}
    for line in hashes_path.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        expected[relative] = digest
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != hashes_path
    }
    if actual != set(expected):
        raise ValidationError(
            f"RAW file inventory mismatch in {root}: "
            f"unlisted={sorted(actual - set(expected))}, missing={sorted(set(expected) - actual)}"
        )
    for relative, digest in expected.items():
        path = root / relative
        if sha256(path.read_bytes()) != digest:
            raise ValidationError(f"RAW hash mismatch: {path}")
        if path.stat().st_mode & 0o222:
            raise ValidationError(f"RAW file is writable: {path}")
    for directory in (root, *[item for item in root.rglob("*") if item.is_dir()]):
        if directory.stat().st_mode & 0o222:
            raise ValidationError(f"RAW directory is writable: {directory}")
    if manifest.get("jaeger_query_strategy"):
        window = manifest.get("window", {})
        try:
            validate_trace_window_coverage(
                manifest.get("trace_queries", []),
                int(window["jaeger_start_us"]), int(window["jaeger_end_us"]),
                manifest.get("trace_query_attempts", []),
            )
            validate_trace_artifact_references(root, manifest, expected)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            raise ValidationError(f"Jaeger trace query coverage is invalid: {exc}") from exc
    return {
        "root": str(root),
        "manifest_sha256": sha256((root / "manifest.json").read_bytes()),
        "hash_list_sha256": sha256(hashes_path.read_bytes()),
        "files_verified": len(expected),
        "status": "PASS",
    }


def normalize_operation(value: str) -> str:
    return value.removeprefix("grpc.").removeprefix("/")


def metric_rows(root: Path, scenario: str) -> list[dict[str, Any]]:
    response = read_json(root / f"dataset{scenario}" / "metrics.json")
    if response.get("status") not in (None, "success"):
        raise ValidationError(f"Prometheus {scenario} status is {response.get('status')}")
    return response.get("data", {}).get("result", [])


def metric_service(metric: dict[str, Any]) -> str | None:
    for label in ("service_name", "exported_service_name", "exported_job"):
        value = metric.get(label)
        if value in SERVICES:
            return value
    return None


def parse_metric_coverage(root: Path, scenario: str) -> dict[str, Any]:
    rows = metric_rows(root, scenario)
    names_by_service: dict[str, set[str]] = {service: set() for service in SERVICES}
    cpu_services: set[str] = set()
    cpu_sut: set[str] = set()
    for row in rows:
        metric = row.get("metric", {})
        name = metric.get("__name__", "")
        service = metric_service(metric)
        if service and name in {
            "tcc_conventional_requests_total",
            "tcc_conventional_request_duration_seconds_count",
            "tcc_conventional_request_duration_seconds_sum",
            "otel_rpc_server_call_duration_seconds_count",
            "otel_rpc_server_call_duration_seconds_sum",
            "otel_http_server_request_duration_seconds_count",
            "otel_http_server_request_duration_seconds_sum",
        }:
            names_by_service[service].add(name)
        if name == "container_cpu_usage_seconds_total" and metric.get("namespace") == "online-boutique":
            service_name = metric.get("service_name")
            if service_name in SERVICES:
                cpu_sut.add(service_name)
                cpu_services.add(service_name)
            elif service_name in DATASTORES:
                cpu_services.add(service_name)

    if scenario == "A":
        request_names = {"tcc_conventional_requests_total"}
        duration_names = {
            "tcc_conventional_request_duration_seconds_count",
            "tcc_conventional_request_duration_seconds_sum",
        }
    else:
        request_names = {
            "otel_rpc_server_call_duration_seconds_count",
            "otel_http_server_request_duration_seconds_count",
        }
        duration_names = {
            "otel_rpc_server_call_duration_seconds_sum",
            "otel_http_server_request_duration_seconds_sum",
        }
    request_services = sorted(
        service for service, names in names_by_service.items() if names & request_names
    )
    duration_services = sorted(
        service for service, names in names_by_service.items() if names & duration_names
    )
    return {
        "metric_series": len(rows),
        "request_count_services": request_services,
        "request_duration_services": duration_services,
        "cpu_sut_services": sorted(cpu_sut),
        "cpu_sut_and_datastore_services": sorted(cpu_services),
    }


def trace_spans(root: Path, scenario: str) -> tuple[dict[tuple[str, str], dict[str, Any]], list[str]]:
    spans: dict[tuple[str, str], dict[str, Any]] = {}
    conflicts: list[str] = []
    trace_dir = root / f"dataset{scenario}" / "traces"
    for path in trace_chunk_paths(root, scenario):
        response = read_json(path)
        if response.get("errors"):
            raise ValidationError(f"Jaeger response errors in {path}: {response['errors']}")
        for trace in response.get("data", []):
            processes = trace.get("processes", {})
            for span in trace.get("spans", []):
                process = processes.get(span.get("processID"), {})
                service = process.get("serviceName") if isinstance(process, dict) else None
                tags = {
                    item.get("key"): item.get("value")
                    for item in span.get("tags", [])
                    if isinstance(item, dict) and item.get("key")
                }
                key = (span.get("traceID", trace.get("traceID", "")), span.get("spanID", ""))
                normalized = {
                    "trace_id": key[0],
                    "span_id": key[1],
                    "service": service,
                    "operation": span.get("operationName", ""),
                    "kind": str(tags.get("span.kind", "")).lower(),
                    "tags": tags,
                    "start_time": span.get("startTime"),
                    "duration": span.get("duration"),
                }
                previous = spans.get(key)
                if previous is not None and previous != normalized:
                    conflicts.append(f"{key[0]}/{key[1]}")
                else:
                    spans[key] = normalized
    return spans, conflicts


def canonical_log_text(line: str) -> str:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return line
    pieces: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key, nested in item.items():
                if key.lower() in {"message", "body", "exception.message", "exception_message"} and isinstance(nested, str):
                    pieces.append(nested)
                visit(nested)
        elif isinstance(item, list):
            for nested in item:
                visit(nested)

    visit(value)
    return " ".join(pieces) if pieces else line


def severity_semantic(value: str) -> str:
    value = value.lower()
    if value in {"warn", "warning"}:
        return "WARN"
    if value == "error":
        return "ERROR"
    if value in {"info", "information"}:
        return "INFO"
    if value == "debug":
        return "DEBUG"
    return "UNKNOWN"


def log_records(root: Path, scenario: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / f"dataset{scenario}" / "logs").rglob("*.json")):
        response = read_json(path)
        if response.get("status") not in (None, "success"):
            raise ValidationError(f"Loki {scenario} status is {response.get('status')} in {path}")
        for stream in response.get("data", {}).get("result", []):
            labels = stream.get("stream", {})
            service = labels.get("service_name")
            for timestamp, line in stream.get("values", []):
                records.append({
                    "service": service,
                    "labels": labels,
                    "timestamp": str(timestamp),
                    "line": line,
                    "text": canonical_log_text(line),
                    "severity": str(labels.get("severity_text") or labels.get("severity")
                                     or labels.get("detected_level") or "").lower(),
                    "trace_id": labels.get("trace_id") or labels.get("traceId"),
                    "span_id": labels.get("span_id") or labels.get("spanId"),
                })
    return records


def contains_explicit_trace_fields(value: Any) -> bool:
    names = {"trace_id", "span_id", "traceid", "spanid", "otel.trace_id", "otel.span_id"}
    if isinstance(value, dict):
        return any(key.lower() in names or contains_explicit_trace_fields(nested) for key, nested in value.items())
    if isinstance(value, list):
        return any(contains_explicit_trace_fields(item) for item in value)
    return False


def span_services(spans: dict[tuple[str, str], dict[str, Any]], kind: str) -> set[str]:
    return {
        span["service"] for span in spans.values()
        if span["kind"] == kind and span["service"] in SERVICES
    }


def normal_span_signatures(spans: dict[tuple[str, str], dict[str, Any]], kind: str) -> set[tuple[str, str]]:
    return {
        (span["service"], normalize_operation(span["operation"]))
        for span in spans.values()
        if span["kind"] == kind and span["service"] in SERVICES
    }


def canonical_frontend_route(path: str) -> str:
    if path.startswith("/product/"):
        return "/product/{id}"
    if path.startswith("/product-meta/"):
        return "/product-meta/{ids}"
    if path.startswith("/static/"):
        return "/static/{asset}"
    return path


def is_raw_only_trace(span: dict[str, Any]) -> bool:
    tags = span["tags"]
    operation = span["operation"]
    path = tags.get("http.route") or tags.get("url.path")
    normalized_operation = normalize_operation(operation).lstrip("/")
    if span["kind"] == "server" and (
        operation == "GET /metrics" or path == "/metrics"
        or normalized_operation.startswith("grpc.health.v1.Health/")
        or normalized_operation.startswith("health.v1.Health/")
        or path == "/_healthz"
    ):
        return True
    return (
        span["kind"] == "client"
        and "java-http-client" in str(tags.get("otel.scope.name", ""))
        and "jaeger-a.observability-a" in str(tags.get("url.full", tags.get("server.address", "")))
    )


def semantic_operation_signature(span: dict[str, Any]) -> tuple[str, str] | None:
    """Compare logical application boundaries while preserving native RAW."""
    service = span["service"]
    operation = span["operation"]
    tags = span["tags"]
    if service not in SERVICES or is_raw_only_trace(span):
        return None
    if service == "frontend" and span["kind"] == "server":
        method = tags.get("http.request.method")
        path = tags.get("http.route") or tags.get("url.path")
        if method and path:
            return service, f"{method} {canonical_frontend_route(str(path))}"
        match = re.match(r"^(GET|POST|PUT|DELETE|HEAD)\s+(.+)$", operation)
        if match:
            return service, f"{match.group(1)} {canonical_frontend_route(match.group(2))}"
    if service == "cartservice" and span["kind"] == "client":
        database = tags.get("db.system.name", tags.get("db.system"))
        target = tags.get("server.address")
        command = tags.get("db.operation.name")
        if database == "redis" and target == "redis-cart" and command:
            return service, f"redis-cart {str(command).replace(' ', '/')}"
        match = re.match(r"^redis-cart\s+(.+)$", operation)
        if match:
            return service, f"redis-cart {match.group(1).replace(' ', '/')}"
    return service, normalize_operation(operation)


def is_error_span(span: dict[str, Any]) -> bool:
    tags = span["tags"]
    return (
        str(tags.get("error", "")).lower() == "true"
        or str(tags.get("otel.status_code", "")).upper() == "ERROR"
        or str(tags.get("status.code", "")).upper() in {"ERROR", "2"}
        or str(tags.get("rpc.grpc.status_code", "")) not in {"", "0", "OK"}
        or str(tags.get("result", "")).upper() not in {"", "OK", "SUCCESS", "200"}
    )


def verify_gt_and_window(root: Path, gt_path: Path, fault_type: str, location_kind: str, location: str) -> dict[str, Any]:
    gt = read_json(gt_path)
    root_location = gt.get("root_location", {})
    passed = (
        gt.get("status") == "RECOVERED"
        and gt.get("execution_eligible") is True
        and gt.get("fault_type") == fault_type
        and root_location.get("kind") == location_kind
        and root_location.get("value") == location
    )
    manifest = read_json(root / "manifest.json")
    window = manifest.get("window", {})
    start = gt.get("confirmed_start")
    end = gt.get("confirmed_end")
    comparisons: dict[str, Any] = {}
    window_matches = start is not None and end is not None
    for boundary, expected_raw in (("start", start), ("end", end)):
        actual_raw = window.get(boundary)
        comparison: dict[str, Any] = {
            "expected_raw": expected_raw,
            "actual_raw": actual_raw,
        }
        try:
            if expected_raw is None or actual_raw is None:
                raise ValueError("timestamp is missing")
            expected_ns = rfc3339_epoch_ns(expected_raw)
            actual_ns = rfc3339_epoch_ns(actual_raw)
            delta_ns = actual_ns - expected_ns
            comparison.update({
                "expected_epoch_ns": expected_ns,
                "actual_epoch_ns": actual_ns,
                "delta_ns": delta_ns,
                "status": "PASS" if delta_ns == 0 else "FAIL",
            })
            window_matches = window_matches and delta_ns == 0
        except ValueError as exc:
            comparison.update({"status": "FAIL", "error": str(exc)})
            window_matches = False
        comparisons[boundary] = comparison
    return {
        "ground_truth": {
            "path": str(gt_path),
            "status": gt.get("status"),
            "execution_eligible": gt.get("execution_eligible"),
            "fault_type": gt.get("fault_type"),
            "root_location": root_location,
            "status_check": "PASS" if passed else "FAIL",
        },
        "window": {
            "start": start,
            "end": end,
            "manifest_window": window,
            "comparisons": comparisons,
            "status": "PASS" if window_matches else "FAIL",
        },
    }


def validate_normal(root: Path, checks: Checks) -> dict[str, Any]:
    metrics = {scenario: parse_metric_coverage(root, scenario) for scenario in ("A", "B")}
    for scenario in ("A", "B"):
        checks.add(
            f"normal.metrics.{scenario}.request_count",
            set(metrics[scenario]["request_count_services"]) == set(SERVICES),
            metrics[scenario]["request_count_services"], list(SERVICES),
        )
        checks.add(
            f"normal.metrics.{scenario}.request_duration",
            set(metrics[scenario]["request_duration_services"]) == set(SERVICES),
            metrics[scenario]["request_duration_services"], list(SERVICES),
        )
        checks.add(
            f"normal.metrics.{scenario}.cpu",
            set(metrics[scenario]["cpu_sut_services"]) == set(SERVICES)
            and set(metrics[scenario]["cpu_sut_and_datastore_services"]) == set(SERVICES) | set(DATASTORES),
            metrics[scenario]["cpu_sut_and_datastore_services"], list(SERVICES) + list(DATASTORES),
        )

    spans: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    trace_conflicts: dict[str, list[str]] = {}
    for scenario in ("A", "B"):
        spans[scenario], trace_conflicts[scenario] = trace_spans(root, scenario)
        checks.add(
            f"normal.traces.{scenario}.duplicate_span_snapshots_consistent",
            not trace_conflicts[scenario], trace_conflicts[scenario], [],
        )
        checks.add(
            f"normal.traces.{scenario}.server_service_coverage",
            span_services(spans[scenario], "server") == set(SERVICES),
            sorted(span_services(spans[scenario], "server")), list(SERVICES),
        )
    signatures = {
        scenario: {
            "server": {signature for span in value.values()
                       if span["kind"] == "server"
                       and (signature := semantic_operation_signature(span)) is not None},
            "client": {signature for span in value.values()
                       if span["kind"] == "client"
                       and (signature := semantic_operation_signature(span)) is not None},
        }
        for scenario, value in spans.items()
    }
    server_a, server_b = signatures["A"]["server"], signatures["B"]["server"]
    client_a, client_b = signatures["A"]["client"], signatures["B"]["client"]
    checks.add("normal.traces.server_operation_signature_parity", server_a == server_b,
               {"only_A": sorted(server_a - server_b), "only_B": sorted(server_b - server_a)}, "same signatures")
    checks.add("normal.traces.client_operation_signature_parity", client_a == client_b,
               {"only_A": sorted(client_a - client_b), "only_B": sorted(client_b - client_a)}, "same signatures")
    payment_edge = any(service == "checkoutservice" and "PaymentService/Charge" in operation
                       for service, operation in client_a)
    checks.add("normal.traces.checkout_to_payment_client_boundary", payment_edge,
               sorted(x for x in client_a if x[0] == "checkoutservice" and "PaymentService/Charge" in x[1]),
               "checkoutservice client operation PaymentService/Charge")
    datastore_edge = all(any(
        span["service"] == "cartservice" and span["kind"] == "client"
        and ((span["tags"].get("server.address") == "redis-cart"
              and span["tags"].get("db.system.name", span["tags"].get("db.system")) == "redis")
             or span["operation"].startswith("redis-cart "))
        for span in spans[scenario].values()
    ) for scenario in ("A", "B"))
    checks.add("normal.traces.cart_to_redis_boundary", datastore_edge, datastore_edge,
               "A and B cartservice client span to redis-cart")

    logs = {scenario: log_records(root, scenario) for scenario in ("A", "B")}
    log_services = {scenario: sorted({record["service"] for record in records if record["service"]})
                    for scenario, records in logs.items()}
    checks.add("normal.logs.natural_service_coverage_parity",
               set(log_services["A"]) == set(log_services["B"]),
               log_services, "same naturally observed service set; no success log required")
    a_has_correlation = any(contains_explicit_trace_fields(record["line"]) or
                            contains_explicit_trace_fields(record["labels"]) for record in logs["A"])
    checks.add("normal.logs.scenario_a_has_no_explicit_trace_fields", not a_has_correlation,
               a_has_correlation, False)
    trace_index_b = set(spans["B"])
    correlated = 0
    invalid_correlations: list[dict[str, str]] = []
    for record in logs["B"]:
        trace_id, span_id = record["trace_id"], record["span_id"]
        if bool(trace_id) != bool(span_id):
            invalid_correlations.append({"service": str(record["service"]), "reason": "only one ID present"})
        elif trace_id and span_id:
            correlated += 1
            if not TRACE_ID_RE.fullmatch(str(trace_id)) or not SPAN_ID_RE.fullmatch(str(span_id)):
                invalid_correlations.append({"service": str(record["service"]), "reason": "invalid ID format"})
            elif (str(trace_id), str(span_id)) not in trace_index_b:
                invalid_correlations.append({"service": str(record["service"]), "reason": "ID pair absent from Jaeger B"})
    checks.add("normal.logs.scenario_b_correlation_matches_real_spans",
               correlated > 0 and not invalid_correlations,
               {"correlated_records": correlated, "invalid": invalid_correlations},
               "correlated IDs identify real B trace spans; uncorrelated logs remain allowed")
    signatures = Counter((record["service"], record["timestamp"], record["line"]) for record in logs["B"])
    duplicate_signatures = [
        {"service": service, "timestamp": timestamp, "copies": count}
        for (service, timestamp, _line), count in signatures.items() if count > 1
    ]
    checks.add("normal.logs.scenario_b_exact_duplicates", not duplicate_signatures,
               duplicate_signatures, [])
    return {
        "metrics": metrics,
        "trace_counts_unique": {scenario: len(value) for scenario, value in spans.items()},
        "trace_conflicts": trace_conflicts,
        "span_signatures": {
            "server": {"A": len(server_a), "B": len(server_b), "parity": server_a == server_b},
            "client": {"A": len(client_a), "B": len(client_b), "parity": client_a == client_b},
        },
        "raw_only_trace_signatures_b": sorted({
            (span["service"], span["operation"])
            for span in spans["B"].values() if is_raw_only_trace(span)
        }),
        "logs": {
            "record_counts": {scenario: len(value) for scenario, value in logs.items()},
            "natural_service_coverage": log_services,
            "scenario_b_correlated_records": correlated,
            "scenario_b_exact_duplicates": duplicate_signatures,
        },
    }


def validate_f4(root: Path, gt_path: Path, checks: Checks) -> dict[str, Any]:
    control = verify_gt_and_window(root, gt_path, "application_exception", "operation", "paymentservice/Charge")
    checks.add("F4.ground_truth_independent_expected_location", control["ground_truth"]["status_check"] == "PASS",
               control["ground_truth"], "application_exception at paymentservice/Charge, recovered and eligible")
    checks.add("F4.collection_window_matches_control_plane", control["window"]["status"] == "PASS",
               control["window"], "RAW start/end equal confirmed_start/confirmed_end")

    scenario: dict[str, Any] = {}
    error_spans: dict[str, list[dict[str, Any]]] = {}
    for side in ("A", "B"):
        logs = log_records(root, side)
        payment_logs = [record for record in logs if record["service"] == "paymentservice"
                        and "payment charge failed" in record["text"].lower()]
        spans, conflicts = trace_spans(root, side)
        selected = [span for span in spans.values() if span["service"] == "paymentservice"
                    and "PaymentService/Charge" in span["operation"] and span["kind"] == "server"
                    and is_error_span(span)]
        error_spans[side] = selected
        if side == "A":
            no_ids = not any(contains_explicit_trace_fields(record["line"]) or
                             contains_explicit_trace_fields(record["labels"]) for record in payment_logs)
            ids_match = True
        else:
            no_ids = True
            ids_match = bool(payment_logs) and all(
                record["trace_id"] and record["span_id"]
                and any(span["trace_id"] == record["trace_id"] and span["span_id"] == record["span_id"]
                        for span in selected)
                for record in payment_logs
            )
        severity = [record["severity"] for record in payment_logs]
        equivalent_severity = bool(severity) and all(
            severity_semantic(value) == "ERROR" for value in severity
        )
        checks.add(f"F4.{side}.payment_error_log_present", bool(payment_logs),
                   {"count": len(payment_logs), "severity": severity}, "Payment charge failed application log")
        checks.add(f"F4.{side}.payment_charge_error_server_span", bool(selected) and not conflicts,
                   [{"operation": span["operation"], "error": True} for span in selected],
                   "paymentservice server Charge span with error outcome")
        if side == "A":
            checks.add("F4.A.log_has_no_explicit_trace_correlation", no_ids, no_ids, True)
        else:
            checks.add("F4.B.log_correlation_matches_error_span", ids_match, ids_match,
                       "OTLP trace_id/span_id match paymentservice Charge error span")
        scenario[side] = {"error_log_count": len(payment_logs), "severity": severity,
                          "severity_semantic": sorted({severity_semantic(value) for value in severity}),
                          "explicit_trace_correlation_present": any(r["trace_id"] or r["span_id"] for r in payment_logs),
                          "error_span_count": len(selected), "log_span_correlation": ids_match,
                          "severity_semantics_valid": equivalent_severity}
    checks.add("F4.A_B.same_error_event_and_severity",
               all(scenario[x]["error_log_count"] > 0 and scenario[x]["severity_semantics_valid"] for x in ("A", "B"))
               and scenario["A"]["severity_semantic"] == scenario["B"]["severity_semantic"],
               {side: {"count": v["error_log_count"], "severity": v["severity_semantic"]}
                for side, v in scenario.items()}, "same Payment charge failed error/warning fact")
    return {**control, "scenarios": scenario}


def validate_f5(root: Path, gt_path: Path, checks: Checks) -> dict[str, Any]:
    control = verify_gt_and_window(root, gt_path, "datastore_unavailable", "datastore", "redis-cart")
    checks.add("F5.ground_truth_independent_expected_location", control["ground_truth"]["status_check"] == "PASS",
               control["ground_truth"], "datastore_unavailable at redis-cart, recovered and eligible")
    checks.add("F5.collection_window_matches_control_plane", control["window"]["status"] == "PASS",
               control["window"], "RAW start/end equal confirmed_start/confirmed_end")

    scenario: dict[str, Any] = {}
    for side in ("A", "B"):
        spans, conflicts = trace_spans(root, side)
        selected = [span for span in spans.values() if span["service"] == "cartservice"
                    and span["kind"] == "client" and is_error_span(span)
                    and ((span["tags"].get("server.address") == "redis-cart"
                          and span["tags"].get("db.system.name", span["tags"].get("db.system")) == "redis")
                         or span["operation"].startswith("redis-cart "))]
        checks.add(f"F5.{side}.cart_to_redis_error_span", bool(selected) and not conflicts,
                   [{"operation": span["operation"], "target": span["tags"].get("server.address"),
                     "error_type": span["tags"].get("error.type")} for span in selected],
                   "cartservice CLIENT/DATASTORE span to redis-cart with error outcome")
        scenario[side] = {"error_span_count": len(selected),
                          "operations": sorted({span["operation"] for span in selected})}
    return {**control, "scenarios": scenario}


def validate(args: argparse.Namespace) -> dict[str, Any]:
    checks = Checks()
    raw_roots = {"normal": args.normal_raw, "F4": args.f4_raw, "F5": args.f5_raw}
    bundles = {name: verify_raw_bundle(root) for name, root in raw_roots.items()}
    source_manifests = {name: read_json(root / "manifest.json") for name, root in raw_roots.items()}
    normal = validate_normal(args.normal_raw, checks)
    f4 = validate_f4(args.f4_raw, args.f4_ground_truth, checks)
    f5 = validate_f5(args.f5_raw, args.f5_ground_truth, checks)
    failed = [item for item in checks.items if item["status"] != "PASS"]
    return {
        "format": "tcc-experiment-v2-raw-semantic-validation-v1",
        "bundle_provenance": {
            name: {
                "application_image_source_fingerprint": manifest.get("application_image_source_fingerprint"),
                "collector_git_commit": manifest.get("collector_git_commit"),
                "collector_script_sha256": manifest.get("collector_script_sha256"),
                "window": manifest.get("window"),
            }
            for name, manifest in source_manifests.items()
        },
        "normal_raw": str(args.normal_raw),
        "f4_raw": str(args.f4_raw),
        "f5_raw": str(args.f5_raw),
        "bundles": bundles,
        "normal_operation": normal,
        "F4": f4,
        "F5": f5,
        "checks": checks.items,
        "check_count": len(checks.items),
        "failed_checks": failed,
        "status": "PASS" if checks.passed else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--normal-raw", type=Path, required=True)
    parser.add_argument("--f4-raw", type=Path, required=True)
    parser.add_argument("--f4-ground-truth", type=Path, required=True)
    parser.add_argument("--f5-raw", type=Path, required=True)
    parser.add_argument("--f5-ground-truth", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = validate(args)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (ValidationError, OSError) as exc:
        print(f"RAW semantic validation failed closed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"status": report["status"], "check_count": report["check_count"],
                      "failed_checks": report["failed_checks"], "output": str(args.output)}, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
