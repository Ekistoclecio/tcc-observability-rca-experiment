#!/usr/bin/env python3
"""Collect native Scenario A/B backend responses into a write-once RAW bundle."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen


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
TRACE_WINDOW_US = 30_000_000
MIN_TRACE_WINDOW_US = 1
LOG_WINDOW_NS = 30_000_000_000
MIN_LOG_WINDOW_NS = 1
PORT_FORWARDS = (
    ("prometheus-a", "observability-a", "prometheus-a", 9090, "/-/ready"),
    ("prometheus-b", "observability-b", "prometheus-b", 9090, "/-/ready"),
    ("loki-a", "observability-a", "loki-a", 3100, "/ready"),
    ("loki-b", "observability-b", "loki-b", 3100, "/ready"),
    ("jaeger-a", "observability-a", "jaeger-a", 16686, "/api/services"),
    ("jaeger-b", "observability-b", "jaeger-b", 16686, "/api/services"),
)


class CollectionError(RuntimeError):
    pass


class BackendRequestError(CollectionError):
    def __init__(self, message: str, *, http_status: int | None = None,
                 response_bytes: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.response_bytes = response_bytes


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def epoch_ns(value: datetime) -> int:
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1_000


def format_epoch_ns(value: int) -> str:
    seconds, nanos = divmod(value, 1_000_000_000)
    instant = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    return instant.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


def generate_log_windows(start_ns: int, end_ns: int,
                         window_ns: int = LOG_WINDOW_NS) -> list[tuple[int, int]]:
    """Partition the Loki half-open interval [start_ns,end_ns) using integer arithmetic."""
    if start_ns >= end_ns:
        raise CollectionError("Loki query start must precede end")
    if window_ns < 1:
        raise CollectionError("Loki window width must be at least one nanosecond")
    windows = []
    current = start_ns
    while current < end_ns:
        boundary = min(current + window_ns, end_ns)
        windows.append((current, boundary))
        current = boundary
    validate_log_interval_coverage(windows, start_ns, end_ns)
    return windows


def split_log_window(start_ns: int, end_ns: int) -> tuple[tuple[int, int], tuple[int, int]]:
    if end_ns - start_ns <= MIN_LOG_WINDOW_NS:
        raise CollectionError(
            f"Loki result may be truncated at minimum {MIN_LOG_WINDOW_NS}ns resolution: "
            f"window=[{start_ns},{end_ns})"
        )
    midpoint = start_ns + ((end_ns - start_ns) // 2)
    if midpoint <= start_ns or midpoint >= end_ns:
        raise CollectionError(f"Loki window cannot be split safely: [{start_ns},{end_ns})")
    return (start_ns, midpoint), (midpoint, end_ns)


def validate_log_interval_coverage(windows: list[tuple[int, int]], start_ns: int,
                                   end_ns: int) -> None:
    if not windows or windows[0][0] != start_ns or windows[-1][1] != end_ns:
        raise CollectionError("Loki windows do not cover the exact requested interval")
    previous_end = start_ns
    for left, right in windows:
        if left != previous_end:
            raise CollectionError("Loki windows contain a temporal gap or overlap")
        if left >= right:
            raise CollectionError("Loki window must have positive duration")
        previous_end = right


def loki_entry_count(parsed: dict[str, Any]) -> tuple[int, int]:
    data = parsed.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "streams":
        raise CollectionError("Loki query response is not a streams result")
    streams = data.get("result")
    if not isinstance(streams, list):
        raise CollectionError("Loki streams result is malformed")
    count = 0
    for stream in streams:
        values = stream.get("values") if isinstance(stream, dict) else None
        if not isinstance(values, list):
            raise CollectionError("Loki stream has no values array")
        count += len(values)
    return count, len(streams)


def validate_log_query_coverage(entries: list[dict[str, Any]], start_ns: int,
                                end_ns: int, attempts: list[dict[str, Any]],
                                window_ns: int = LOG_WINDOW_NS) -> dict[str, Any]:
    """Validate deterministic split trees for every scenario/service Loki query."""
    expected = {(scenario, service) for scenario in ("A", "B") for service in SERVICES}
    leaves_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    attempts_by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record, destination in [(item, leaves_by_key) for item in entries] + [
        (item, attempts_by_key) for item in attempts
    ]:
        key = (record.get("scenario"), record.get("service"))
        if key not in expected:
            raise CollectionError(f"invalid Loki query coverage key: {key}")
        destination.setdefault(key, []).append(record)

    roots = generate_log_windows(start_ns, end_ns, window_ns)
    coverage: dict[str, Any] = {}
    for key in sorted(expected):
        scenario, service = key
        leaves = leaves_by_key.get(key, [])
        splits = attempts_by_key.get(key, [])
        leaf_map = {(item.get("start_ns"), item.get("end_ns")): item for item in leaves}
        split_map = {(item.get("start_ns"), item.get("end_ns")): item for item in splits}
        if len(leaf_map) != len(leaves) or len(split_map) != len(splits):
            raise CollectionError(f"duplicate Loki query window for {scenario}/{service}")
        visited: set[tuple[int, int]] = set()
        accepted: list[dict[str, Any]] = []

        def visit(left: int, right: int, depth: int) -> None:
            bounds = (left, right)
            if bounds in visited:
                raise CollectionError(f"repeated Loki split node for {scenario}/{service}: {bounds}")
            visited.add(bounds)
            leaf = leaf_map.get(bounds)
            attempt = split_map.get(bounds)
            if leaf is not None and attempt is not None:
                raise CollectionError(f"Loki window both accepted and split: {scenario}/{service} {bounds}")
            if leaf is not None:
                count = leaf.get("entry_count")
                if (not isinstance(count, int) or count < 0 or count >= leaf.get("limit", 0)
                        or leaf.get("http_status") != 200 or leaf.get("split_depth") != depth
                        or leaf.get("start_ns") != left or leaf.get("end_ns") != right
                        or leaf.get("actual_sent_start") != str(left)
                        or leaf.get("actual_sent_end") != str(right)
                        or leaf.get("direction") != "forward"):
                    raise CollectionError(f"Loki leaf is incomplete for {scenario}/{service}: {leaf}")
                accepted.append(leaf)
                return
            if attempt is None:
                raise CollectionError(f"Loki split tree has a missing query node for {scenario}/{service}: {bounds}")
            if (attempt.get("http_status") != 200 or attempt.get("entry_count") != attempt.get("limit")
                    or attempt.get("split_depth") != depth
                    or attempt.get("actual_sent_start") != str(left)
                    or attempt.get("actual_sent_end") != str(right)
                    or attempt.get("direction") != "forward"):
                raise CollectionError(f"Loki split attempt has no saturation evidence: {attempt}")
            left_child, right_child = split_log_window(left, right)
            if attempt.get("split_into") != [list(left_child), list(right_child)]:
                raise CollectionError(f"Loki split is not deterministic for {scenario}/{service}: {attempt}")
            visit(*left_child, depth + 1)
            visit(*right_child, depth + 1)

        for left, right in roots:
            visit(left, right, 0)
        if visited != set(leaf_map) | set(split_map):
            raise CollectionError(f"Loki query has unreachable windows for {scenario}/{service}")
        if not accepted:
            raise CollectionError(f"no accepted Loki leaves for {scenario}/{service}")
        accepted.sort(key=lambda item: item["start_ns"])
        accepted_ranges = [(item["start_ns"], item["end_ns"]) for item in accepted]
        validate_log_interval_coverage(accepted_ranges, start_ns, end_ns)
        coverage[f"{scenario}/{service}"] = {
            "query_count": len(accepted),
            "split_attempts": len(splits),
            "covered_start_ns": accepted[0]["start_ns"],
            "covered_end_ns": accepted[-1]["end_ns"],
            "coverage": "CONTIGUOUS_HALF_OPEN_INTEGER_NANOSECONDS",
        }
    return coverage


def validate_log_artifact_references(root: Path, manifest: dict[str, Any],
                                    expected_hashes: dict[str, str]) -> None:
    leaves = manifest.get("log_queries", [])
    attempts = manifest.get("log_query_attempts", [])
    accepted_paths: set[str] = set()
    for record in leaves:
        relative = record.get("path")
        if (not isinstance(relative, str) or relative in accepted_paths
                or relative not in expected_hashes or expected_hashes[relative] != record.get("sha256")
                or not relative.startswith(f"dataset{record.get('scenario')}/logs/{record.get('service')}/chunk-")):
            raise CollectionError(f"Loki leaf chunk is missing or inconsistent: {record}")
        path = root / relative
        body = path.read_bytes()
        if len(body) != record.get("bytes") or sha256(body) != record.get("sha256"):
            raise CollectionError(f"Loki leaf exact-byte integrity mismatch: {relative}")
        parsed = json.loads(body)
        entry_count, stream_count = loki_entry_count(parsed)
        if (entry_count != record.get("entry_count") or stream_count != record.get("stream_count")
                or entry_count >= record.get("limit", 0)):
            raise CollectionError(f"Loki leaf counts are incomplete or inconsistent: {relative}")
        accepted_paths.add(relative)
    actual_chunks = {
        path.relative_to(root).as_posix()
        for scenario in ("A", "B")
        for path in (root / f"dataset{scenario}" / "logs").glob("*/*.json")
    }
    if actual_chunks != accepted_paths:
        raise CollectionError(
            "Loki chunk inventory differs from query manifest "
            f"(unlisted={sorted(actual_chunks - accepted_paths)}, missing={sorted(accepted_paths - actual_chunks)})"
        )
    controls = {item.get("path"): item for item in manifest.get("control_artifacts", [])}
    for record in attempts:
        relative = record.get("path")
        control = controls.get(relative)
        if (not isinstance(relative, str) or relative not in expected_hashes
                or expected_hashes[relative] != record.get("sha256")
                or not isinstance(control, dict) or control.get("model_visible") is not False
                or control.get("sha256") != record.get("sha256")
                or record.get("entry_count") != record.get("limit")):
            raise CollectionError(f"Loki saturated attempt is not control-only/integral: {record}")
        body = (root / relative).read_bytes()
        if len(body) != record.get("bytes") or sha256(body) != record.get("sha256"):
            raise CollectionError(f"Loki saturated attempt integrity mismatch: {relative}")
    window = manifest.get("window", {})
    start_ns, end_ns = window.get("loki_start_ns"), window.get("loki_end_ns")
    if not isinstance(start_ns, int) or not isinstance(end_ns, int):
        raise CollectionError("RAW manifest lacks exact integer-nanosecond Loki bounds")
    coverage = validate_log_query_coverage(leaves, start_ns, end_ns, attempts)
    if manifest.get("log_query_coverage") != coverage:
        raise CollectionError("Loki coverage summary differs from validated query tree")


def epoch_us(value: datetime) -> int:
    delta = value.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def trace_chunk_paths(root: Path, scenario: str) -> list[Path]:
    """Return immutable per-service Jaeger query chunks in deterministic order."""
    trace_dir = root / f"dataset{scenario}" / "traces"
    files = [path for path in trace_dir.glob("*.json")
             if path.is_file() and path.name != "services.json"]
    files.extend(path for path in trace_dir.glob("*/chunk-*.json") if path.is_file())
    return sorted(files)


def generate_trace_windows(start_us: int, end_us: int,
                           window_us: int = TRACE_WINDOW_US) -> list[tuple[int, int]]:
    """Partition an inclusive microsecond interval without gaps or overlaps."""
    if start_us > end_us:
        raise CollectionError("trace query start must not be after end")
    if window_us < 1:
        raise CollectionError("trace window width must be at least one microsecond")
    windows = []
    current = start_us
    while current <= end_us:
        last = min(current + window_us - 1, end_us)
        windows.append((current, last))
        current = last + 1
    return windows


def split_trace_window(start_us: int, end_us: int) -> tuple[tuple[int, int], tuple[int, int]]:
    if start_us >= end_us:
        raise CollectionError(
            f"Jaeger result may be truncated at minimum {MIN_TRACE_WINDOW_US}us resolution: "
            f"window=[{start_us},{end_us}]"
        )
    midpoint = (start_us + end_us) // 2
    return (start_us, midpoint), (midpoint + 1, end_us)


def validate_trace_window_coverage(entries: list[dict[str, Any]], start_us: int,
                                   end_us: int,
                                   attempts: list[dict[str, Any]] | None = None,
                                   window_us: int = TRACE_WINDOW_US) -> dict[str, Any]:
    """Validate every base window and deterministic split node has one complete leaf tree."""
    coverage: dict[str, Any] = {}
    expected = {(scenario, service) for scenario in ("A", "B") for service in SERVICES}
    grouped_leaves: dict[tuple[str, str], list[dict[str, Any]]] = {}
    grouped_attempts: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in entries:
        key = (entry.get("scenario"), entry.get("service"))
        if key not in expected:
            raise CollectionError(f"invalid Jaeger trace coverage key: {key}")
        grouped_leaves.setdefault(key, []).append(entry)
    for entry in attempts or []:
        key = (entry.get("scenario"), entry.get("service"))
        if key not in expected:
            raise CollectionError(f"invalid Jaeger trace attempt coverage key: {key}")
        grouped_attempts.setdefault(key, []).append(entry)
    roots = generate_trace_windows(start_us, end_us, window_us)
    for scenario, service in sorted(expected):
        key = (scenario, service)
        leaves = grouped_leaves.get(key, [])
        split_attempts = grouped_attempts.get(key, [])
        leaf_by_range = {(item.get("query_start_us"), item.get("query_end_us")): item for item in leaves}
        attempt_by_range = {(item.get("query_start_us"), item.get("query_end_us")): item
                            for item in split_attempts}
        if len(leaf_by_range) != len(leaves) or len(attempt_by_range) != len(split_attempts):
            raise CollectionError(f"duplicate Jaeger query window for {scenario}/{service}")
        visited: set[tuple[int, int]] = set()
        accepted: list[dict[str, Any]] = []

        def visit(left: int, right: int, depth: int) -> None:
            bounds = (left, right)
            if bounds in visited:
                raise CollectionError(f"repeated Jaeger split node for {scenario}/{service}: {bounds}")
            visited.add(bounds)
            leaf = leaf_by_range.get(bounds)
            attempt = attempt_by_range.get(bounds)
            if leaf is not None and attempt is not None:
                raise CollectionError(f"Jaeger window both accepted and split for {scenario}/{service}: {bounds}")
            if leaf is not None:
                count, limit = leaf.get("trace_count"), leaf.get("limit")
                if (not isinstance(count, int) or not isinstance(limit, int)
                        or count < 0 or limit < 1 or count >= limit
                        or leaf.get("http_status") != 200 or leaf.get("split_depth") != depth):
                    raise CollectionError(f"Jaeger trace leaf is incomplete for {scenario}/{service}: {leaf}")
                accepted.append(leaf)
                return
            if attempt is None:
                raise CollectionError(f"Jaeger split tree has a missing query node for {scenario}/{service}: {bounds}")
            count, limit, total = attempt.get("trace_count"), attempt.get("limit"), attempt.get("reported_total")
            if (attempt.get("http_status") != 200 or not isinstance(count, int)
                    or not isinstance(limit, int) or count < 0 or limit < 1 or count < limit and not (
                        isinstance(total, int) and total > count
                    ) or attempt.get("split_depth") != depth):
                raise CollectionError(f"Jaeger split attempt has no truncation evidence for {scenario}/{service}: {attempt}")
            if right - left + 1 <= MIN_TRACE_WINDOW_US:
                raise CollectionError(f"Jaeger minimum-window query cannot be proven complete for {scenario}/{service}: {bounds}")
            left_child, right_child = split_trace_window(left, right)
            if attempt.get("split_into") != [list(left_child), list(right_child)]:
                raise CollectionError(f"Jaeger split is not deterministic for {scenario}/{service}: {attempt}")
            visit(*left_child, depth + 1)
            visit(*right_child, depth + 1)

        for left, right in roots:
            visit(left, right, 0)
        if visited != set(leaf_by_range) | set(attempt_by_range):
            raise CollectionError(f"Jaeger trace query contains unreachable windows for {scenario}/{service}")
        if not accepted:
            raise CollectionError(f"no accepted Jaeger trace leaves for {scenario}/{service}")
        windows = sorted(accepted, key=lambda item: item["query_start_us"])
        coverage[f"{scenario}/{service}"] = {
            "query_count": len(windows),
            "covered_start_us": windows[0]["query_start_us"],
            "covered_end_us": windows[-1]["query_end_us"],
            "coverage": "CONTIGUOUS_INCLUSIVE_MICROSECONDS",
            "split_attempts": len(split_attempts),
        }
    return coverage


def validate_trace_artifact_references(root: Path, manifest: dict[str, Any],
                                       expected_hashes: dict[str, str]) -> None:
    """Cross-check accepted/split query metadata against immutable files and inventory."""
    accepted = manifest.get("trace_queries", [])
    attempts = manifest.get("trace_query_attempts", [])
    accepted_paths: set[str] = set()
    attempt_paths: set[str] = set()
    for record in accepted:
        relative = record.get("path")
        if (not isinstance(relative, str) or relative in accepted_paths
                or relative not in expected_hashes
                or not relative.startswith(f"dataset{record.get('scenario')}/traces/{record.get('service')}/chunk-")
                or expected_hashes[relative] != record.get("sha256")):
            raise CollectionError(f"Jaeger query chunk is missing or inconsistent with manifest: {record}")
        path = root / relative
        if path.stat().st_size != record.get("bytes"):
            raise CollectionError(f"Jaeger query chunk byte count differs from manifest: {relative}")
        try:
            parsed = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError) as exc:
            raise CollectionError(f"Jaeger query chunk is invalid JSON: {relative}: {exc}") from exc
        traces = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(traces, list) or len(traces) != record.get("trace_count"):
            raise CollectionError(f"Jaeger query chunk trace count differs from manifest: {relative}")
        accepted_paths.add(relative)
    actual_chunks = {path.relative_to(root).as_posix()
                     for scenario in ("A", "B") for path in trace_chunk_paths(root, scenario)}
    if accepted_paths != actual_chunks:
        raise CollectionError(
            "Jaeger chunk inventory differs from query manifest "
            f"(unlisted={sorted(actual_chunks - accepted_paths)}, missing={sorted(accepted_paths - actual_chunks)})"
        )
    control_paths = {entry.get("path"): entry for entry in manifest.get("control_artifacts", [])}
    for record in attempts:
        relative = record.get("path")
        control = control_paths.get(relative)
        if (not isinstance(relative, str) or relative in attempt_paths
                or relative not in expected_hashes or expected_hashes[relative] != record.get("sha256")
                or not isinstance(control, dict) or control.get("model_visible") is not False
                or control.get("sha256") != record.get("sha256")
                or record.get("kind") != "jaeger-truncated-query-attempt"):
            raise CollectionError(f"Jaeger split attempt is missing from control inventory: {record}")
        if (root / relative).stat().st_size != record.get("bytes"):
            raise CollectionError(f"Jaeger split attempt byte count differs from manifest: {relative}")
        attempt_paths.add(relative)
    declared_attempt_paths = {
        entry.get("path") for entry in manifest.get("control_artifacts", [])
        if entry.get("kind") == "jaeger-truncated-query-attempt"
    }
    if attempt_paths != declared_attempt_paths:
        raise CollectionError("Jaeger split attempts and control artifact manifest differ")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_bytes(url: str, timeout: float = 30) -> tuple[int, bytes]:
    try:
        with urlopen(url, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        body = exc.read()
        raise BackendRequestError(
            f"HTTP {exc.code} from {url}: {body[:500]!r}",
            http_status=exc.code, response_bytes=len(body),
        ) from exc
    except (URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        raise BackendRequestError(f"request failed for {url}: {exc}") from exc


def query_url(base: str, path: str, params: dict[str, str | int]) -> str:
    return f"{base.rstrip('/')}{path}?{urlencode(params)}"


def write_native_bytes(root: Path, relative: str, body: bytes) -> None:
    output = root / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(body)
    output.chmod(0o444)


def trace_query_event(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, **values}, sort_keys=True), file=sys.stderr, flush=True)


def collect_trace_service(
    *, root: Path, scenario: str, service: str, query_service: str, base: str,
    start_us: int, end_us: int, limit: int, native_responses: list[dict[str, Any]],
    control_artifacts: list[dict[str, Any]], trace_queries: list[dict[str, Any]],
    trace_query_attempts: list[dict[str, Any]],
    transport_state: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fetch a service's inclusive interval into immutable 30s JSON leaves."""
    attempt_sequence = len(trace_queries) + len(trace_query_attempts)
    chunk_sequence = 0
    service_trace_ids: set[str] = set()
    service_span_ids: set[tuple[str, str]] = set()
    trace_occurrences = 0
    span_occurrences = 0

    def fetch(left: int, right: int, depth: int) -> None:
        nonlocal attempt_sequence, chunk_sequence, trace_occurrences, span_occurrences
        params = {
            "service": query_service,
            "start": left,
            "end": right,
            "limit": limit,
        }
        url = query_url(base, "/api/traces", params)
        query_id = f"{scenario}/{service}/[{left},{right}]"
        started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        started_monotonic = time.monotonic()
        transport = transport_state() if transport_state else None
        trace_query_event(
            "TRACE_QUERY_START", scenario=scenario, service=service,
            query_service=query_service, window_start_us=left, window_end_us=right,
            limit=limit, split_depth=depth, query_id=query_id, request_url=url,
            query_started_at=started_at, port_forward_state=transport,
        )
        status: int | None = None
        body = b""
        try:
            status, body = request_bytes(url, timeout=90)
            parsed = json.loads(body)
            if not isinstance(parsed, dict) or parsed.get("status") not in (None, "success"):
                raise CollectionError(f"Jaeger response is not successful JSON for {query_id}")
            if parsed.get("errors"):
                raise CollectionError(f"Jaeger returned errors for {query_id}: {parsed['errors']}")
            traces = parsed.get("data")
            if not isinstance(traces, list):
                raise CollectionError(f"Jaeger response has no trace array for {query_id}")
            if any(not isinstance(trace, dict) or not isinstance(trace.get("spans", []), list)
                   for trace in traces):
                raise CollectionError(f"Jaeger response contains malformed trace records for {query_id}")
        except Exception as exc:
            elapsed = round(time.monotonic() - started_monotonic, 6)
            trace_query_event(
                "TRACE_QUERY_FAILED", scenario=scenario, service=service,
                window_start_us=left, window_end_us=right, limit=limit,
                split_depth=depth, query_id=query_id, elapsed_seconds=elapsed,
                http_status=status or getattr(exc, "http_status", None),
                response_bytes=(len(body) if body else getattr(exc, "response_bytes", None)),
                exception_type=type(exc).__name__, error=str(exc),
                port_forward_state=transport_state() if transport_state else transport,
            )
            raise CollectionError(f"trace query failed ({query_id}): {exc}") from exc

        elapsed = round(time.monotonic() - started_monotonic, 6)
        total = parsed.get("total")
        possible_truncation = len(traces) >= limit or (isinstance(total, int) and total > len(traces))
        event_result = "POSSIBLE_TRUNCATION_SPLIT" if possible_truncation else "ACCEPTED"

        if possible_truncation:
            attempt_relative = (
                f"control/trace-query-attempts/{scenario}/{service}/"
                f"attempt-{attempt_sequence:06d}.json"
            )
            attempt_sequence += 1
            write_native_bytes(root, attempt_relative, body)
            attempt = {
                "path": attempt_relative,
                "kind": "jaeger-truncated-query-attempt",
                "model_visible": False,
                "content_type": "application/json",
                "bytes": len(body),
                "sha256": sha256(body),
                "query_id": query_id,
                "scenario": scenario,
                "service": service,
                "query_start_us": left,
                "query_end_us": right,
                "limit": limit,
                "http_status": status,
                "trace_count": len(traces),
                "reported_total": total,
                "elapsed_seconds": elapsed,
                "query_started_at": started_at,
                "split_depth": depth,
            }
            control_artifacts.append(attempt)
            trace_query_attempts.append(attempt)
            if right - left + 1 <= MIN_TRACE_WINDOW_US:
                trace_query_event(
                    "TRACE_QUERY_FAILED", scenario=scenario, service=service,
                    window_start_us=left, window_end_us=right, limit=limit,
                    split_depth=depth, query_id=query_id, http_status=status,
                    response_bytes=len(body), trace_count=len(traces), reported_total=total,
                    elapsed_seconds=elapsed, exception_type="MinimumWindowTruncation",
                    error="result may be truncated at minimum 1us resolution",
                )
                raise CollectionError(f"Jaeger result remains truncated at minimum window for {query_id}")
            left_window, right_window = split_trace_window(left, right)
            attempt["split_into"] = [list(left_window), list(right_window)]
            trace_query_event(
                "TRACE_QUERY_COMPLETE", scenario=scenario, service=service,
                window_start_us=left, window_end_us=right, limit=limit,
                split_depth=depth, query_id=query_id, http_status=status,
                response_bytes=len(body), trace_count=len(traces), reported_total=total,
                elapsed_seconds=elapsed, result=event_result,
                split_into=[left_window, right_window], raw_path=attempt_relative,
            )
            fetch(*left_window, depth + 1)
            fetch(*right_window, depth + 1)
            return

        relative = f"dataset{scenario}/traces/{service}/chunk-{chunk_sequence:06d}.json"
        chunk_sequence += 1
        write_native_bytes(root, relative, body)
        record = {
            "path": relative,
            "query_kind": "jaeger-traces-by-service-window",
            "request_url": url,
            "http_status": status,
            "content_type": "application/json",
            "bytes": len(body),
            "sha256": sha256(body),
            "scenario": scenario,
            "service": service,
            "query_start_us": left,
            "query_end_us": right,
            "limit": limit,
            "trace_count": len(traces),
            "reported_total": total,
            "span_count": sum(len(trace.get("spans", [])) for trace in traces),
            "elapsed_seconds": elapsed,
            "query_started_at": started_at,
            "query_completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "split_depth": depth,
        }
        native_responses.append(record)
        trace_queries.append(record)
        for trace in traces:
            trace_id = str(trace.get("traceID") or "")
            if trace_id:
                service_trace_ids.add(trace_id)
            trace_occurrences += 1
            for span in trace.get("spans", []):
                span_id = str(span.get("spanID") or "")
                if trace_id and span_id:
                    service_span_ids.add((trace_id, span_id))
                span_occurrences += 1
        trace_query_event(
            "TRACE_QUERY_COMPLETE", scenario=scenario, service=service,
            window_start_us=left, window_end_us=right, limit=limit,
            split_depth=depth, query_id=query_id, http_status=status,
            response_bytes=len(body), trace_count=len(traces), reported_total=total,
            span_count=record["span_count"], elapsed_seconds=elapsed,
            sha256=record["sha256"], result=event_result, raw_path=relative,
        )

    for window_start, window_end in generate_trace_windows(start_us, end_us):
        fetch(window_start, window_end, 0)

    return {
        "scenario": scenario,
        "service": service,
        "query_service": query_service,
        "query_count": sum(1 for item in trace_queries
                           if item.get("scenario") == scenario and item.get("service") == service),
        "trace_occurrences": trace_occurrences,
        "unique_trace_ids": len(service_trace_ids),
        "duplicate_trace_occurrences": trace_occurrences - len(service_trace_ids),
        "span_occurrences": span_occurrences,
        "unique_spans": len(service_span_ids),
        "duplicate_span_occurrences": span_occurrences - len(service_span_ids),
        "trace_ids_sha256": sha256("\n".join(sorted(service_trace_ids)).encode()),
        "span_ids_sha256": sha256("\n".join(f"{trace_id}/{span_id}"
                                              for trace_id, span_id in sorted(service_span_ids)).encode()),
        "accepted_chunks": chunk_sequence,
    }


