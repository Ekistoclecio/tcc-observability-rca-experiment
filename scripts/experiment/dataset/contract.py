from __future__ import annotations

"""Common, fault-independent RCA telemetry view for scenarios A and B."""

from collections import defaultdict
from datetime import datetime, timezone
import json
import math
import re
from typing import Any

from .core import DatasetError, SUT_SERVICES


CONTRACT_VERSION = "1.0"
_RPC_METHODS = {
    "AdService/GetAds", "CartService/AddItem", "CartService/EmptyCart", "CartService/GetCart",
    "CheckoutService/PlaceOrder", "CurrencyService/Convert", "CurrencyService/GetSupportedCurrencies",
    "EmailService/SendOrderConfirmation", "PaymentService/Charge", "ProductCatalogService/GetProduct",
    "ProductCatalogService/ListProducts", "RecommendationService/ListRecommendations",
    "ShippingService/GetQuote", "ShippingService/ShipOrder",
}
_HTTP_ROUTES = {"/", "/product/:id", "/product-meta/:ids", "/cart", "/cart/checkout", "/cart/empty",
                "/setCurrency", "/logout", "/assistant", "/static/*", "/robots.txt", "/bot"}
_RPC_TARGETS = {
    "AdService": "adservice", "CartService": "cartservice", "CheckoutService": "checkoutservice",
    "CurrencyService": "currencyservice", "EmailService": "emailservice", "PaymentService": "paymentservice",
    "ProductCatalogService": "productcatalogservice", "RecommendationService": "recommendationservice",
    "ShippingService": "shippingservice",
}
_RESOURCE_NOISE = {
    "telemetry_sdk_name", "telemetry_sdk_version", "telemetry_sdk_language", "instrumentation_scope",
    "instrumentation_scope_version", "otel_scope_name", "otel_scope_version", "deployment_environment_name",
    "service_version", "service_instance_id", "exported_instance", "exported_job", "instance", "job",
    "process_pid", "process_command", "process_command_args", "process_command_line",
    "process_executable_name", "process_executable_path", "process_runtime_name",
    "process_runtime_version", "process_runtime_description", "host_name", "host_arch",
    "container_id", "pod_uid", "filename", "namespace", "stream", "telemetry_scenario",
    "exception_stacktrace", "exception_stack_trace", "stacktrace",
    "timestamp", "time", "instant", "pid", "hostname", "name", "thread", "threadid",
    "threadpriority", "loggername", "loggerfqcn", "endofbatch",
}
_NON_DIAGNOSTIC_LOG_FIELDS = {
    "userid", "user_id", "session", "session_id", "cartid", "cart_id", "productid", "product_id",
    "creditcardnumber", "credit_card_number", "creditcardcvv", "credit_card_cvv", "cvv",
}
_GRPC_STATUS_NAMES = {
    "0": "SUCCESS", "ok": "SUCCESS", "1": "ERROR", "cancelled": "ERROR",
    "2": "ERROR", "unknown": "ERROR", "3": "ERROR", "invalid_argument": "ERROR",
    "4": "ERROR", "deadline_exceeded": "ERROR", "5": "ERROR", "not_found": "ERROR",
    "6": "ERROR", "already_exists": "ERROR", "7": "ERROR", "permission_denied": "ERROR",
    "8": "ERROR", "resource_exhausted": "ERROR", "9": "ERROR", "failed_precondition": "ERROR",
    "10": "ERROR", "aborted": "ERROR", "11": "ERROR", "out_of_range": "ERROR",
    "12": "ERROR", "unimplemented": "ERROR", "13": "ERROR", "internal": "ERROR",
    "14": "ERROR", "unavailable": "ERROR", "15": "ERROR", "data_loss": "ERROR",
    "16": "ERROR", "unauthenticated": "ERROR",
}
_LOG_METADATA_KEYS = {
    "scope_name", "scope_version", "instrumentation_scope", "instrumentation_scope_version",
    "os_description", "os_type", "os_version", "telemetry_distro_name", "telemetry_distro_version",
    "code_file_path", "code_function_name", "code_line_number", "process_pid", "process_command",
    "process_command_line", "process_runtime_name", "process_runtime_version", "container_id", "pod_uid",
    "exporter", "exporter_name", "collector", "collector_name", "exception_type", "error_type",
    "exception_stacktrace", "stacktrace",
}
_SHARED_APPLICATION_EVENTS = {
    "application_error", "application_warning", "downstream_operation_failed",
    "downstream_call_failed", "datastore_operation_failed", "payment_charge_failed",
}


def _shared_event_value(attributes: dict[str, Any], *names: str) -> Any:
    normalized = {_normalized_key(key): value for key, value in attributes.items()}
    for name in names:
        key = _normalized_key(name)
        if key in normalized:
            return normalized[key]
    return None
