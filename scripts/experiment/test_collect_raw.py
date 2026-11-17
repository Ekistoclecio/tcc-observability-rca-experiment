"""Focused tests for write-once RAW response capture helpers."""

from __future__ import annotations

from datetime import timezone
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import StringIO
import json
from unittest.mock import patch
from pathlib import Path
import tempfile
from threading import Thread
from urllib.parse import parse_qs, urlparse
import unittest

from collect_raw import (
    CollectionError,
    SERVICES,
    collect_trace_service,
    collect_loki_service,
    forwarding_confirmed,
    generate_log_windows,
    generate_trace_windows,
    format_epoch_ns,
    parse_time,
    query_url,
    save_native,
    sha256,
    split_trace_window,
    split_log_window,
    validate_log_interval_coverage,
    validate_log_query_coverage,
    validate_trace_artifact_references,
    validate_trace_window_coverage,
    verify_bundle,
)


class _Handler(BaseHTTPRequestHandler):
    body = b'{"status":"success","data":{"result":[]}}\n'

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args: object) -> None:
        pass


class RawCollectorTest(unittest.TestCase):
    def test_time_is_normalized_to_utc(self) -> None:
        value = parse_time("2026-11-16T12:00:00-03:00")
        self.assertEqual(value.tzinfo, timezone.utc)
        self.assertEqual(value.isoformat(), "2026-11-16T15:00:00+00:00")
        self.assertEqual(format_epoch_ns(1_795_000_000_123_456_789),
                         "2026-11-18T11:06:40.123456789Z")

    def test_loki_integer_nanosecond_windows_are_exact_half_open_and_deterministic(self) -> None:
        self.assertEqual(generate_log_windows(10, 70, 30), [(10, 40), (40, 70)])
        self.assertEqual(generate_log_windows(10, 75, 30), [(10, 40), (40, 70), (70, 75)])
        self.assertEqual(generate_log_windows(10, 11, 30), [(10, 11)])
        self.assertEqual(split_log_window(10, 15), ((10, 12), (12, 15)))
        validate_log_interval_coverage([(10, 12), (12, 15)], 10, 15)
        with self.assertRaisesRegex(CollectionError, "gap or overlap"):
            validate_log_interval_coverage([(10, 12), (13, 15)], 10, 15)
        with self.assertRaisesRegex(CollectionError, "minimum 1ns"):
            split_log_window(10, 11)

    def test_loki_saturated_query_splits_and_preserves_exact_leaf_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native: list[dict[str, object]] = []
            controls: list[dict[str, object]] = []
            leaves: list[dict[str, object]] = []
            attempts: list[dict[str, object]] = []

            def body(values):
                return json.dumps({"status": "success", "data": {"resultType": "streams",
                    "result": [{"stream": {"service_name": "frontend"}, "values": values}]}
                }, separators=(",", ":")).encode()

            responses = [body([["10", "a"], ["11", "b"]]), body([["10", "a"]]), body([["12", "b"]])]
            with patch("collect_raw.request_bytes", side_effect=[(200, item) for item in responses]) as request:
                parsed, summary = collect_loki_service(
                    root=root, scenario="B", service="frontend", base="http://loki", start_ns=10,
                    end_ns=14, limit=2, native_responses=native, control_artifacts=controls,
                    log_queries=leaves, log_query_attempts=attempts,
                )
            self.assertEqual(request.call_count, 3)
            sent_bounds = [parse_qs(urlparse(call.args[0]).query) for call in request.call_args_list]
            self.assertEqual([(item["start"][0], item["end"][0]) for item in sent_bounds],
                             [("10", "14"), ("10", "12"), ("12", "14")])
            self.assertTrue(all(item["direction"] == ["forward"] for item in sent_bounds))
            self.assertEqual([entry["path"] for entry in leaves], [
                "datasetB/logs/frontend/chunk-000000.json",
                "datasetB/logs/frontend/chunk-000001.json",
            ])
            self.assertEqual([Path(root / item["path"]).read_bytes() for item in leaves], responses[1:])
            self.assertEqual([item["sha256"] for item in leaves], [sha256(item) for item in responses[1:]])
            self.assertEqual(attempts[0]["split_into"], [[10, 12], [12, 14]])
            self.assertEqual(controls[0]["model_visible"], False)
            self.assertEqual(summary["split_count"], 1)
            self.assertEqual(summary["entry_count"], 2)
            self.assertEqual(len(parsed), 2)

    def test_loki_saturated_one_nanosecond_leaf_fails_closed(self) -> None:
        payload = json.dumps({"status": "success", "data": {"resultType": "streams",
            "result": [{"stream": {"service_name": "frontend"}, "values": [["10", "a"], ["10", "b"]]}]}
        }).encode()
        with tempfile.TemporaryDirectory() as temporary, patch(
            "collect_raw.request_bytes", return_value=(200, payload)
        ), redirect_stderr(StringIO()):
            with self.assertRaisesRegex(CollectionError, "minimum 1ns"):
                collect_loki_service(
                    root=Path(temporary), scenario="B", service="frontend", base="http://loki",
                    start_ns=10, end_ns=11, limit=2, native_responses=[], control_artifacts=[],
                    log_queries=[], log_query_attempts=[], window_ns=30,
                )

    def test_loki_query_coverage_requires_all_scenario_service_pairs_and_exact_bounds(self) -> None:
        leaves = [{"scenario": scenario, "service": service, "start_ns": left,
                   "end_ns": right, "entry_count": 0, "limit": 5000,
                   "http_status": 200, "split_depth": 0,
                   "actual_sent_start": str(left), "actual_sent_end": str(right),
                   "direction": "forward"}
                  for scenario in ("A", "B") for service in SERVICES
                  for left, right in ((100, 130), (130, 141))]
        coverage = validate_log_query_coverage(leaves, 100, 141, [], window_ns=30)
        self.assertEqual(len(coverage), 20)
        self.assertEqual(coverage["A/frontend"]["covered_start_ns"], 100)
        self.assertEqual(coverage["A/frontend"]["covered_end_ns"], 141)
        with self.assertRaisesRegex(CollectionError, "missing query node"):
            validate_log_query_coverage(leaves[:-1], 100, 141, [], window_ns=30)
        leaves[0]["end_ns"] = 129
        with self.assertRaisesRegex(CollectionError, "missing query node"):
            validate_log_query_coverage(leaves, 100, 141, [], window_ns=30)

    def test_trace_windows_cover_inclusive_microseconds_without_gaps(self) -> None:
        self.assertEqual(generate_trace_windows(100, 100), [(100, 100)])
        self.assertEqual(generate_trace_windows(10, 29, 10), [(10, 19), (20, 29)])
        self.assertEqual(generate_trace_windows(10, 34, 10), [(10, 19), (20, 29), (30, 34)])
        self.assertEqual(generate_trace_windows(0, 2), [(0, 2)])
        self.assertEqual(generate_trace_windows(5, 14, 10), generate_trace_windows(5, 14, 10))
        with self.assertRaises(CollectionError):
            generate_trace_windows(2, 1)
        with self.assertRaises(CollectionError):
            generate_trace_windows(1, 2, 0)

    def test_capped_trace_windows_split_deterministically_or_fail_at_one_microsecond(self) -> None:
        self.assertEqual(split_trace_window(0, 9), ((0, 4), (5, 9)))
        self.assertEqual(split_trace_window(10, 11), ((10, 10), (11, 11)))
        with self.assertRaisesRegex(CollectionError, "minimum 1us"):
            split_trace_window(10, 10)

    def test_trace_coverage_rejects_a_gap_or_a_leaf_at_the_limit(self) -> None:
        entries = [
            {"scenario": scenario, "service": service,
             "query_start_us": 1, "query_end_us": 2,
             "limit": 10, "trace_count": 0, "http_status": 200, "split_depth": 0}
            for scenario in ("A", "B") for service in SERVICES
        ]
        validate_trace_window_coverage(entries, 1, 2)
        entries[0]["trace_count"] = 10
        with self.assertRaisesRegex(CollectionError, "incomplete"):
            validate_trace_window_coverage(entries, 1, 2)
        entries[0]["trace_count"] = 0
        entries[0]["query_end_us"] = 1
        with self.assertRaisesRegex(CollectionError, "missing query node"):
            validate_trace_window_coverage(entries, 1, 2)

    def test_trace_manifest_references_every_immutable_chunk_and_only_those_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            records: list[dict[str, object]] = []
            hashes: dict[str, str] = {}
            body = b'{"status":"success","data":[]}\n'
            for scenario in ("A", "B"):
                for service in SERVICES:
                    relative = f"dataset{scenario}/traces/{service}/chunk-000000.json"
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(body)
                    digest = sha256(body)
                    hashes[relative] = digest
                    records.append({"path": relative, "scenario": scenario, "service": service,
                                    "sha256": digest, "bytes": len(body), "trace_count": 0})
            validate_trace_artifact_references(root, {
                "trace_queries": records, "trace_query_attempts": [], "control_artifacts": []
            }, hashes)
            records[0]["path"] = records[0]["path"].replace("chunk-000000", "chunk-000001")
            with self.assertRaisesRegex(CollectionError, "missing or inconsistent"):
                validate_trace_artifact_references(root, {
                    "trace_queries": records, "trace_query_attempts": [], "control_artifacts": []
                }, hashes)

    def test_trace_collection_splits_and_preserves_native_response_bytes(self) -> None:
        class TraceHandler(BaseHTTPRequestHandler):
            payloads: dict[tuple[int, int], bytes] = {}

            def do_GET(self) -> None:  # noqa: N802
                query = parse_qs(urlparse(self.path).query)
                start, end = int(query["start"][0]), int(query["end"][0])
                if end - start >= 1:
                    traces = [
                        {"traceID": "a" * 32, "spans": [{"spanID": "1" * 16}]},
                        {"traceID": "b" * 32, "spans": [{"spanID": "2" * 16}]},
                    ]
                else:
                    trace_id = "a" * 32 if start == 10 else "b" * 32
                    span_id = "1" * 16 if start == 10 else "2" * 16
                    traces = [{"traceID": trace_id, "spans": [{"spanID": span_id}]}]
                payload = json.dumps({"status": "success", "data": traces}, separators=(",", ":")).encode()
                self.payloads[(start, end)] = payload
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", 0), TraceHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                native: list[dict[str, object]] = []
                controls: list[dict[str, object]] = []
                queries: list[dict[str, object]] = []
                attempts: list[dict[str, object]] = []
                events = StringIO()
                with redirect_stderr(events):
                    summary = collect_trace_service(
                        root=root, scenario="A", service="frontend", query_service="frontend",
                        base=f"http://127.0.0.1:{server.server_port}", start_us=10, end_us=11,
                        limit=2, native_responses=native, control_artifacts=controls,
                        trace_queries=queries, trace_query_attempts=attempts,
                    )
                self.assertEqual([entry["trace_count"] for entry in queries], [1, 1])
                self.assertEqual(len(attempts), 1)
                self.assertEqual(len(controls), 1)
                self.assertFalse(controls[0]["model_visible"])
                self.assertEqual(summary["unique_trace_ids"], 2)
                self.assertEqual(summary["unique_spans"], 2)
                self.assertEqual(summary["accepted_chunks"], 2)
                for record in native:
                    raw = root / record["path"]
                    self.assertEqual(raw.read_bytes(), TraceHandler.payloads[(
                        record["query_start_us"], record["query_end_us"]
                    )])
                    self.assertEqual(sha256(raw.read_bytes()), record["sha256"])
                    self.assertEqual(raw.stat().st_mode & 0o777, 0o444)
                attempt_path = root / attempts[0]["path"]
                self.assertEqual(attempt_path.read_bytes(), TraceHandler.payloads[(10, 11)])
                self.assertEqual(sha256(attempt_path.read_bytes()), attempts[0]["sha256"])
                all_chunks = [
                    {"scenario": "A", "service": service,
                     "query_start_us": 10, "query_end_us": 11,
                     "limit": 2, "trace_count": 0, "http_status": 200, "split_depth": 0}
                    for service in SERVICES
                ] + [
                    {"scenario": "B", "service": service,
                     "query_start_us": 10, "query_end_us": 11,
                     "limit": 2, "trace_count": 0, "http_status": 200, "split_depth": 0}
                    for service in SERVICES
                ]
                all_chunks = [entry for entry in all_chunks if not (
                    entry["scenario"] == "A" and entry["service"] == "frontend")]
                all_chunks.extend(queries)
                validate_trace_window_coverage(all_chunks, 10, 11, attempts)
                event_rows = [json.loads(line) for line in events.getvalue().splitlines()]
                self.assertEqual(sum(row["event"] == "TRACE_QUERY_START" for row in event_rows), 3)
                self.assertEqual(sum(row["event"] == "TRACE_QUERY_COMPLETE" for row in event_rows), 3)
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_trace_collection_recursively_splits_every_capped_node(self) -> None:
        def response_for(start: int, end: int) -> bytes:
            width = end - start + 1
            count = 2 if width > 2 else 1
            traces = [{"traceID": f"{value:032x}", "spans": []}
                      for value in range(start, start + count)]
            return json.dumps({"status": "success", "data": traces}, separators=(",", ":")).encode()

        with tempfile.TemporaryDirectory() as directory, patch(
            "collect_raw.request_bytes",
            side_effect=lambda url, timeout=30: (200, response_for(
                int(parse_qs(urlparse(url).query)["start"][0]),
                int(parse_qs(urlparse(url).query)["end"][0]),
            )),
        ):
            root = Path(directory)
            native: list[dict[str, object]] = []
            controls: list[dict[str, object]] = []
            queries: list[dict[str, object]] = []
            attempts: list[dict[str, object]] = []
            with redirect_stderr(StringIO()):
                collect_trace_service(
                    root=root, scenario="B", service="frontend", query_service="frontend",
                    base="http://jaeger-b", start_us=0, end_us=7, limit=2,
                    native_responses=native, control_artifacts=controls,
                    trace_queries=queries, trace_query_attempts=attempts,
                )
            self.assertEqual(len(attempts), 3)
            self.assertEqual(len(queries), 4)
            self.assertEqual({item["split_depth"] for item in attempts}, {0, 1})
            self.assertEqual({item["split_depth"] for item in queries}, {2})
            self.assertEqual(len(controls), 3)

    def test_trace_collection_fails_if_a_one_microsecond_leaf_stays_at_limit(self) -> None:
        payload = json.dumps({"status": "success", "data": [{}, {}]}).encode()
        with tempfile.TemporaryDirectory() as directory, patch(
            "collect_raw.request_bytes", return_value=(200, payload)
        ), redirect_stderr(StringIO()):
            with self.assertRaisesRegex(CollectionError, "minimum window"):
                collect_trace_service(
                    root=Path(directory), scenario="A", service="frontend",
                    query_service="frontend", base="http://jaeger-a",
                    start_us=123, end_us=123, limit=2,
                    native_responses=[], control_artifacts=[],
                    trace_queries=[], trace_query_attempts=[],
                )

    def test_trace_transport_failure_logs_query_and_port_forward_state_without_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = {"running": True, "returncode": None}
            events = StringIO()

            def disconnect(_url: str, timeout: float = 30) -> tuple[int, bytes]:
                state.update(running=False, returncode=1)
                raise ConnectionResetError("closed")

            with redirect_stderr(events), patch(
                "collect_raw.request_bytes", side_effect=disconnect
            ) as request:
                with self.assertRaisesRegex(CollectionError, "trace query failed"):
                    collect_trace_service(
                        root=Path(directory), scenario="B", service="cartservice",
                        query_service="cartservice", base="http://127.0.0.1:16686",
                        start_us=10, end_us=10, limit=10000, native_responses=[],
                        control_artifacts=[], trace_queries=[], trace_query_attempts=[],
                        transport_state=lambda: dict(state),
                    )
            self.assertEqual(request.call_count, 1)
            event_rows = [json.loads(line) for line in events.getvalue().splitlines()]
            self.assertEqual([row["event"] for row in event_rows],
                             ["TRACE_QUERY_START", "TRACE_QUERY_FAILED"])
            self.assertEqual(event_rows[-1]["scenario"], "B")
            self.assertEqual(event_rows[-1]["service"], "cartservice")
            self.assertEqual(event_rows[0]["port_forward_state"]["running"], True)
            self.assertEqual(event_rows[-1]["port_forward_state"],
                             {"running": False, "returncode": 1})

    def test_query_url_encodes_native_query_parameters(self) -> None:
        value = query_url("http://localhost:9090/", "/api/v1/query_range", {"query": '{__name__=~".+"}', "step": "1s"})
        self.assertIn("query=%7B__name__%3D~%22.%2B%22%7D", value)
        self.assertIn("step=1s", value)

    def test_port_forward_must_confirm_its_expected_local_and_remote_ports(self) -> None:
        self.assertTrue(forwarding_confirmed(
            "Forwarding from 127.0.0.1:45678 -> 16686\n", 45678, 16686
        ))
        self.assertTrue(forwarding_confirmed(
            "Forwarding from [::1]:45678 -> 16686\n", 45678, 16686
        ))
        self.assertFalse(forwarding_confirmed(
            "Forwarding from 127.0.0.1:45678 -> 16686\n", 45679, 16686
        ))
        self.assertFalse(forwarding_confirmed(
            "Forwarding from 127.0.0.1:45678 -> 9411\n", 45678, 16686
        ))

    def test_response_body_is_preserved_byte_for_byte_and_hashed(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                records: list[dict[str, object]] = []
                save_native(root, "datasetA/metrics.json", f"http://127.0.0.1:{server.server_port}/", "prometheus", records)
                output = root / "datasetA/metrics.json"
                self.assertEqual(output.read_bytes(), _Handler.body)
                self.assertEqual(output.stat().st_mode & 0o777, 0o444)
                self.assertEqual(records[0]["sha256"], sha256(_Handler.body))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_jaeger_query_control_diagnostics_are_written_outside_raw_bundle(self) -> None:
        server = HTTPServer(("127.0.0.1", 0), _Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                diagnostics = StringIO()
                with redirect_stderr(diagnostics):
                    save_native(
                        root,
                        "datasetB/traces/cartservice.json",
                        f"http://127.0.0.1:{server.server_port}/api/traces?service=cartservice&start=100&end=200&limit=10000",
                        "jaeger-traces-by-service",
                        [],
                    )
                event = json.loads(diagnostics.getvalue())
                self.assertEqual(event["backend"], "jaeger-traces-by-service")
                self.assertEqual(event["scenario"], "B")
                self.assertEqual(event["service"], "cartservice")
                self.assertEqual(event["start"], "100")
                self.assertEqual(event["end"], "200")
                self.assertEqual(event["limit"], "10000")
                self.assertEqual(event["http_status"], 200)
                self.assertEqual(event["response_bytes"], len(_Handler.body))
                self.assertIn("elapsed_seconds", event)
                self.assertFalse(any("raw_backend_query" in path.read_text(errors="ignore")
                                     for path in root.rglob("*.json")
                                     if path.name != "cartservice.json"))
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

    def test_verify_bundle_detects_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sample.json").write_text("{}\n", encoding="utf-8")
            (root / "manifest.json").write_text(json.dumps({"sample": True}) + "\n", encoding="utf-8")
            (root / "hashes.sha256").write_text(f"{sha256(b'wrong')}  sample.json\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(verify_bundle(root), 1)

    def test_verify_bundle_accepts_complete_read_only_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "raw"
            root.mkdir()
            sample = root / "sample.json"
            sample.write_text("{}\n", encoding="utf-8")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"sample": True}) + "\n", encoding="utf-8")
            (root / "hashes.sha256").write_text(
                f"{sha256(sample.read_bytes())}  sample.json\n"
                f"{sha256(manifest.read_bytes())}  manifest.json\n",
                encoding="utf-8",
            )
            sample.chmod(0o444)
            manifest.chmod(0o444)
            (root / "hashes.sha256").chmod(0o444)
            root.chmod(0o555)
            with redirect_stdout(StringIO()):
                self.assertEqual(verify_bundle(root), 0)


if __name__ == "__main__":
    unittest.main()
