import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from build_dataset import (BuildError, build, counter_delta, extract_message,
                           omit_health_metric, operation_from_labels, remove_stack_trace,
                           outcome_from_labels)
import argparse


class DatasetBuilderTests(unittest.TestCase):
    def test_counter_delta_uses_window_baseline_and_end(self):
        self.assertEqual(counter_delta([[100.0, "7"], [101.0, "9"]], 100.0),
                         (2.0, None, 100.0, 101.0))

    def test_missing_counter_baseline_stays_unknown(self):
        delta, reason, _, _ = counter_delta([[102.0, "9"], [103.0, "11"]], 100.0)
        self.assertIsNone(delta)
        self.assertEqual(reason, "missing_baseline")

    def test_counter_reset_stays_unknown(self):
        delta, reason, _, _ = counter_delta([[100.0, "9"], [101.0, "2"]], 100.0)
        self.assertIsNone(delta)
        self.assertEqual(reason, "counter_reset")

    def test_otel_rpc_response_status_is_canonicalized(self):
        self.assertEqual(outcome_from_labels({"rpc_response_status_code": "INTERNAL"}), "ERROR")
        self.assertEqual(outcome_from_labels({"rpc_response_status_code": "OK"}), "SUCCESS")

    def test_http_grpc_route_maps_to_logical_rpc_operation(self):
        labels = {"http_request_method": "POST",
                  "http_route": "/hipstershop.CartService/AddItem"}
        self.assertEqual(operation_from_labels(labels), "hipstershop.CartService/AddItem")

    def test_health_route_variants_are_excluded(self):
        self.assertTrue(omit_health_metric({"rpc_method": "grpc.health.v1.Health/Check"},
                                           "health.v1.Health/Check"))
        self.assertTrue(omit_health_metric({"http_route": "/grpc.health.v1.Health/Check"}, None))
        self.assertTrue(omit_health_metric({"http_route": "/_healthz"}, "GET /_healthz"))

    def test_log_message_selects_application_message_without_stack(self):
        line = json.dumps({"severity": "warn", "message": "Payment charge failed",
                           "err": {"type": "Error", "stack": "at /app/fault-hook.js"}})
        self.assertEqual(extract_message(line), "Payment charge failed")

    def test_stack_frames_are_removed_without_dropping_error_summary(self):
        message = ("Can't access cart storage. RedisConnectionException: ConnectTimeout\n"
                   " ---> RedisConnectionException: no connection\n"
                   "   at RedisCache.GetAsync(String key)\n"
                   "   at cartservice.RedisCartStore.GetCartAsync(String userId) in /app/cart.cs:110\n"
                   "--- End of inner exception stack trace ---")
        self.assertEqual(remove_stack_trace(message),
                         "Can't access cart storage. RedisConnectionException: ConnectTimeout")

    def test_stack_only_log_record_is_excluded(self):
        self.assertIsNone(remove_stack_trace(
            "   at cartservice.RedisCartStore.GetCartAsync(String userId) in /app/cart.cs:110"))

    def test_embedded_payment_credentials_are_redacted_without_dropping_other_facts(self):
        message = ('PaymentService#Charge invoked with request '
                   '{"amount":{"currency_code":"USD","units":"28"},'
                   '"credit_card":{"credit_card_number":"4111111111111111",'
                   '"credit_card_cvv":123,"credit_card_expiration_year":2030}}')
        extracted = extract_message(json.dumps({"message": message}))
        self.assertIn('"currency_code":"USD"', extracted)
        self.assertIn('"units":"28"', extracted)
        self.assertEqual(extracted.count("[REDACTED]"), 3)
        for secret in ("4111111111111111", '"credit_card_cvv":123', '"credit_card_expiration_year":2030'):
            self.assertNotIn(secret, extracted)

    def test_build_is_deterministic_traceable_and_does_not_project_stack_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "raw"
            start = 1_800_000_000.0
            end = start + 2
            window_start = datetime.fromtimestamp(start, timezone.utc).isoformat().replace("+00:00", "Z")
            window_end = datetime.fromtimestamp(end, timezone.utc).isoformat().replace("+00:00", "Z")
            manifest = {"window": {"start": window_start, "end": window_end},
                        "application_image_source_fingerprint": "test-revision",
                        "collector_git_commit": "collector-revision",
                        "collector_script_sha256": "c" * 64}

            def metric(name, labels, values):
                return {"metric": {"__name__": name, **labels},
                        "values": [[start, str(values[0])], [end, str(values[1])]]}

            server_span = {
                "traceID": "a" * 32, "spanID": "b" * 16, "operationName": "/hipstershop.PaymentService/Charge",
                "processID": "p", "startTime": int(start * 1_000_000), "duration": 25_000,
                "references": [],
                "tags": [{"key": "span.kind", "value": "server"}, {"key": "result", "value": "13"}],
            }
            b_span = {**server_span, "traceID": "d" * 32, "spanID": "e" * 16,
                      "operationName": "grpc.hipstershop.PaymentService/Charge",
                      "tags": [{"key": "span.kind", "value": "server"},
                               {"key": "rpc.grpc.status_code", "value": 13},
                               {"key": "error", "value": True}]}
            for side in "AB":
                base = root / f"dataset{side}"
                for kind in ("logs", "traces"):
                    (base / kind).mkdir(parents=True)
                if side == "A":
                    metric_rows = [
                        metric("tcc_conventional_requests_total", {"service_name": "paymentservice",
                               "operation": "hipstershop.PaymentService/Charge", "status_code": "ERROR"}, [4, 7]),
                        metric("tcc_conventional_request_duration_seconds_count", {"service_name": "paymentservice",
                               "operation": "hipstershop.PaymentService/Charge", "status_code": "ERROR"}, [4, 7]),
                        metric("tcc_conventional_request_duration_seconds_sum", {"service_name": "paymentservice",
                               "operation": "hipstershop.PaymentService/Charge", "status_code": "ERROR"}, [1, 2.2]),
                    ]
                    span = server_span
                    line = json.dumps({"severity": "warn", "message": "Payment charge failed",
                                       "err": {"type": "Error", "message": "Payment charge failed",
                                               "stack": "at /usr/src/app/fault-hook.js:1"}})
                    labels = {"service_name": "paymentservice", "detected_level": "warn"}
                else:
                    metric_rows = [
                        metric("otel_rpc_server_call_duration_seconds_count", {"exported_job": "paymentservice",
                               "rpc_method": "hipstershop.PaymentService/Charge",
                               "rpc_grpc_status_code": "13"}, [9, 12]),
                        metric("otel_rpc_server_call_duration_seconds_sum", {"exported_job": "paymentservice",
                               "rpc_method": "hipstershop.PaymentService/Charge",
                               "rpc_grpc_status_code": "13"}, [3, 4.2]),
                    ]
                    span = b_span
                    line = "Payment charge failed"
                    labels = {"service_name": "paymentservice", "severity_text": "WARN",
                              "trace_id": "d" * 32, "span_id": "e" * 16,
                              "exception_stacktrace": "at /usr/src/app/fault-hook.js:1",
                              "StatusCode": "FailedPrecondition",
                              "Detail": "Can't access cart storage. RedisConnectionException: ConnectTimeout\n"
                                        "   at cartservice.RedisCartStore.GetCartAsync() in /app/cart.cs:110",
                              "telemetry_sdk_name": "opentelemetry",
                              "service_instance_id": "ephemeral-instance"}
                metrics_name = "metrics.json"
                (base / metrics_name).write_text(json.dumps({"status": "success", "data": {"result": metric_rows}}))
                (base / "logs" / "paymentservice" / "chunk-000000.json").parent.mkdir(parents=True)
                (base / "logs" / "paymentservice" / "chunk-000000.json").write_text(json.dumps({"status": "success", "data": {
                    "result": [{"stream": labels, "values": [[str(int(end * 1_000_000_000)), line]]}]}}))
                trace_response = json.dumps({"data": [{
                    "traceID": span["traceID"], "spans": [span],
                    "processes": {"p": {"serviceName": "paymentservice"}},
                }]})
                chunk_dir = base / "traces" / "paymentservice"
                chunk_dir.mkdir(parents=True)
                # Inclusive temporal queries may return the same full trace in
                # adjacent chunks. Keep both RAW sources and verify the logical
                # union carries both provenance references.
                (chunk_dir / "chunk-000000.json").write_text(trace_response)
                (chunk_dir / "chunk-000001.json").write_text(trace_response)
            root.mkdir(exist_ok=True)
            (root / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
            files = sorted(p for p in root.rglob("*") if p.is_file())
            hashes = []
            for path in files:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                hashes.append(f"{digest}  {path.relative_to(root).as_posix()}")
            (root / "hashes.sha256").write_text("\n".join(hashes) + "\n")
            for path in (root, *(p for p in root.rglob("*") if p.is_dir())):
                path.chmod(0o555)
            for path in root.rglob("*"):
                if path.is_file():
                    path.chmod(0o444)

            out_a = Path(temporary) / "out-a"
            out_b = Path(temporary) / "out-b"
            result_a = build(argparse.Namespace(raw=root, output_dir=out_a))
            result_b = build(argparse.Namespace(raw=root, output_dir=out_b))
            self.assertEqual(result_a["fact_counts"], result_b["fact_counts"])
            for name in ("dataset-A.json", "dataset-B.json", "provenance.json", "build-manifest.json"):
                self.assertEqual((out_a / name).read_bytes(), (out_b / name).read_bytes())

            dataset_a = json.loads((out_a / "dataset-A.json").read_text())
            dataset_b = json.loads((out_a / "dataset-B.json").read_text())
            self.assertNotIn("fault-hook", json.dumps(dataset_a).lower())
            self.assertNotIn("fault-hook", json.dumps(dataset_b).lower())
            trace_fact_a = next(fact for fact in dataset_a["facts"] if fact["signal"] == "trace")
            trace_sources_a = json.loads((out_a / "provenance.json").read_text())[
                "evidence_sources"]["A"][trace_fact_a["id"]]
            self.assertEqual(len(trace_sources_a), 2)
            self.assertEqual({Path(source["raw_file"]).name for source in trace_sources_a},
                             {"chunk-000000.json", "chunk-000001.json"})
            log_a = next(fact for fact in dataset_a["facts"] if fact["signal"] == "log")
            log_b = next(fact for fact in dataset_b["facts"] if fact["signal"] == "log")
            self.assertNotIn("trace_id", log_a)
            self.assertEqual(log_b["trace_id"], "d" * 32)
            self.assertEqual(log_b["span_id"], "e" * 16)
            self.assertEqual(log_b["attributes"]["StatusCode"], "FailedPrecondition")
            self.assertEqual(log_b["attributes"]["Detail"],
                             "Can't access cart storage. RedisConnectionException: ConnectTimeout")
            self.assertNotIn("telemetry_sdk_name", log_b["attributes"])
            self.assertNotIn("service_instance_id", log_b["attributes"])
            self.assertNotIn("fault-hook", json.dumps(log_b).lower())
            duration_a = next(fact for fact in dataset_a["facts"] if fact.get("metric") == "request_duration")
            duration_b = next(fact for fact in dataset_b["facts"] if fact.get("metric") == "request_duration")
            self.assertAlmostEqual(duration_a["value"]["mean_seconds"], 0.4)
            self.assertAlmostEqual(duration_b["value"]["mean_seconds"], 0.4)
            self.assertTrue(json.loads((out_a / "provenance.json").read_text())["evidence_sources"]["A"])

    def test_builder_rejects_modified_raw_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "raw"
            root.mkdir()
            (root / "manifest.json").write_text("{}")
            (root / "hashes.sha256").write_text("0" * 64 + "  manifest.json\n")
            (root / "manifest.json").chmod(0o444)
            (root / "hashes.sha256").chmod(0o444)
            root.chmod(0o555)
            with self.assertRaises(BuildError):
                build(argparse.Namespace(raw=root, output_dir=Path(temporary) / "output"))


if __name__ == "__main__":
    unittest.main()