_STACK_FRAME_RE = re.compile(r"^\s*(?:at\s+[\w.$<>`+/-]+\s*\(|---\s+End of|Caused by:|Traceback \(most recent call last\):)")
_RUNTIME_NOISE_RE = re.compile(
    r"^(?:env platform is either empty or invalid|\d+:[A-Z]\s|redis is starting|"
    r"DeprecationWarning:|RuntimeWarning:|ExperimentalWarning:|.*(?:health|ready|startup).*(?:check|probe).*|"
    r".*checking\s+[\w.-]+\s+(?:service\s+)?health.*)",
    re.IGNORECASE,
)
_HEALTH_LOG_RE = re.compile(
    r"(?:\bhealth\s*(?:check|probe)\b|\bchecking\s+[\w.-]+\s+(?:service\s+)?health\b|"
    r"\bhealth endpoint\b)", re.IGNORECASE,
)
_LIFECYCLE_LOG_RE = re.compile(
    r"(?:^\s*\d+:[A-Z]\s|^\s*starting\s+redis\b|\bredis is (?:starting|now ready to exit|ready to accept connections)\b|"
    r"\b(?:saving the final rdb snapshot|user requested shutdown|scheduling shutdown|server initialized)\b|"
    r"\b(?:service|process|container) (?:starting|started|stopping|stopped|shutting down)\b)",
    re.IGNORECASE,
)
_REDUNDANT_SUCCESS_LOG_RE = re.compile(r"^\s*transaction processed\b", re.IGNORECASE)
_APPLICATION_DIAGNOSTIC_LOG_RE = re.compile(
    r"^\s*(?:request\s+(?:started|complete|error)\b|checkout\s+started\b|home\b|"
    r"[\w.]+async\s+called\b|"
    r"(?:getting|listing|reading|adding|added|emptying|empty|received|serving|placing|placed)\b|"
    r"conversion request successful\b|order placed\b|paymentservice#\w+\b|"
    r"\[(?:recv|getquote|shiporder|placeorder)\b|"
    r"a request to send\b|order confirmation email sent\b)",
    re.IGNORECASE,
)


def _normalized_key(value: Any) -> str:
    return str(value).casefold().replace("-", "_").replace(".", "_")


def _status_value(value: Any, *, grpc: bool = False, http: bool = False, otel: bool = False) -> str:
    if value is None:
        return "UNKNOWN"
    if isinstance(value, bool):
        return "ERROR" if value else "SUCCESS"
    text = str(value).strip().casefold()
    if not text or text in {"none", "null", "unset", "unspecified"}:
        return "UNKNOWN"
    if grpc:
        return _GRPC_STATUS_NAMES.get(text, "UNKNOWN")
    if otel:
        return {"0": "UNKNOWN", "unset": "UNKNOWN", "1": "SUCCESS", "ok": "SUCCESS", "2": "ERROR", "error": "ERROR"}.get(text, "UNKNOWN")
    if text == "unknown":
        return "UNKNOWN"
    if http:
        try:
            code = int(float(text))
        except ValueError:
            return "UNKNOWN"
        return "SUCCESS" if 200 <= code < 400 else ("ERROR" if code >= 400 else "UNKNOWN")
    try:
        numeric = int(float(text))
    except ValueError:
        numeric = None
    if numeric is not None:
        if numeric == 0:
            return "SUCCESS"
        if 1 <= numeric <= 16:
            return "ERROR"
        if 200 <= numeric < 400:
            return "SUCCESS"
        if numeric >= 400:
            return "ERROR"
    if text in {"success", "succeeded", "ok", "okay", "0", "unset"}:
        return "SUCCESS"
    if text in {"error", "failed", "failure", "false", "2", "internal", "unavailable", "deadlineexceeded",
                "deadline_exceeded", "cancelled", "canceled", "unknown", "invalidargument", "invalid_argument",
                "failedprecondition", "failed_precondition", "notfound", "not_found", "resourceexhausted",
                "resource_exhausted", "aborted", "outofrange", "out_of_range", "unimplemented", "dataloss",
                "data_loss", "unauthenticated"}:
        return "ERROR"
    if text.endswith(("exception", "error")):
        return "ERROR"
    return "UNKNOWN"


def canonical_span_outcome(span: dict[str, Any]) -> str:
    """Normalize span status across conventional tags and OTel/gRPC/HTTP attributes."""
    attrs = span.get("attributes") if isinstance(span.get("attributes"), dict) else {}
    candidates: list[tuple[Any, str, bool, bool, bool]] = []
    for key in ("status", "otel.status_code", "status.code"):
        if key in span:
            candidates.append((span[key], key, False, False, key == "otel.status_code"))
        if key in attrs:
            candidates.append((attrs[key], key, False, False, key == "otel.status_code"))
    for key in ("attributes.result", "result"):
        if key in attrs:
            text = str(attrs[key]).strip().casefold()
            grpc_result = text in _GRPC_STATUS_NAMES
            if text.isdigit():
                grpc_result = int(text) <= 16
            candidates.append((attrs[key], key, grpc_result, False, False))
    for key in ("rpc.grpc.status_code", "grpc.status_code", "rpc.response.status_code"):
        if key in attrs:
            candidates.append((attrs[key], key, True, False, False))
    for key in ("http.response.status_code", "http.status_code"):
        if key in attrs:
            candidates.append((attrs[key], key, False, True, False))
    error_value = attrs.get("error", span.get("error"))
    if error_value not in (None, False, 0, "") and str(error_value).strip().casefold() not in {"false", "0", "no", "none"}:
        return "ERROR"
    outcomes = [_status_value(value, grpc=grpc, http=http, otel=otel) for value, _, grpc, http, otel in candidates]
    if "ERROR" in outcomes:
        return "ERROR"
    if "SUCCESS" in outcomes:
        return "SUCCESS"
    # OTel span status is UNSET by default; for an otherwise normally completed
    # semantic operation, instrumentation convention treats the absence of an
    # error status as success. Spans without these semantic markers remain unknown.
    if any(key in attrs for key in ("rpc.system", "rpc.system.name", "http.request.method", "db.system.name")):
        return "SUCCESS"
    return "UNKNOWN"


