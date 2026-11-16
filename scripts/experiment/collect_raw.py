#!/usr/bin/env python3
"""Collect native Scenario A/B backend responses into a write-once RAW bundle."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
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


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def epoch_ns(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


def epoch_us(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_bytes(url: str, timeout: float = 30) -> tuple[int, bytes]:
    try:
        with urlopen(url, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        body = exc.read()
        raise CollectionError(f"HTTP {exc.code} from {url}: {body[:500]!r}") from exc
    except (URLError, TimeoutError) as exc:
        raise CollectionError(f"request failed for {url}: {exc}") from exc


def query_url(base: str, path: str, params: dict[str, str | int]) -> str:
    return f"{base.rstrip('/')}{path}?{urlencode(params)}"


def save_native(
    root: Path,
    relative: str,
    url: str,
    query_kind: str,
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    status, body = request_bytes(url)
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
    print(json.dumps({"status": "PASS", "files_verified": len(expected)}, indent=2))
    return 0


def collect(args: argparse.Namespace) -> int:
    start = parse_time(args.start)
    end = parse_time(args.end)
    if start >= end:
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

        window_start = start.isoformat().replace("+00:00", "Z")
        window_end = end.isoformat().replace("+00:00", "Z")
        manifest: dict[str, Any] = {
            "format": "tcc-experiment-v2-raw-collection-v1",
            "collected_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "window": {"start": window_start, "end": window_end},
            "drain_seconds_before_queries": args.drain_seconds,
            "port_forwards": forward_manifest,
            "collector_git_commit": args.collector_commit,
            "collector_script_sha256": sha256(Path(__file__).read_bytes()),
            "application_image_source_fingerprint": args.application_image_source_fingerprint,
            "workload_record": None,
            "native_responses": [],
            "control_artifacts": [],
            "notes": [
                "Response bodies are stored as received; no normalization, filtering, or cross-scenario equalization is applied.",
                "Jaeger stores traces once; service-scoped query responses may overlap. Each native response is retained; overlap is reported, never removed.",
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
        for scenario, backend, base, path, params, filename in specs:
            url = query_url(base, path, params)
            parsed_responses[f"{scenario}/{backend}"] = save_native(
                root, f"dataset{scenario}/{filename}", url, backend, manifest["native_responses"]
            )

        for scenario in ("A", "B"):
            base = f"http://127.0.0.1:{ports[f'loki-{scenario.lower()}']}"
            for service in SERVICES:
                selector = f'{{service_name="{service}"}}'
                if scenario == "A":
                    selector = f'{{namespace="online-boutique",service_name="{service}"}}'
                url = query_url(base, "/loki/api/v1/query_range", {
                    "query": selector,
                    "start": str(epoch_ns(start)),
                    "end": str(epoch_ns(end)),
                    "direction": "forward",
                    "limit": str(args.limit),
                })
                result = save_native(
                    root,
                    f"dataset{scenario}/logs/{service}.json",
                    url,
                    "loki-logs-by-service",
                    manifest["native_responses"],
                )
                streams = result.get("data", {}).get("result", [])
                entries = sum(len(stream.get("values", [])) for stream in streams)
                if entries >= args.limit:
                    raise CollectionError(
                        f"Loki result may be truncated for {scenario}/{service}: "
                        f"returned={entries} limit={args.limit}; refusing incomplete RAW"
                    )
                manifest.setdefault("log_counts", {}).setdefault(scenario, {})[service] = entries
                parsed_responses[f"{scenario}/loki/{service}"] = result

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
            for service in SERVICES:
                if service not in available and f"unknown_service:{service}" not in available:
                    manifest.setdefault("trace_services_absent", {}).setdefault(scenario, []).append(service)
                    continue
                query_service = service if service in available else f"unknown_service:{service}"
                url = query_url(f"{base}/api/traces", "", {
                    "service": query_service,
                    "start": str(epoch_us(start)),
                    "end": str(epoch_us(end)),
                    "limit": str(args.trace_limit),
                }).replace("?&", "?")
                result = save_native(
                    root,
                    f"dataset{scenario}/traces/{service}.json",
                    url,
                    "jaeger-traces-by-service",
                    manifest["native_responses"],
                )
                data = result.get("data", [])
                total = result.get("total")
                if len(data) >= args.trace_limit or (isinstance(total, int) and total > len(data)):
                    raise CollectionError(
                        f"Jaeger result may be truncated for {scenario}/{service}: "
                        f"returned={len(data)} total={total} limit={args.trace_limit}; refusing incomplete RAW"
                    )
                manifest.setdefault("trace_counts", {}).setdefault(scenario, {})[service] = len(data)

        summary = summarize(parsed_responses)
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


def summarize(responses: dict[str, dict[str, Any]]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for scenario in ("A", "B"):
        metric = responses[f"{scenario}/prometheus"].get("data", {}).get("result", [])
        loki = [
            stream
            for service in SERVICES
            for stream in responses[f"{scenario}/loki/{service}"].get("data", {}).get("result", [])
        ]
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
    if args.limit < 1 or args.trace_limit < 1 or args.drain_seconds < 0:
        parser.error("limits must be positive and drain-seconds must be non-negative")
    try:
        return collect(args)
    except (CollectionError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"RAW collection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
