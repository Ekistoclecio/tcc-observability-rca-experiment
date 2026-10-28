from __future__ import annotations

from datetime import datetime, timezone
import json
import socket
import subprocess
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from .core import DatasetError, SUT_SERVICES, Window, epoch_microseconds, iso_utc


BACKENDS = {
    "A": {
        "prometheus": ("observability-conventional", "prometheus-conventional", 9090, "/-/ready"),
        "loki": ("observability-conventional", "loki-conventional", 3100, "/ready"),
        "jaeger": ("observability-conventional", "jaeger-conventional", 16686, "/api/services"),
    },
    "B": {
        "prometheus": ("observability-otel", "prometheus-otel", 9090, "/-/ready"),
        "loki": ("observability-otel", "loki-otel", 3100, "/ready"),
        "jaeger": ("observability-otel", "jaeger-otel", 16686, "/api/services"),
    },
}


class PortForwardSet:
    def __init__(self, kubectl: str = "kubectl", context: str | None = None):
        self.kubectl = kubectl
        self.context = context
        self.processes: list[subprocess.Popen] = []
        self.urls: dict[str, str] = {}

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def start(self) -> dict[str, str]:
        for scenario in ("A", "B"):
            for backend, (namespace, service, remote_port, _) in BACKENDS[scenario].items():
                key = f"{scenario}.{backend}"
                local_port = self._free_port()
                command = [self.kubectl]
                if self.context:
                    command.extend(["--context", self.context])
                command.extend(["-n", namespace, "port-forward", f"service/{service}", f"{local_port}:{remote_port}", "--address", "127.0.0.1"])
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.processes.append(process)
                self.urls[key] = f"http://127.0.0.1:{local_port}"
        for scenario in ("A", "B"):
            for backend, (_, _, _, readiness_path) in BACKENDS[scenario].items():
                key = f"{scenario}.{backend}"
                wait_http(self.urls[key] + readiness_path, timeout=45)
        return dict(self.urls)

    def close(self) -> None:
        for process in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(self.processes):
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        self.processes.clear()

    def __enter__(self):
        try:
            return self.start()
        except Exception:
            self.close()
            raise

    def __exit__(self, exc_type, exc, tb):
        self.close()