def _utc_iso_epoch(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _epoch(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _delta(values: list[list[Any]], window_start: float) -> tuple[float | None, bool, str | None, str | None]:
    parsed = [(float(sample[0]), _finite(sample[1])) for sample in values if len(sample) >= 2]
    parsed = sorted(((ts, val) for ts, val in parsed if val is not None), key=lambda row: row[0])
    if len(parsed) < 2 or parsed[0][0] > window_start:
        return None, False, _utc_iso_epoch(parsed[0][0]) if parsed else None, _utc_iso_epoch(parsed[-1][0]) if parsed else None
    first_ts = parsed[0][0]
    last_ts = parsed[-1][0]
    delta = 0.0
    reset = False
    previous = parsed[0][1]
    for _, current in parsed[1:]:
        if current < previous:
            # Prometheus counters reset to zero when their process restarts.
            delta += current
            reset = True
        else:
            delta += current - previous
        previous = current
    return delta, reset, _utc_iso_epoch(first_ts), _utc_iso_epoch(last_ts)


def _label(labels: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = labels.get(name)
        if value not in (None, ""):
            return str(value)
    return None


def _service(labels: dict[str, Any]) -> str | None:
    candidate = _label(labels, "service_name", "service.name", "service")
    if candidate in SUT_SERVICES:
        return candidate
    pod = _label(labels, "pod", "pod_name") or ""
    return next((item for item in SUT_SERVICES if pod == item or pod.startswith(item + "-")), None)


def _canonical_operation(labels: dict[str, Any], name: str) -> str | None:
    if "redis" in name.casefold():
        db_operation = _label(labels, "db_operation_name", "db.operation.name", "operation") or "operation"
        return "redis " + db_operation.upper()
    rpc_method = _label(labels, "rpc_method", "rpc.method")
    rpc_service = _label(labels, "rpc_service", "rpc.service")
    if rpc_method and rpc_service and "/" not in rpc_method:
        candidate = f"{rpc_service.removeprefix('hipstershop.')}/{rpc_method}"
        return candidate if candidate in _RPC_METHODS else None
    http_method = _label(labels, "http_request_method", "http.request.method")
    operation = _label(labels, "operation", "rpc_method", "rpc.method", "http_route", "http.route", "url_path", "url.path") or ""
    operation = operation.removeprefix("grpc.")
    if operation.startswith("/hipstershop."):
        operation = operation.removeprefix("/")
    if operation.startswith("hipstershop."):
        operation = operation.removeprefix("hipstershop.")
        return operation if operation in _RPC_METHODS else None
    if operation in _RPC_METHODS:
        return operation
    if operation.startswith("redis "):
        return operation.upper().replace("REDIS ", "redis ")
    method_path = re.match(r"(GET|POST|PUT|DELETE|PATCH|HEAD)\s+(/[^ ]*)", operation)
    if method_path:
        http_method, operation = method_path.group(1), method_path.group(2)
    if operation in ("GET", "POST", "GET /", "POST /"):
        operation = "/"
    if operation.startswith("/product/"):
        operation = "/product/:id"
    if operation in _HTTP_ROUTES:
        return f"{http_method} {operation}" if http_method else operation
    return None


def _kind(labels: dict[str, Any], name: str, operation: str | None) -> str:
    raw = (_label(labels, "span_kind", "span.kind") or "").casefold()
    lower = name.casefold()
    if operation and operation.startswith("redis "):
        return "datastore"
    if raw in ("client", "server"):
        return raw
    if "client" in lower:
        return "client"
    if "redis" in lower or "datastore" in lower:
        return "datastore"
    return "server"


def _outcome(labels: dict[str, Any]) -> str:
    value = (_label(labels, "outcome", "status_code", "rpc_response_status_code", "rpc.response.status_code",
                    "http_response_status_code", "http.response.status_code", "grpc_status_code", "rpc_grpc_status_code") or "OK")
    return "error" if value.casefold() not in {"ok", "success", "0", "200", "201", "202", "204"} else "success"


def _series_key(service: str, operation: str | None, kind: str, outcome: str, labels: dict[str, Any]) -> tuple[str, str | None, str, str, str | None]:
    target = None
    if operation and operation.startswith("redis "):
        target = "redis-cart"
    elif kind == "client" and operation and "/" in operation:
        target = _RPC_TARGETS.get(operation.split("/", 1)[0])
    return service, operation, kind, outcome, target


def _metric_candidate(scenario: str, labels: dict[str, Any], name: str) -> tuple[int, str, str, str] | None:
    """Return deterministic preference, metric component, and unit for recognized series."""
    low = name.casefold()
    if scenario == "A":
        if low == "tcc_conventional_requests_total":
            return 100, "conventional", "count", "{request}"
        if low == "tcc_conventional_request_duration_seconds_sum":
            return 100, "conventional", "duration_sum", "s"
        if low == "tcc_conventional_request_duration_seconds_count":
            return 100, "conventional", "duration_count", "{request}"
        return None
    if low.endswith("container_cpu_usage_seconds_total"):
        return 100, "cpu_counter", "cpu_cores"
    # Application RPC metrics. Prefer stable semantic-convention instruments over
    # duplicate application counters; use the TCC instrument as a deterministic fallback.
    candidates = (
        (r"rpc_client_call_duration_seconds_(count|sum)$", 100, "rpc_client"),
        (r"rpc_server_call_duration_seconds_(count|sum)$", 100, "rpc_server"),
        (r"tcc_otel_rpc_client_duration_seconds_(count|sum)$", 100, "tcc_rpc_client"),
        (r"tcc_otel_rpc_client_requests_total$", 100, "tcc_rpc_client_count"),
        (r"http_server_request_duration_seconds_(count|sum)$", 100, "http_server"),
        (r"tcc_otel_http_server_requests_total$", 100, "http_server_count"),
        (r"tcc_otel_http_server_duration_seconds_(count|sum)$", 100, "http_server"),
        (r"rpc_server_duration_milliseconds_(count|sum)$", 90, "dotnet_rpc_server"),
        (r"tcc_otel_rpc_server_duration_seconds_(count|sum)$", 80, "tcc_rpc_server"),
        (r"cart_redis_operation_duration_seconds_(count|sum)$", 100, "redis"),
        (r"cart_redis_operations_total$", 100, "redis_count"),
    )
    for pattern, priority, family in candidates:
        match = re.search(pattern, low)
        if match:
            component = match.group(1) if match.lastindex else "count"
            if family == "dotnet_rpc_server":
                component = "duration_" + component
            if family in ("redis_count", "http_server_count", "tcc_rpc_client_count"):
                component = "count" if family in ("redis_count", "tcc_rpc_client_count") else "errors"
                if family == "http_server_count":
                    family, component = "http_server", "count"
                elif family == "tcc_rpc_client_count":
                    family, component = "tcc_rpc_client", "count"
                else:
                    family = "redis"
            unit = "ms" if "milliseconds" in low else ("s" if component.endswith("sum") else "{request}")
            return priority, family, component, unit
    return None


def common_metrics(raw: dict[str, Any], scenario: str, window: dict[str, str]) -> list[dict[str, Any]]:
    """Map fixed A/B native metric names to request/count/duration/CPU concepts."""
    data = raw.get("response", {}).get("data", {})
    series = data.get("result", [])
    by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    cpu_series: dict[str, dict[float, float]] = defaultdict(lambda: defaultdict(float))
    for item in series:
        labels = dict(item.get("metric", {}))
        name = labels.pop("__name__", "")
        svc = _service(labels)
        if not svc:
            continue
        if name.casefold().endswith("container_cpu_usage_seconds_total"):
            values = item.get("values", [])
            parsed = sorted(((float(x[0]), _finite(x[1])) for x in values if len(x) >= 2
                             and _finite(x[1]) is not None), key=lambda row: row[0])
            for (ts0, v0), (ts1, v1) in zip(parsed, parsed[1:]):
                delta = v1 - v0 if v1 >= v0 else v1
                elapsed = ts1 - ts0
                if elapsed > 0:
                    cpu_series[svc][ts1] += max(delta, 0.0) / elapsed
            continue
        candidate = _metric_candidate(scenario, labels, name)
        if candidate is None:
            continue
        if scenario == "B" and not ("tcc_otel_" in name.casefold() or "cart_redis_" in name.casefold()):
            # Native SDK/framework metrics remain available in RAW for audit.
            # Only instruments emitted by a shared observation adapter enter
            # the model-visible common contract.
            continue
        priority, family, component, unit = candidate
        operation = _canonical_operation(labels, name)
        if operation is None:
            continue
        if "rpc_service" in labels and _label(labels, "rpc_method"):
            rpc_method = str(labels["rpc_method"])
            rpc_service = str(labels["rpc_service"]).removeprefix("hipstershop.")
            operation = rpc_method if "/" in rpc_method else f"{rpc_service}/{rpc_method}"
            if operation not in _RPC_METHODS:
                continue
        kind = "client" if "rpc_client" in name else ("server" if "rpc_server" in name or "http_server" in name else _kind(labels, name, operation))
        outcome = _outcome(labels)
        key = (*_series_key(svc, operation, kind, outcome, labels), family, component)
        previous = by_key.get(key)
        if previous is None or priority > previous["priority"]:
            by_key[key] = {"priority": priority, "family": family, "component": component, "unit": unit,
                           "labels": labels, "name": name, "values": item.get("values", [])}
    canonical: dict[tuple[Any, ...], dict[str, Any]] = {}
    for key, row in by_key.items():
        svc, operation, kind, outcome, target, family, component = key
        base = (svc, operation, kind, outcome, target)
        group = canonical.setdefault(base, {"service": svc, "operation": operation, "kind": kind,
            "outcome": outcome, "target": target, "native": {}, "family_priority": {}, "labels": {}})
        delta, reset, first, last = _delta(row["values"], _epoch(window["start"]))
        if row["priority"] > group["family_priority"].get(family, -1):
            group["family_priority"][family] = row["priority"]
        group["native"].setdefault(family, {})[component] = {"delta": delta, "unit": row["unit"], "counter_reset": reset,
                                      "first_sample": first, "last_sample": last, "native_name": row["name"]}
        # The stable diagnostic dimensions have dedicated common fields above.
    out: list[dict[str, Any]] = []
    for base, group in sorted(canonical.items(), key=lambda x: tuple(str(v) for v in x[0])):
        all_native = group.pop("native")
        priorities = group.pop("family_priority")
        selected_family = max(priorities, key=lambda name: (priorities[name], name))
        native = all_native[selected_family]
        count_row = native.get("count") or native.get("duration_count")
        count = count_row["delta"] if count_row else None
        duration_count_row = native.get("duration_count") or native.get("count")
        duration_count = duration_count_row["delta"] if duration_count_row else None
        sum_row = native.get("duration_sum") or native.get("sum")
        duration_sum = sum_row["delta"] if sum_row else None
        if duration_sum is not None and sum_row and sum_row.get("unit") == "ms":
            duration_sum /= 1000.0
        # A valid cumulative counter/histogram with no increment contributes no
        # activity to the RCA interval. Missing baselines remain unknown and
        # are deliberately not treated as zero.
        activity_delta = count if count_row is not None else duration_count
        if activity_delta == 0:
            continue
        timestamp = next((v["last_sample"] for v in native.values() if v.get("last_sample")), window["end"])
        if count_row is not None:
            out.append({"service": group["service"], "concept": "request_count", "operation": group["operation"],
                        "kind": group["kind"], "target": group["target"], "outcome": group["outcome"],
                        "timestamp": timestamp, "value": count, "unit": "{request}", "labels": group["labels"],
                        "counter_reset": any(v["counter_reset"] for v in native.values()),
                        "delta_status": "missing_baseline" if count is None else ("counter_reset" if any(v["counter_reset"] for v in native.values()) else "valid"),
                        "native_names": sorted({v["native_name"] for v in native.values()})})
        if sum_row is not None and duration_count_row is not None:
            avg = duration_sum / duration_count if duration_sum is not None and duration_count and duration_count > 0 else None
            out.append({"service": group["service"], "concept": "request_duration", "operation": group["operation"],
                        "kind": group["kind"], "target": group["target"], "outcome": group["outcome"],
                        "timestamp": timestamp, "value": avg, "unit": "s", "count_delta": duration_count,
                        "sum_delta_seconds": duration_sum, "labels": group["labels"],
                        "counter_reset": any(v["counter_reset"] for v in native.values()),
                        "delta_status": "missing_baseline" if duration_sum is None or duration_count is None else ("counter_reset" if any(v["counter_reset"] for v in native.values()) else "valid"),
                        "native_names": sorted({v["native_name"] for v in native.values()})})
    for service, samples in sorted(cpu_series.items()):
        ordered_samples = sorted(samples.items())
        values = [v for _, v in ordered_samples]
        if not values:
            continue
        out.append({"service": service, "concept": "cpu_utilization", "operation": None, "kind": "service",
                    "target": None, "outcome": None, "timestamp": _utc_iso_epoch(ordered_samples[-1][0]),
                    "window": window, "sample_count": len(values), "value": {"first": values[0], "min": min(values), "max": max(values),
                    "avg": sum(values) / len(values), "last": values[-1]}, "unit": "cpu_cores", "labels": {},
                    "native_names": ["container_cpu_usage_seconds_total"]})
    for index, metric in enumerate(out, 1):
        metric["evidence_id"] = f"metric-{index:06d}"
    return out


def _compact_log_labels(labels: dict[str, Any], structured: dict[str, Any], scenario: str) -> tuple[dict[str, Any], dict[str, Any]]:
    merged = {**labels, **structured}
    severity = _label(merged, "severity", "severity_text", "level", "detected_level", "severity_number")
    correlation = {"trace_id": _label(merged, "trace_id", "traceID"), "span_id": _label(merged, "span_id", "spanID")}
    if scenario == "A" and any(correlation.values()):
        raise DatasetError("Scenario A log correlation invariant violated in RAW")
    attributes: dict[str, Any] = {}
    for key, value in merged.items():
        normalized = _normalized_key(key)
        if normalized in _RESOURCE_NOISE or normalized in _LOG_METADATA_KEYS or normalized in {"service_name", "service", "severity", "severity_text", "severity_number", "level", "detected_level", "trace_id", "traceid", "span_id", "spanid", "traceparent", "observed_timestamp", "flags"}:
            continue
        if normalized.replace("_", "") in _NON_DIAGNOSTIC_LOG_FIELDS:
            continue
        if normalized.startswith(("telemetry_sdk_", "process_", "host_", "container_", "pod_", "otel_scope_")):
            continue
        attributes[key] = value
    return {"service": _label(merged, "service_name", "service.name", "service"), "severity": severity,
            **correlation}, attributes


def _log_body_and_fields(body: Any) -> tuple[Any, dict[str, Any], Any]:
    """Extract native structured application messages and discard logger envelope noise."""
    parsed: dict[str, Any] = {}
    if isinstance(body, dict):
        parsed = dict(body)
    elif isinstance(body, str) and body.lstrip().startswith("{"):
        try:
            value = json.loads(body)
            if isinstance(value, dict):
                parsed = value
        except json.JSONDecodeError:
            pass
    if not parsed:
        message = body
        body_severity = None
        # The conventional .NET console logger stores its text envelope inside
        # the Loki body, while the OTel provider stores the same information in
        # structured fields. Remove only that known logger envelope so the
        # normalized event has the same message/severity representation.
        if isinstance(message, str):
            logger_line = re.match(
                r"(?is)^\s*(trce|dbug|info|warn|fail|crit|trace|debug|information|warning|error|critical)\s*:\s*[^\[\n]+\[\d+\]\s*\n\s*(.*)$",
                message,
            )
            if logger_line:
                level = logger_line.group(1).casefold()
                body_severity = {
                    "trce": "TRACE", "trace": "TRACE", "dbug": "DEBUG", "debug": "DEBUG",
                    "info": "INFO", "information": "INFO", "warn": "WARN", "warning": "WARN",
                    "fail": "ERROR", "error": "ERROR", "crit": "FATAL", "critical": "FATAL",
                }[level]
                message = logger_line.group(2).strip()
    else:
        message = parsed.get("message", parsed.get("body", body))
        body_severity = parsed.get("severity", parsed.get("severity_text", parsed.get("level")))
        parsed = {key: value for key, value in parsed.items()
                  if str(key).casefold().replace(".", "_").replace("-", "_")
                  not in {"message", "body", "severity", "severity_text", "severity_number", "level", "detected_level"}}
    if isinstance(message, str):
        # These fields vary per request/user and do not identify any fault in the
        # fixed experiment. Keep the event and operation while removing values.
        message = re.sub(r"\buserId=([^,\s]+)", "userId={UserId}", message)
        message = re.sub(r"\bproductId=([^,\s]+)", "productId={ProductId}", message)
        message = re.sub(r"\bquantity=([^,\s]+)", "quantity={Quantity}", message)
        message = re.sub(r"\b(for user )[^\s,]+", r"\1{UserId}", message, flags=re.IGNORECASE)
        message = re.sub(r"\bAdding product [^\s]+ to cart for user \{UserId\}",
                         "Adding product {ProductId} to cart for user {UserId}", message)
        message = re.sub(r"product_ids=\[[^\]]*\]", "product_ids=[{ProductIds}]", message)
        message = re.sub(r"(PaymentService#Charge invoked with request )\{.*\}$", r"\1{request}", message)
        message = re.sub(r"(request )\{\"amount\":.*\}$", r"\1{request}", message)
    # Align native exception fields and conventional error messages on one field.
    error_message = None
    for key in ("error.message", "error_message", "exception.message", "exception_message", "error"):
        candidate = parsed.get(key)
        if isinstance(candidate, (str, int, float)) and str(candidate).strip():
            error_message = str(candidate).strip()
            break
    if error_message is None and isinstance(message, str):
        match = re.match(r"^Error:\s*(.+)$", message.strip(), re.IGNORECASE)
        if match:
            error_message = match.group(1).strip()
    if error_message:
        parsed["error.message"] = error_message
        for key in ("error", "error_message", "exception.message", "exception_message"):
            parsed.pop(key, None)
    return message, parsed, body_severity


def canonical_log_severity(value: Any) -> str | None:
    """Normalize known severity names/numbers; return None when not safely inferable."""
    if value is None:
        return None
    text = str(value).strip().casefold()
    aliases = {
        "trace": "DEBUG", "trce": "DEBUG", "debug": "DEBUG", "dbug": "DEBUG",
        "info": "INFO", "information": "INFO", "informational": "INFO",
        "warn": "WARN", "warning": "WARN", "wrn": "WARN",
        "error": "ERROR", "err": "ERROR", "fail": "ERROR", "critical": "FATAL", "crit": "FATAL", "fatal": "FATAL",
    }
    if text in aliases:
        return aliases[text]
    try:
        number = int(text)
    except ValueError:
        return None
    if 1 <= number <= 4:
        return "DEBUG"
    if 5 <= number <= 8:
        return "DEBUG"
    if 9 <= number <= 12:
        return "INFO"
    if 13 <= number <= 16:
        return "WARN"
    if 17 <= number <= 20:
        return "ERROR"
    if 21 <= number <= 24:
        return "FATAL"
    return None


def canonical_log_classification(log: dict[str, Any]) -> dict[str, Any]:
    """Classify a normalized log without scenario, fault, or Ground Truth context.

    Only explicit application error/warning classes and recognized application
    diagnostic event forms enter the Common RCA View. Unknown events remain in
    RAW and are excluded by default.
    """
    attrs = log.get("attributes") if isinstance(log.get("attributes"), dict) else {}
    event_name = str(_shared_event_value(attrs, "event_name") or log.get("event_name") or "").strip().casefold()
    marked_value = _shared_event_value(attrs, "observation_event")
    if marked_value is None:
        marked_value = log.get("observation_event")
    marked = marked_value is True or str(marked_value).casefold() in {"true", "1"}
    if not marked or event_name not in _SHARED_APPLICATION_EVENTS:
        return {"category": "RAW_AUDIT_ONLY", "model_visible": False,
                "reason": "not_a_marked_shared_observation_event", "event_class": event_name}
    severity = canonical_log_severity(log.get("severity"))
    if severity not in {"WARN", "ERROR", "FATAL"}:
        return {"category": "RAW_AUDIT_ONLY", "model_visible": False,
                "reason": "shared_event_has_non_diagnostic_severity", "event_class": event_name}
    category = "APPLICATION_WARNING" if severity == "WARN" else "APPLICATION_ERROR"
    return {"category": category, "model_visible": True,
            "reason": "explicit_shared_observation_event", "event_class": event_name}


def _canonical_log_message(value: Any) -> str:
    text = str(value or "").casefold().strip()
    text = re.sub(r"\b[0-9a-f]{8}-[0-9a-f-]{27,}\b", "{uuid}", text)
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "{number}", text)
    text = re.sub(r"\s+", " ", text)
    return text


def canonical_log_event(log: dict[str, Any]) -> tuple[str, ...]:
    """A/B identity for explicit shared events; rendered text and IDs are secondary."""
    attrs = log.get("attributes") if isinstance(log.get("attributes"), dict) else {}
    classification = canonical_log_classification(log)
    service = str(log.get("service") or "")
    severity = canonical_log_severity(log.get("severity")) or "UNKNOWN"
    event_name = str(_shared_event_value(attrs, "event_name") or log.get("event_name") or "").casefold()
    operation = str(_shared_event_value(attrs, "operation", "rpc.method", "rpc_method", "http.route", "http_route") or "")
    outcome = str(_shared_event_value(attrs, "outcome") or log.get("outcome") or "").casefold()
    error_type = str(_shared_event_value(attrs, "error.type", "error_type") or "").casefold()
    return service, severity, event_name, operation.casefold(), outcome, error_type


def _common_error_event(message: Any, attributes: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """Represent the same native .NET error event without logger/stack formatting."""
    text = str(message) if message is not None else ""
    status_match = re.search(r"Error status code '([^']+)' with detail '(.*)' raised\.?", text, re.DOTALL)
    status_code = attributes.get("StatusCode", attributes.get("status_code"))
    detail = attributes.get("Detail", attributes.get("detail"))
    if status_match:
        status_code = status_code or status_match.group(1)
        detail = detail or status_match.group(2)
    if detail is None and not status_match:
        return message, attributes

    clean_detail = re.split(r"\n|\s+--->\s+|\s+at [\w.]+\.", str(detail), maxsplit=1)[0].strip().rstrip("'")
    exception_match = re.search(r"([\w.$+]+(?:Exception|Error)):\s*", clean_detail)
    error_type = exception_match.group(1) if exception_match else None
    error_message = clean_detail
    if error_type:
        if exception_match and exception_match.start() > 0:
            error_message = clean_detail[:exception_match.start()].strip()
        elif exception_match:
            error_message = clean_detail[exception_match.end():].strip()
    normalized = {key: value for key, value in attributes.items() if key not in {"StatusCode", "status_code", "Detail", "detail"}}
    if status_code:
        normalized["status_code"] = status_code
    if error_type:
        normalized["error.type"] = error_type
    if error_message:
        normalized["error.message"] = error_message
    return "Error status code '{StatusCode}' with detail '{Detail}' raised.", normalized


def _canonical_error_message(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # Unwrap nested RPC errors to their innermost application description, then
    # remove stack/runtime detail beginning at an exception class or frame.
    descriptions = re.findall(r"\bdesc\s*=\s*([^\n]+)", text, flags=re.IGNORECASE)
    if descriptions:
        text = descriptions[-1].strip()
    text = re.split(r"\n|\s+--->\s+|\s+at\s+[\w.$<>`+/-]+\s*\(", text, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    exception = re.search(r"\b[\w.$+]*(?:Exception|Error):", text)
    if exception:
        text = text[:exception.start()].strip()
    text = re.sub(r"^(?:rpc error:\s*code\s*=\s*[^ ]+\s*desc\s*=\s*)", "", text, flags=re.IGNORECASE).strip()
    return text or None


def common_logs_with_audit(raw: dict[str, Any], scenario: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = []
    categories: dict[str, int] = defaultdict(int)
    excluded_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    # collect_loki stores both the merged response and original pages; consume only
    # the merged response to avoid duplicating log records.
    responses = [raw.get("response", {})]
    for response in responses:
        for stream in response.get("data", {}).get("result", []):
            labels = dict(stream.get("stream", {})); structured = dict(stream.get("structuredMetadata", {}))
            for timestamp, body in stream.get("values", []):
                normalized_body, body_fields, body_severity = _log_body_and_fields(body)
                view, attrs = _compact_log_labels(labels, {**structured, **body_fields}, scenario)
                service = view["service"]
                if service not in SUT_SERVICES:
                    continue
                attrs = {key: value for key, value in attrs.items()
                         if str(key).casefold().replace(".", "_").replace("-", "_")
                         not in {"timestamp", "time", "instant", "message", "body"}}
                normalized_body, attrs = _common_error_event(normalized_body, attrs)
                severity_value = view["severity"]
                if severity_value is None or str(severity_value).casefold() in {"unknown", "unspecified", "none", ""}:
                    severity_value = body_severity or attrs.get("severity_number")
                if severity_value is None and re.match(r"^(?:Error:|Error status code )", str(normalized_body or ""), re.IGNORECASE):
                    severity_value = "ERROR"
                # Only explicit, safely classifiable severity is admitted to the Common RCA View.
                severity = canonical_log_severity(severity_value)
                message_text = str(normalized_body or "")
                # Do not represent stack continuations as independent application events.
                if _STACK_FRAME_RE.match(message_text):
                    category = "RUNTIME_NOISE"
                    classification = {"category": category, "model_visible": False,
                                      "reason": "stack_frame_or_continuation", "event_class": _canonical_log_message(message_text)}
                else:
                    classification = canonical_log_classification({"service": service, "severity": severity,
                        "body": normalized_body, "attributes": attrs})
                    category = str(classification["category"])
                categories[category] += 1
                if not classification["model_visible"]:
                    if len(excluded_samples[category]) < 5:
                        excluded_samples[category].append({"service": service, "severity": severity,
                            "body": normalized_body, "reason": classification["reason"]})
                    continue
                canonical_attrs = {}
                for key, value in attrs.items():
                    normalized_key = _normalized_key(key)
                    if normalized_key in {"error", "error_type", "error_message", "exception_message", "exception_type", "exception_stacktrace", "exception_stack_trace", "observation_event"}:
                        continue
                    canonical_attrs[key] = value
                event_name = _shared_event_value(attrs, "event_name") or body_fields.get("event_name")
                if event_name:
                    canonical_attrs["event_name"] = str(event_name).casefold()
                operation = _shared_event_value(attrs, "operation") or body_fields.get("operation") or body_fields.get("Operation")
                if operation:
                    canonical_attrs["operation"] = str(operation)
                outcome = _shared_event_value(attrs, "outcome") or body_fields.get("outcome")
                if outcome:
                    canonical_attrs["outcome"] = str(outcome).upper()
                error_message = next((value for key, value in attrs.items()
                                      if _normalized_key(key) in {"error_message", "exception_message", "error"}), None)
                error_message = error_message or attrs.get("error.message") or attrs.get("exception.message")
                if error_message is None:
                    error_message = canonical_attrs.pop("error.message", None)
                else:
                    canonical_attrs.pop("exception.message", None)
                    canonical_attrs.pop("error.message", None)
                if error_message is None:
                    error_message = body_fields.get("error.message")
                if error_message:
                    canonical_attrs["error.message"] = _canonical_error_message(error_message) or str(error_message)
                rows.append({"timestamp": _utc_iso_epoch(int(timestamp) / 1_000_000_000), "service": service,
                             "severity": severity, "body": normalized_body,
                             "event_name": str(event_name or "").casefold(),
                             "trace_id": view["trace_id"], "span_id": view["span_id"], "attributes": canonical_attrs})
    rows.sort(key=lambda x: (x["timestamp"], x["service"], str(x["body"]), str(x.get("trace_id"))))
    for index, row in enumerate(rows, 1):
        row["evidence_id"] = f"log-{index:06d}"
    return rows, {"categories": dict(sorted(categories.items())),
                  "excluded_samples": dict(sorted(excluded_samples.items())),
                  "raw_candidate_records": sum(categories.values()),
                  "model_visible_records": len(rows)}


def common_logs(raw: dict[str, Any], scenario: str) -> list[dict[str, Any]]:
    """Return only the model-visible Common RCA log projection."""
    return common_logs_with_audit(raw, scenario)[0]


def _canonical_span_operation(span: dict[str, Any], tags: dict[str, Any]) -> str | None:
    raw = str(span.get("operationName") or "")
    http_method = str(tags.get("http.request.method") or "")
    lower = raw.casefold()
    if "grpc.health.v1.health/check" in lower or "/metrics" in lower:
        return None
    if raw in ("GET", "POST", "PUT", "DELETE"):
        raw = str(tags.get("http.route") or tags.get("url.path") or "")
    method_match = re.match(r"(GET|POST|PUT|DELETE|PATCH|HEAD)\s+", raw)
    if method_match:
        http_method = method_match.group(1)
        raw = raw[method_match.end():]
    raw = raw.removeprefix("grpc.")
    if raw.startswith("/hipstershop."):
        raw = raw.removeprefix("/")
    if raw.startswith("hipstershop."):
        raw = raw.removeprefix("hipstershop.")
        return raw if raw in _RPC_METHODS else None
    if raw in _RPC_METHODS:
        return raw
    if raw.startswith("redis "):
        return raw.lower()
    if raw in ("", "/"):
        return "/"
    if raw.startswith("/product/"):
        raw = "/product/:id"
    if raw in _HTTP_ROUTES:
        return f"{http_method} {raw}" if http_method else raw
    return None


def common_traces(raw: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    span_records = []
    for search in raw.get("search_results", []):
        for trace in search.get("response", {}).get("data", []):
            trace_id = trace.get("traceID")
            if not trace_id:
                continue
            dest = grouped.setdefault(trace_id, {"trace_id": trace_id, "spans": {}})
            for span in trace.get("spans", []):
                if span.get("spanID"):
                    # Jaeger process IDs are scoped to one trace payload, not
                    # globally stable across service searches. Keep service
                    # identity with each span before union/deduplication.
                    process = trace.get("processes", {}).get(span.get("processID"), {})
                    dest["spans"].setdefault(span["spanID"], (span, process.get("serviceName")))
    for trace in grouped.values():
        accepted = []
        for span, service in trace["spans"].values():
            if service not in SUT_SERVICES:
                continue
            tags = {item.get("key"): item.get("value") for item in span.get("tags", []) if item.get("key") is not None}
            operation = _canonical_span_operation(span, tags)
            if operation is None:
                continue
            kind = str(tags.get("span.kind") or "server").casefold()
            if kind not in ("server", "client"):
                continue
            peer_address = str(tags.get("server.address") or tags.get("net.peer.name") or "")
            if "observability" in peer_address.casefold():
                continue
            target = None
            if operation.startswith("redis "):
                kind, target = "datastore", "redis-cart"
            elif kind == "client" and "/" in operation:
                target = _RPC_TARGETS.get(operation.split("/", 1)[0])
            if isinstance(target, str) and "observability" in target.casefold():
                continue
            parent = next((ref.get("spanID") for ref in span.get("references", []) if ref.get("refType") == "CHILD_OF"), None)
            start_us = int(span.get("startTime", 0)); duration_us = int(span.get("duration", 0))
            # Canonical outcome consumes the complete native status surface before
            # redundant instrumentation attributes are discarded from the RCA view.
            outcome = canonical_span_outcome({"status": tags.get("status"), "attributes": tags})
            attrs: dict[str, Any] = {}
            events: list[dict[str, Any]] = []
            accepted.append({"trace_id": trace["trace_id"], "span_id": span.get("spanID"),
                "parent_span_id": parent, "service": service, "operation": operation, "kind": kind,
                "target": target, "start": _utc_iso_epoch(start_us / 1_000_000),
                "duration_ms": duration_us / 1000.0, "status": outcome,
                "attributes": attrs, "events": events})
        if accepted:
            span_records.extend(accepted)
    # A/B correlation IDs differ, so trace/span IDs are intentionally preserved;
    # only the logical span envelope is compacted and internal health/scrape spans removed.
    by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for span in span_records:
        by_trace[span["trace_id"]].append(span)
    traces = []
    for trace_id, spans in sorted(by_trace.items(), key=lambda x: (min(s["start"] for s in x[1]), x[0])):
        spans.sort(key=lambda s: (s["start"], s["span_id"] or ""))
        starts = [datetime.fromisoformat(s["start"].replace("Z", "+00:00")) for s in spans]
        start = min(starts)
        end = max(datetime.fromtimestamp(starts[i].timestamp() + spans[i]["duration_ms"] / 1000, timezone.utc) for i in range(len(spans)))
        traces.append({"trace_id": trace_id, "start": start.isoformat(timespec="microseconds").replace("+00:00", "Z"),
            "duration_ms": max(0.0, (end-start).total_seconds()*1000), "services": sorted({s["service"] for s in spans}), "spans": spans})
    for index, trace in enumerate(traces, 1):
        trace["evidence_id"] = f"trace-{index:06d}"
        for span_index, span in enumerate(trace["spans"], 1):
            span["evidence_id"] = f"span-{index:06d}-{span_index:04d}"
            span.pop("trace_id", None)
    return traces

