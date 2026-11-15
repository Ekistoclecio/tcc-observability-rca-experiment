from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any


SUT_SERVICES = (
    "frontend", "checkoutservice", "cartservice", "productcatalogservice", "currencyservice",
    "paymentservice", "shippingservice", "emailservice", "recommendationservice", "adservice", "redis-cart",
)
SCENARIOS = ("A", "B")
RAW_SIGNALS = ("metrics", "logs", "traces")
RUN_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,50}\Z")


class DatasetError(RuntimeError):
    pass


@dataclass(frozen=True)
class Window:
    confirmed_start: datetime
    confirmed_end: datetime
    start: datetime
    end: datetime
    pre_buffer_seconds: float
    post_buffer_seconds: float

    def json(self) -> dict[str, Any]:
        return {
            "confirmed_start": iso_utc(self.confirmed_start),
            "confirmed_end": iso_utc(self.confirmed_end),
            "pre_buffer_seconds": self.pre_buffer_seconds,
            "post_buffer_seconds": self.post_buffer_seconds,
            "start": iso_utc(self.start),
            "end": iso_utc(self.end),
        }


def parse_timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise DatasetError(f"Ground Truth {field} must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DatasetError(f"Ground Truth {field} is not a valid ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DatasetError(f"Ground Truth {field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def epoch_microseconds(value: datetime) -> int:
    utc = value.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = utc - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def epoch_nanoseconds(value: datetime) -> int:
    return epoch_microseconds(value) * 1_000


def epoch_seconds_decimal(value: datetime) -> str:
    micros = epoch_microseconds(value)
    seconds, fraction = divmod(micros, 1_000_000)
    return f"{seconds}.{fraction:06d}"


def window_from_ground_truth(document: dict[str, Any], pre_buffer_seconds: float, post_buffer_seconds: float) -> Window:
    """Read only confirmed_start/confirmed_end; causal fields are intentionally ignored."""
    if document.get("status") != "valid":
        raise DatasetError("only Ground Truth runs with status=valid can produce an official dataset")
    validation = document.get("validation", {})
    if validation.get("injection_confirmed") is not True or validation.get("recovery_confirmed") is not True:
        raise DatasetError("Ground Truth does not confirm both injection and recovery")
    if (not math.isfinite(pre_buffer_seconds) or not math.isfinite(post_buffer_seconds)
            or pre_buffer_seconds < 0 or post_buffer_seconds < 0):
        raise DatasetError("pre/post buffers cannot be negative")
    timing = document.get("timing", {})
    confirmed_start = parse_timestamp(timing.get("confirmed_start"), "confirmed_start")
    confirmed_end = parse_timestamp(timing.get("confirmed_end"), "confirmed_end")
    if confirmed_end <= confirmed_start:
        raise DatasetError("confirmed_end must be later than confirmed_start")
    return Window(
        confirmed_start=confirmed_start,
        confirmed_end=confirmed_end,
        start=confirmed_start - timedelta(seconds=pre_buffer_seconds),
        end=confirmed_end + timedelta(seconds=post_buffer_seconds),
        pre_buffer_seconds=pre_buffer_seconds,
        post_buffer_seconds=post_buffer_seconds,
    )


def build_query_plan(window: Window) -> dict[str, list[dict[str, Any]]]:
    """Build fixed global SUT queries. No Ground Truth fault field is accepted here."""
    # PromQL/RE2 treats '-' literally outside a character class; Python's re.escape
    # emits "\\-", which Prometheus rejects as an unknown escape.
    services_regex = "^(" + "|".join(SUT_SERVICES) + ")$"
    pod_regex = "^(" + "|".join(SUT_SERVICES) + ")-.*$"
    prometheus_selector = f'{{service_name=~"{services_regex}"}} or {{pod=~"{pod_regex}"}}'
    start_s = epoch_seconds_decimal(window.start)
    end_s = epoch_seconds_decimal(window.end)
    start_ns = epoch_nanoseconds(window.start)
    end_ns = epoch_nanoseconds(window.end)
    start_us = epoch_microseconds(window.start)
    end_us = epoch_microseconds(window.end)
    plan: dict[str, list[dict[str, Any]]] = {}
    for scenario in SCENARIOS:
        plan[scenario] = [
            {
                "backend": "prometheus", "scenario": scenario,
                "method": "GET", "path": "/api/v1/query_range",
                "params": {"query": prometheus_selector, "start": start_s,
                           "end": end_s, "step": "5s"},
            },
            {
                "backend": "loki", "scenario": scenario,
                "method": "GET", "path": "/loki/api/v1/query_range",
                "params": {"query": f'{{service_name=~"{services_regex}"}}', "start": str(start_ns),
                           "end": str(end_ns), "direction": "forward", "limit": 100000},
            },
            {
                "backend": "jaeger", "scenario": scenario,
                "method": "GET", "path": "/api/services", "params": {},
                "search": {"services": list(SUT_SERVICES), "start": start_us, "end": end_us, "limit": 10000},
            },
        ]
    return plan


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"cannot read valid JSON from {path}: {exc}") from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_run_id(run_id: str) -> None:
    if not RUN_ID_RE.fullmatch(run_id):
        raise DatasetError("run_id must be 1-51 lowercase DNS-safe characters")


def validate_dataset_shape(dataset: dict[str, Any]) -> None:
    required = {"schema_version", "run_id", "scenario", "window", "services", "metrics", "logs", "traces", "summary"}
    if not required.issubset(dataset):
        raise DatasetError(f"normalized dataset missing fields: {sorted(required - set(dataset))}")
    if dataset["schema_version"] != "1.0" or dataset["scenario"] not in SCENARIOS:
        raise DatasetError("normalized dataset schema/scenario is invalid")
    if set(dataset["services"]) != set(SUT_SERVICES):
        raise DatasetError("normalized dataset service scope differs from the fixed SUT allowlist")
    for signal in ("metrics", "logs", "traces"):
        if not isinstance(dataset[signal], list):
            raise DatasetError(f"normalized {signal} must be an array")
    for record in dataset["logs"]:
        if record.get("service") not in SUT_SERVICES:
            raise DatasetError("normalized log escaped the fixed SUT allowlist")
        if dataset["scenario"] == "A" and (record.get("trace_id") is not None or record.get("span_id") is not None):
            raise DatasetError("Scenario A logs must not receive synthetic trace/span correlation")
    for metric in dataset["metrics"]:
        if metric.get("service") not in SUT_SERVICES:
            raise DatasetError("normalized metric escaped the fixed SUT allowlist")
        forbidden_metric_labels = {"trace_id", "span_id", "request_id", "flow_id", "user_id", "session_id", "cart_id", "run_id"}
        if forbidden_metric_labels.intersection(str(key).lower() for key in metric.get("labels", {})):
            raise DatasetError("metrics must not use request/trace/run identifiers as labels")
    for trace in dataset["traces"]:
        if any(service not in SUT_SERVICES for service in trace.get("services", [])):
            raise DatasetError("normalized trace contains service outside the fixed SUT allowlist")