def http_get(url: str, params: dict[str, Any] | None = None, timeout: float = 30) -> tuple[int, bytes]:
    full_url = url + ("?" + urlencode(params, doseq=True) if params else "")
    try:
        with urlopen(full_url, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:1000]
        raise DatasetError(f"HTTP {exc.code} from {full_url}: {body}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise DatasetError(f"backend request failed for {full_url}: {exc}") from exc


def get_json(url: str, params: dict[str, Any] | None = None, timeout: float = 30) -> tuple[int, Any]:
    status, body = http_get(url, params, timeout)
    try:
        return status, json.loads(body)
    except json.JSONDecodeError as exc:
        raise DatasetError(f"backend returned invalid JSON from {url}") from exc


def wait_http(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status, _ = http_get(url, timeout=2)
            if 200 <= status < 300:
                return
        except Exception as exc:
            last_error = exc
        time.sleep(0.2)
    raise DatasetError(f"backend readiness failed for {url}: {last_error or 'non-2xx response'}")


def _api_data(payload: Any, backend: str, path: str) -> Any:
    if not isinstance(payload, dict):
        raise DatasetError(f"invalid {backend} response from {path}")
    if backend == "prometheus":
        if payload.get("status") != "success":
            raise DatasetError(f"Prometheus query failed: {payload.get('error', payload)}")
        return payload.get("data", {})
    return payload


def collect_prometheus(base_url: str, window: Window, scenario: str, query: dict[str, Any]) -> dict[str, Any]:
    started = iso_utc(datetime.now(timezone.utc))
    status, response = get_json(base_url + query["path"], query["params"])
    data = _api_data(response, "prometheus", query["path"])
    if data.get("resultType") not in ("matrix", "vector"):
        raise DatasetError(f"unexpected Prometheus result type: {data.get('resultType')}")
    series = data.get("result", [])
    sample_count = sum(len(item.get("values", [])) if "values" in item else 1 for item in series)
    names = sorted({item.get("metric", {}).get("__name__") for item in series if item.get("metric", {}).get("__name__")})
    metadata: dict[str, Any] = {}
    metadata_queries: list[dict[str, Any]] = []
    for name in names:
        code, payload = get_json(base_url + "/api/v1/metadata", {"metric": name})
        if payload.get("status") != "success":
            raise DatasetError(f"Prometheus metadata query failed for {name}")
        metadata[name] = payload.get("data", {}).get(name, [])
        metadata_queries.append({"path": "/api/v1/metadata", "params": {"metric": name}, "http_status": code})
    query_meta = {
        "backend": "prometheus", "scenario": scenario, "method": "GET", "path": query["path"],
        "params": query["params"], "window": {"start": iso_utc(window.start), "end": iso_utc(window.end)},
        "http_status": status, "records_returned": len(series), "samples_returned": sample_count,
        "collection_started_at": started, "collection_finished_at": iso_utc(datetime.now(timezone.utc)),
        "metadata_queries": metadata_queries,
    }
    return {"schema_version": "1.0", "scenario": scenario, "source": "prometheus", "query": query_meta,
            "metadata": metadata, "response": response}


def collect_loki(base_url: str, window: Window, scenario: str, query: dict[str, Any], limit: int) -> dict[str, Any]:
    params = dict(query["params"])
    params["limit"] = min(limit, 5000)
    started = iso_utc(datetime.now(timezone.utc))
    pages: list[dict[str, Any]] = []
    page_meta: list[dict[str, Any]] = []

    def fetch(start_ns: int, end_ns: int) -> None:
        page_params = {**params, "start": str(start_ns), "end": str(end_ns)}
        try:
            status, response = get_json(base_url + query["path"], page_params)
        except DatasetError as exc:
            # Loki returns HTTP 400 rather than a truncated response when its server-side
            # max_entries_limit_per_query is exceeded. Split only that case.
            if "max_entries_limit_per_query" not in str(exc):
                raise
            status, response = None, None
        if response is not None:
            if response.get("status") != "success":
                raise DatasetError(f"Loki query failed: {response.get('error', response)}")
            result = response.get("data", {}).get("result", [])
            count = sum(len(stream.get("values", [])) for stream in result)
            if count < params["limit"]:
                pages.append(response)
                page_meta.append({"path": query["path"], "params": page_params, "http_status": status,
                                  "records_returned": count})
                return
        else:
            result, count = [], params["limit"]
        if end_ns <= start_ns:
            raise DatasetError("Loki query cannot be subdivided further without risking truncation")
        midpoint = (start_ns + end_ns) // 2
        if midpoint < start_ns or midpoint >= end_ns:
            raise DatasetError("Loki query interval reached nanosecond precision while still at the limit")
        fetch(start_ns, midpoint)
        fetch(midpoint + 1, end_ns)

    fetch(int(params["start"]), int(params["end"]))
    merged_streams: dict[str, dict[str, Any]] = {}
    for page in pages:
        for stream in page.get("data", {}).get("result", []):
            fields = {key: value for key, value in stream.items() if key != "values"}
            key = json.dumps(fields, sort_keys=True, separators=(",", ":"))
            merged_streams.setdefault(key, {**fields, "values": []})["values"].extend(stream.get("values", []))
    result = list(merged_streams.values())
    result.sort(key=lambda stream: json.dumps(stream.get("stream", {}), sort_keys=True))
    for stream in result:
        stream["values"].sort(key=lambda value: (int(value[0]), str(value[1])))
    records = sum(len(stream.get("values", [])) for stream in result)
    if len(pages) == 0:
        raise DatasetError("Loki collection returned no query pages")
    response = {"status": "success", "data": {"resultType": "streams", "result": result}}
    status = page_meta[0]["http_status"] if len(page_meta) == 1 else None
    query_meta = {
        "backend": "loki", "scenario": scenario, "method": "GET", "path": query["path"],
        "params": params, "pages": page_meta, "window": {"start": iso_utc(window.start), "end": iso_utc(window.end)},
        "http_status": status, "records_returned": records, "page_count": len(pages),
        "collection_started_at": started, "collection_finished_at": iso_utc(datetime.now(timezone.utc)),
    }
    return {"schema_version": "1.0", "scenario": scenario, "source": "loki", "query": query_meta,
            "response": response, "pages": pages}


def collect_jaeger(base_url: str, window: Window, scenario: str, query: dict[str, Any], limit: int) -> dict[str, Any]:
    started = iso_utc(datetime.now(timezone.utc))
    status, services_response = get_json(base_url + "/api/services")
    if not isinstance(services_response.get("data"), list):
        raise DatasetError("Jaeger /api/services response is missing data array")
    discovered = set(services_response["data"])
    targets = [service for service in SUT_SERVICES if service in discovered]
    search_results = []
    search_meta = []
    start_us = epoch_microseconds(window.start)
    end_us = epoch_microseconds(window.end)
    for service in targets:
        params = {"service": service, "start": start_us, "end": end_us, "limit": limit}
        query_status, payload = get_json(base_url + "/api/traces", params)
        traces = payload.get("data")
        if not isinstance(traces, list):
            raise DatasetError(f"Jaeger search for {service} is missing data array")
        if len(traces) >= limit:
            raise DatasetError(f"Jaeger returned {len(traces)} traces at limit {limit}; collection may be truncated")
        search_results.append({"service": service, "response": payload})
        search_meta.append({"path": "/api/traces", "params": params, "http_status": query_status,
                            "records_returned": len(traces)})
    total_occurrences = sum(len(item["response"].get("data", [])) for item in search_results)
    unique_ids = {trace.get("traceID") for item in search_results for trace in item["response"].get("data", []) if trace.get("traceID")}
    query_meta = {
        "backend": "jaeger", "scenario": scenario, "service_discovery": {"path": "/api/services", "http_status": status,
            "sut_services_available": targets},
        "searches": search_meta, "window": {"start": iso_utc(window.start), "end": iso_utc(window.end)},
        "records_returned_before_deduplication": total_occurrences,
        "unique_traces_returned": len(unique_ids), "collection_started_at": started,
        "collection_finished_at": iso_utc(datetime.now(timezone.utc)),
        "time_semantics": "Jaeger search start/end are Unix microseconds; each full returned trace is preserved.",
    }
    return {"schema_version": "1.0", "scenario": scenario, "source": "jaeger", "query": query_meta,
            "search_results": search_results}