def save_native(
    root: Path,
    relative: str,
    url: str,
    query_kind: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    query_started = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    parsed_url = urlparse(url)
    query = parse_qs(parsed_url.query)
    scenario = "A" if relative.startswith("datasetA/") else "B" if relative.startswith("datasetB/") else "unknown"
    service = Path(relative).stem if "traces/" in relative or "/logs/" in relative else None
    details = {
        "event": "raw_backend_query",
        "scenario": scenario,
        "backend": query_kind,
        "service": service,
        "started_at": query_started.isoformat().replace("+00:00", "Z"),
        "request_path": parsed_url.path,
        "start": (query.get("start") or query.get("startTs") or [None])[0],
        "end": (query.get("end") or query.get("endTs") or [None])[0],
        "limit": (query.get("limit") or [None])[0],
    }
    try:
        status, body = request_bytes(url)
    except CollectionError as exc:
        details.update({
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "elapsed_seconds": round(time.monotonic() - started_monotonic, 6),
            "http_status": None,
            "response_bytes": None,
            "result": "FAIL",
            "error": str(exc),
        })
        print(json.dumps(details, sort_keys=True), file=sys.stderr, flush=True)
        raise
    details.update({
        "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "elapsed_seconds": round(time.monotonic() - started_monotonic, 6),
        "http_status": status,
        "response_bytes": len(body),
        "result": "RESPONSE_RECEIVED",
    })
    if query_kind == "jaeger-traces-by-service":
        print(json.dumps(details, sort_keys=True), file=sys.stderr, flush=True)
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CollectionError(f"backend returned non-JSON response for {relative}") from exc
    if status != 200:
        raise CollectionError(f"HTTP {status} for {relative}")
    if isinstance(parsed, dict) and parsed.get("status") not in (None, "success"):
        raise CollectionError(f"backend query failed for {relative}: {parsed.get('error', parsed.get('status'))}")
    if isinstance(parsed, dict) and parsed.get("errors"):
        raise CollectionError(f"backend reported query errors for {relative}: {parsed['errors']}")
    output = root / relative
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(body)
    output.chmod(0o444)
    entry = {
        "path": relative,
        "query_kind": query_kind,
        "request_url": url,
        "http_status": status,
        "content_type": "application/json",
        "bytes": len(body),
        "sha256": sha256(body),
    }
    records.append(entry)
    return parsed


def collect_loki_service(
    *, root: Path, scenario: str, service: str, base: str, start_ns: int, end_ns: int,
    limit: int, native_responses: list[dict[str, Any]], control_artifacts: list[dict[str, Any]],
    log_queries: list[dict[str, Any]], log_query_attempts: list[dict[str, Any]],
    window_ns: int = LOG_WINDOW_NS,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    selector = f'{{service_name="{service}"}}'
    if scenario == "A":
        selector = f'{{namespace="online-boutique",service_name="{service}"}}'
    accepted: list[dict[str, Any]] = []
    parsed_leaves: list[dict[str, Any]] = []
    sequence = 0
    split_count = 0
    max_depth = 0

    def query(left: int, right: int, depth: int) -> None:
        nonlocal sequence, split_count, max_depth
        max_depth = max(max_depth, depth)
        params = {"query": selector, "start": str(left), "end": str(right),
                  "direction": "forward", "limit": str(limit)}
        url = query_url(base, "/loki/api/v1/query_range", params)
        started = time.monotonic()
        event = {"event": "LOKI_QUERY_START", "scenario": scenario, "service": service,
                 "start_ns": left, "end_ns": right, "actual_sent_start": str(left),
                 "actual_sent_end": str(right), "direction": "forward", "limit": limit,
                 "split_depth": depth}
        print(json.dumps(event, sort_keys=True), file=sys.stderr, flush=True)
        try:
            status, body = request_bytes(url)
            parsed = json.loads(body)
            if status != 200:
                raise CollectionError(f"HTTP {status}")
            if not isinstance(parsed, dict) or parsed.get("status") not in (None, "success"):
                raise CollectionError(f"Loki query status is invalid: {parsed!r}")
            if parsed.get("errors"):
                raise CollectionError(f"Loki reported query errors: {parsed['errors']}")
            entry_count, stream_count = loki_entry_count(parsed)
        except (CollectionError, json.JSONDecodeError) as exc:
            print(json.dumps({**event, "event": "LOKI_QUERY_FAILED", "exception": str(exc),
                              "elapsed_ms": round((time.monotonic() - started) * 1000, 3)},
                             sort_keys=True), file=sys.stderr, flush=True)
            raise CollectionError(f"Loki query failed for {scenario}/{service} [{left},{right}): {exc}") from exc

        elapsed_ms = round((time.monotonic() - started) * 1000, 3)
        digest = sha256(body)
        common = {"scenario": scenario, "service": service, "selector": selector,
                  "start_ns": left, "end_ns": right, "actual_sent_start": str(left),
                  "actual_sent_end": str(right), "direction": "forward", "limit": limit,
                  "entry_count": entry_count, "stream_count": stream_count,
                  "response_bytes": len(body), "sha256": digest, "split_depth": depth,
                  "elapsed_ms": elapsed_ms, "http_status": status}
        if entry_count == limit:
            relative = (f"control/loki-query-attempts/{scenario}/{service}/"
                        f"attempt-{sequence:06d}.json")
            sequence += 1
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            target.chmod(0o444)
            left_child, right_child = split_log_window(left, right)
            attempt = {**common, "kind": "loki-saturated-query-attempt", "path": relative,
                       "bytes": len(body), "split_into": [list(left_child), list(right_child)]}
            log_query_attempts.append(attempt)
            control_artifacts.append({"path": relative, "kind": attempt["kind"],
                                      "content_type": "application/json", "bytes": len(body),
                                      "sha256": digest, "model_visible": False})
            print(json.dumps({**event, "event": "LOKI_QUERY_SATURATED", "entries": entry_count,
                              "streams": stream_count, "bytes": len(body), "elapsed_ms": elapsed_ms},
                             sort_keys=True), file=sys.stderr, flush=True)
            split_count += 1
            query(*left_child, depth + 1)
            query(*right_child, depth + 1)
            return

        relative = f"dataset{scenario}/logs/{service}/chunk-{len(accepted):06d}.json"
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        target.chmod(0o444)
        record = {**common, "path": relative, "bytes": len(body), "query_kind": "loki-logs-by-service-window"}
        accepted.append(record)
        native_responses.append({"path": relative, "query_kind": record["query_kind"],
                                 "request_url": url, "http_status": status,
                                 "content_type": "application/json", "bytes": len(body), "sha256": digest})
        log_queries.append(record)
        parsed_leaves.append(parsed)
        print(json.dumps({**event, "event": "LOKI_QUERY_COMPLETE", "status": status,
                          "entries": entry_count, "streams": stream_count, "bytes": len(body),
                          "sha256": digest, "elapsed_ms": elapsed_ms},
                         sort_keys=True), file=sys.stderr, flush=True)

    for left, right in generate_log_windows(start_ns, end_ns, window_ns):
        query(left, right, 0)
    return parsed_leaves, {"query_count": len(accepted), "split_count": split_count,
                           "max_split_depth": max_depth,
                           "entry_count": sum(item["entry_count"] for item in accepted),
                           "max_leaf_entries": max(item["entry_count"] for item in accepted),
                           "max_response_bytes": max(item["response_bytes"] for item in accepted)}


def readiness(url: str, deadline: float) -> None:
    while time.monotonic() < deadline:
        try:
            status, _ = request_bytes(url, timeout=1)
            if status == 200:
                return
        except CollectionError:
            time.sleep(0.2)
    raise CollectionError(f"port-forward/backend did not become ready: {url}")


def reserve_local_port() -> int:
    """Choose an ephemeral loopback port instead of trusting a shared fixed port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def forwarding_confirmed(output: str, local_port: int, remote_port: int) -> bool:
    escaped_local = re.escape(str(local_port))
    escaped_remote = re.escape(str(remote_port))
    return bool(re.search(
        rf"Forwarding from (?:127\.0\.0\.1|\[::1\]):{escaped_local} -> {escaped_remote}(?:\s|$)",
        output,
    ))


def wait_for_forward(
    process: subprocess.Popen[bytes],
    output_file: Any,
    local_port: int,
    remote_port: int,
    deadline: float,
) -> str:
    while time.monotonic() < deadline:
        output_file.flush()
        output_file.seek(0)
        output = output_file.read()
        if forwarding_confirmed(output, local_port, remote_port):
            if process.poll() is not None:
                raise CollectionError(f"kubectl port-forward exited after binding port {local_port}: {output[-1000:]}")
            return output
        exit_code = process.poll()
        if exit_code is not None:
            raise CollectionError(
                f"kubectl port-forward exited ({exit_code}) before confirming "
                f"127.0.0.1:{local_port} -> {remote_port}: {output[-1000:]}"
            )
        time.sleep(0.1)
    output_file.flush()
    output_file.seek(0)
    output = output_file.read()
    raise CollectionError(
        f"kubectl port-forward did not confirm 127.0.0.1:{local_port} -> {remote_port}: {output[-1000:]}"
    )


def verify_bundle(root: Path) -> int:
    manifest_path = root / "manifest.json"
    hashes_path = root / "hashes.sha256"
    if not manifest_path.is_file() or not hashes_path.is_file():
        raise CollectionError("bundle is missing manifest.json or hashes.sha256")
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionError(f"bundle has invalid manifest.json: {exc}") from exc
    expected: dict[str, str] = {}
    for line in hashes_path.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        expected[relative] = digest
    failures = []
    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != hashes_path
    }
    if actual_files != set(expected):
        failures.append(
            "bundle file inventory differs from hashes.sha256 "
            f"(unlisted={sorted(actual_files - set(expected))}, missing={sorted(set(expected) - actual_files)})"
        )
    for relative, digest in expected.items():
        path = root / relative
        if not path.is_file() or sha256(path.read_bytes()) != digest or (path.stat().st_mode & 0o222):
            failures.append(relative)
    for path in (root, *[p for p in root.rglob("*") if p.is_dir()]):
        if path.stat().st_mode & 0o222:
            failures.append(f"writable directory: {path.relative_to(root.parent)}")
    if failures:
        print(json.dumps({"status": "FAIL", "mismatches": failures}, indent=2))
        return 1
    if manifest.get("jaeger_query_strategy"):
        try:
            validate_trace_window_coverage(
                manifest.get("trace_queries", []),
                int(manifest["window"]["jaeger_start_us"]),
                int(manifest["window"]["jaeger_end_us"]),
                manifest.get("trace_query_attempts", []),
            )
            validate_trace_artifact_references(root, manifest, expected)
        except (KeyError, TypeError, ValueError, RuntimeError) as exc:
            print(json.dumps({"status": "FAIL", "mismatches": [f"Jaeger trace manifest: {exc}"]}, indent=2))
            return 1
    if manifest.get("loki_query_strategy"):
        try:
            validate_log_artifact_references(root, manifest, expected)
        except (KeyError, TypeError, ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"status": "FAIL", "mismatches": [f"Loki log manifest: {exc}"]}, indent=2))
            return 1
    print(json.dumps({"status": "PASS", "files_verified": len(expected)}, indent=2))
    return 0


def collect(args: argparse.Namespace) -> int:
    start = parse_time(args.start)
    end = parse_time(args.end)
    start_ns = args.start_ns if args.start_ns is not None else epoch_ns(start)
    end_ns = args.end_ns if args.end_ns is not None else epoch_ns(end)
    if (args.start_ns is None) != (args.end_ns is None):
        raise CollectionError("--start-ns and --end-ns must be provided together")
    if start >= end or start_ns >= end_ns:
        raise CollectionError("window start must precede window end")
    root = args.output_dir
    if root.exists():
        raise CollectionError(f"output directory already exists (write-once): {root}")
    root.mkdir(parents=True)

    port_forwards: list[tuple[subprocess.Popen[bytes], Any]] = []
    forward_processes: dict[str, subprocess.Popen[bytes]] = {}
    ports: dict[str, int] = {}
    forward_manifest: list[dict[str, Any]] = []
    try:
        for name, namespace, service, remote_port, _ in PORT_FORWARDS:
            local_port = reserve_local_port()
            output_file = tempfile.TemporaryFile(mode="w+t", encoding="utf-8")
            process = subprocess.Popen(
                [
                    args.kubectl,
                    "--context",
                    args.context,
                    "--namespace",
                    namespace,
                    "port-forward",
                    f"service/{service}",
                    f"{local_port}:{remote_port}",
                ],
                stdout=output_file,
                stderr=subprocess.STDOUT,
                text=True,
            )
            port_forwards.append((process, output_file))
            wait_for_forward(process, output_file, local_port, remote_port, time.monotonic() + 20)
            forward_processes[name] = process
            ports[name] = local_port
            forward_manifest.append({
                "name": name,
                "namespace": namespace,
                "service": service,
                "local_address": f"127.0.0.1:{local_port}",
                "remote_port": remote_port,
                "process_id": process.pid,
                "status": "BOUND_TO_EXPECTED_SERVICE_PORT",
            })

        for name, _, _, _, health_path in PORT_FORWARDS:
            process = forward_processes[name]
            if process.poll() is not None:
                raise CollectionError(f"port-forward {name} exited before backend readiness")
            local_port = ports[name]
            readiness(f"http://127.0.0.1:{local_port}{health_path}", time.monotonic() + 20)
            if process.poll() is not None:
                raise CollectionError(f"port-forward {name} exited during backend readiness")

        if args.drain_seconds:
            time.sleep(args.drain_seconds)

        window_start = format_epoch_ns(start_ns)
        window_end = format_epoch_ns(end_ns)
        start_us = epoch_us(start)
        end_us = epoch_us(end)
        if start_us > end_us:
            raise CollectionError("experiment interval collapses or reverses at Jaeger microsecond precision")
        manifest: dict[str, Any] = {
            "format": "tcc-experiment-v2-raw-collection-v3",
            "collected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "window": {"start": window_start, "end": window_end,
                       "loki_start_ns": start_ns, "loki_end_ns": end_ns,
                       "jaeger_start_us": start_us, "jaeger_end_us": end_us,
                       "jaeger_bounds": "inclusive"},
            "drain_seconds_before_queries": args.drain_seconds,
            "jaeger_query_strategy": {
                "api": "Jaeger HTTP JSON /api/traces",
                "base_window_microseconds": TRACE_WINDOW_US,
                "base_window_seconds": TRACE_WINDOW_US / 1_000_000,
                "limit": args.trace_limit,
                "boundary_semantics": "inclusive microsecond bounds; adjacent interval begins previous end + 1us",
                "split_policy": "inclusive integer-microsecond midpoint; recursively split any result at or above limit or with total greater than returned count",
                "minimum_window_microseconds": MIN_TRACE_WINDOW_US,
                "minimum_window_limit_behavior": "FAIL_CLOSED",
                "retry": False,
            },
            "loki_query_strategy": {
                "api": "Loki HTTP /loki/api/v1/query_range",
                "version": "3.5.5",
                "interval": "half-open [start_ns,end_ns)",
                "arithmetic": "integer nanoseconds only",
                "base_window_ns": LOG_WINDOW_NS,
                "direction": "forward",
                "limit": args.limit,
                "split_policy": "integer nanosecond midpoint; recursively split every result reaching limit",
                "minimum_window_ns": MIN_LOG_WINDOW_NS,
                "minimum_window_limit_behavior": "FAIL_CLOSED",
                "retry": False,
            },
            "port_forwards": forward_manifest,
            "collector_git_commit": args.collector_commit,
            "collector_script_sha256": sha256(Path(__file__).read_bytes()),
            "application_image_source_fingerprint": args.application_image_source_fingerprint,
            "workload_record": None,
            "native_responses": [],
            "control_artifacts": [],
            "trace_query_attempts": [],
            "trace_queries": [],
            "log_query_attempts": [],
            "log_queries": [],
            "notes": [
                "Response bodies are stored as received; no normalization, filtering, or cross-scenario equalization is applied.",
                "Jaeger window responses are stored as immutable native chunks; inclusive-window overlap is retained in RAW and logically deduplicated downstream with source provenance.",
                "A query reaching the trace limit is preserved as a non-model-visible control attempt and split deterministically; it is never accepted as a complete RAW chunk.",
                "hashes.sha256 includes manifest.json and all raw response files; hashes.sha256 does not hash itself.",
            ],
        }
        if args.workload_record:
            if not args.workload_record.is_file():
                raise CollectionError(f"workload record not found: {args.workload_record}")
            workload_relative = "control/workload.json"
            workload_target = root / workload_relative
            workload_target.parent.mkdir(parents=True, exist_ok=True)
            workload_bytes = args.workload_record.read_bytes()
            workload_target.write_bytes(workload_bytes)
            workload_target.chmod(0o444)
            manifest["workload_record"] = {
                "path": workload_relative,
                "bytes": len(workload_bytes),
                "sha256": sha256(workload_bytes),
            }
            manifest["control_artifacts"].append({
                "path": workload_relative,
                "kind": "workload-control-record",
                "content_type": "application/json",
                "bytes": len(workload_bytes),
                "sha256": sha256(workload_bytes),
            })
        specs = (
            ("A", "prometheus", f"http://127.0.0.1:{ports['prometheus-a']}", "/api/v1/query_range", {
                "query": '{__name__=~".+"}', "start": str(start.timestamp()),
                "end": str(end.timestamp()), "step": args.step,
            }, "metrics.json"),
            ("B", "prometheus", f"http://127.0.0.1:{ports['prometheus-b']}", "/api/v1/query_range", {
                "query": '{__name__=~".+"}', "start": str(start.timestamp()),
                "end": str(end.timestamp()), "step": args.step,
            }, "metrics.json"),
        )
        parsed_responses: dict[str, dict[str, Any]] = {}
        parsed_log_responses: dict[str, list[dict[str, Any]]] = {}
        for scenario, backend, base, path, params, filename in specs:
            url = query_url(base, path, params)
            parsed_responses[f"{scenario}/{backend}"] = save_native(
                root, f"dataset{scenario}/{filename}", url, backend, manifest["native_responses"]
            )

        def jaeger_forward_state(scenario: str) -> dict[str, Any]:
            name = f"jaeger-{scenario.lower()}"
            process = forward_processes[name]
            return {
                "name": name,
                "pid": process.pid,
                "local_address": f"127.0.0.1:{ports[name]}",
                "running": process.poll() is None,
                "returncode": process.poll(),
            }

        for scenario in ("A", "B"):
            base = f"http://127.0.0.1:{ports[f'loki-{scenario.lower()}']}"
            manifest.setdefault("log_counts", {})[scenario] = {}
            manifest.setdefault("log_query_summaries", {})[scenario] = {}
            for service in SERVICES:
                result, query_summary = collect_loki_service(
                    root=root, scenario=scenario, service=service, base=base,
                    start_ns=start_ns, end_ns=end_ns, limit=args.limit,
                    native_responses=manifest["native_responses"],
                    control_artifacts=manifest["control_artifacts"],
                    log_queries=manifest["log_queries"],
                    log_query_attempts=manifest["log_query_attempts"],
                )
                entries = query_summary["entry_count"]
                manifest["log_query_summaries"][scenario][service] = query_summary
                manifest["log_counts"][scenario][service] = entries
                parsed_log_responses[f"{scenario}/loki/{service}"] = result

        manifest["log_query_coverage"] = validate_log_query_coverage(
            manifest["log_queries"], start_ns, end_ns, manifest["log_query_attempts"]
        )
        validate_log_artifact_references(root, manifest, {
            path.relative_to(root).as_posix(): sha256(path.read_bytes())
            for path in root.rglob("*") if path.is_file()
        })

        for scenario in ("A", "B"):
            base = f"http://127.0.0.1:{ports[f'jaeger-{scenario.lower()}']}"
            service_url = f"{base}/api/services"
            services = save_native(
                root,
                f"dataset{scenario}/traces/services.json",
                service_url,
                "jaeger-service-discovery",
                manifest["native_responses"],
            )
            available = set(services.get("data", []))
            manifest.setdefault("trace_service_resolution", {})[scenario] = {}
            manifest.setdefault("trace_counts", {})[scenario] = {}
            manifest.setdefault("trace_query_summaries", {})[scenario] = {}
            for service in SERVICES:
                query_service = service if service in available else f"unknown_service:{service}"
                manifest["trace_service_resolution"][scenario][service] = {
                    "service_discovery_present": service in available,
                    "query_service": query_service,
                }
                summary = collect_trace_service(
                    root=root,
                    scenario=scenario,
                    service=service,
                    query_service=query_service,
                    base=base,
                    start_us=start_us,
                    end_us=end_us,
                    limit=args.trace_limit,
                    native_responses=manifest["native_responses"],
                    control_artifacts=manifest["control_artifacts"],
                    trace_queries=manifest["trace_queries"],
                    trace_query_attempts=manifest["trace_query_attempts"],
                    transport_state=lambda scenario=scenario: jaeger_forward_state(scenario),
                )
                manifest["trace_query_summaries"][scenario][service] = summary
                manifest["trace_counts"][scenario][service] = summary["unique_trace_ids"]

        manifest["trace_query_coverage"] = validate_trace_window_coverage(
            manifest["trace_queries"], start_us, end_us,
            manifest["trace_query_attempts"],
        )
        validate_trace_artifact_references(root, manifest, {
            path.relative_to(root).as_posix(): sha256(path.read_bytes())
            for path in root.rglob("*") if path.is_file()
        })
        for scenario in ("A", "B"):
            for service in SERVICES:
                key = f"{scenario}/{service}"
                if key not in manifest["trace_query_coverage"]:
                    raise CollectionError(f"global trace coverage is missing {key}")

        summary = summarize(parsed_responses, parsed_log_responses)
        manifest["signal_summary"] = summary
        for scenario in ("A", "B"):
            if summary[scenario]["metric_series"] == 0:
                raise CollectionError(f"Scenario {scenario} returned no metric series in the requested window")
            if summary[scenario]["log_entries"] == 0:
                raise CollectionError(f"Scenario {scenario} returned no logs in the requested window")
            trace_counts = manifest.get("trace_counts", {}).get(scenario, {})
            if not any(trace_counts.values()):
                raise CollectionError(f"Scenario {scenario} returned no application traces in the requested window")

        files = sorted(path for path in root.rglob("*") if path.is_file())
        hashes_lines = [f"{sha256(path.read_bytes())}  {path.relative_to(root).as_posix()}" for path in files]
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_hash = sha256((root / "manifest.json").read_bytes())
        hashes_lines.append(f"{manifest_hash}  manifest.json")
        hashes_lines.sort()
        hashes_file = root / "hashes.sha256"
        hashes_file.write_text("\n".join(hashes_lines) + "\n", encoding="utf-8")
        for path in root.rglob("*"):
            if path.is_file():
                path.chmod(0o444)
        for path in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            path.chmod(0o555)
        root.chmod(0o555)
        print(json.dumps({"status": "PASS", "output_dir": str(root), "summary": summary, "hashes": len(hashes_lines)}, indent=2))
        return 0
    finally:
        for process, _ in port_forwards:
            process.terminate()
        for process, output_file in port_forwards:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            output_file.close()


def summarize(responses: dict[str, dict[str, Any]],
              log_responses: dict[str, list[dict[str, Any]]] | None = None) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for scenario in ("A", "B"):
        metric = responses[f"{scenario}/prometheus"].get("data", {}).get("result", [])
        loki = [stream for service in SERVICES
                for response in (log_responses or {}).get(f"{scenario}/loki/{service}", [])
                for stream in response.get("data", {}).get("result", [])]
        summary[scenario] = {
            "metric_series": len(metric),
            "log_streams": len(loki),
            "log_entries": sum(len(stream.get("values", [])) for stream in loki),
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--start", help="RFC3339/ISO timestamp with timezone")
    parser.add_argument("--end", help="RFC3339/ISO timestamp with timezone")
    parser.add_argument("--start-ns", type=int, help="exact Loki interval start as integer Unix nanoseconds")
    parser.add_argument("--end-ns", type=int, help="exact Loki interval end as integer Unix nanoseconds")
    parser.add_argument("--context", default="kind-tcc-experiment-v2-stage1")
    parser.add_argument("--kubectl", default=os.environ.get("KUBECTL_BIN", "kubectl"))
    parser.add_argument("--collector-commit")
    parser.add_argument("--application-image-source-fingerprint")
    parser.add_argument("--workload-record", type=Path)
    parser.add_argument("--step", default="1s")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--trace-limit", type=int, default=10000)
    parser.add_argument("--drain-seconds", type=float, default=10)
    parser.add_argument("--verify-existing", type=Path)
    args = parser.parse_args()
    if args.verify_existing:
        return verify_bundle(args.verify_existing)
    if not args.output_dir or not args.start or not args.end or not args.collector_commit or not args.application_image_source_fingerprint:
        parser.error("collection requires --output-dir, --start, --end, --collector-commit, and --application-image-source-fingerprint")
    if (args.start_ns is None) != (args.end_ns is None):
        parser.error("--start-ns and --end-ns must be provided together")
    if args.limit < 1 or args.trace_limit != 10000 or args.drain_seconds < 0:
        parser.error("--limit must be positive, --trace-limit must remain 10000, and drain-seconds must be non-negative")
    try:
        return collect(args)
    except (CollectionError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"RAW collection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
