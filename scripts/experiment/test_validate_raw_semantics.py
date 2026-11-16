import json
from pathlib import Path
import tempfile
import unittest

from validate_raw_semantics import (
    contains_explicit_trace_fields,
    is_raw_only_trace,
    is_error_span,
    normalize_operation,
    semantic_operation_signature,
    severity_semantic,
    trace_spans,
    verify_raw_bundle,
    ValidationError,
)


class RawSemanticValidationTests(unittest.TestCase):
    def test_normalizes_only_native_operation_prefixes(self):
        self.assertEqual(normalize_operation("grpc.hipstershop.PaymentService/Charge"),
                         "hipstershop.PaymentService/Charge")
        self.assertEqual(normalize_operation("/hipstershop.PaymentService/Charge"),
                         "hipstershop.PaymentService/Charge")
        self.assertEqual(normalize_operation("hipstershop.PaymentService/Charge"),
                         "hipstershop.PaymentService/Charge")

    def test_detects_trace_fields_recursively(self):
        self.assertTrue(contains_explicit_trace_fields({"err": {"span_id": "abc"}}))
        self.assertFalse(contains_explicit_trace_fields({"message": "plain log", "severity": "info"}))

    def test_maps_native_severity_without_changing_meaning(self):
        self.assertEqual(severity_semantic("warning"), "WARN")
        self.assertEqual(severity_semantic("warn"), "WARN")
        self.assertEqual(severity_semantic("error"), "ERROR")
        self.assertEqual(severity_semantic("info"), "INFO")

    def test_error_outcome_accepts_native_otel_and_rpc_statuses(self):
        base = {"service": "paymentservice", "tags": {}}
        self.assertTrue(is_error_span({**base, "tags": {"otel.status_code": "ERROR"}}))
        self.assertTrue(is_error_span({**base, "tags": {"error": True}}))
        self.assertTrue(is_error_span({**base, "tags": {"rpc.grpc.status_code": 13}}))
        self.assertFalse(is_error_span({**base, "tags": {"rpc.grpc.status_code": 0}}))
        self.assertTrue(is_error_span({**base, "tags": {"result": "13"}}))

    def test_normalizes_equivalent_frontend_routes_and_redis_spans(self):
        conventional_http = {
            "service": "frontend", "operation": "GET /product/{id}", "kind": "server",
            "tags": {},
        }
        otel_http = {
            "service": "frontend", "operation": "GET", "kind": "server",
            "tags": {"http.request.method": "GET", "url.path": "/product/OLJCESPC7Z"},
        }
        self.assertEqual(semantic_operation_signature(conventional_http),
                         semantic_operation_signature(otel_http))

        conventional_redis = {
            "service": "cartservice", "operation": "redis-cart GET/SET", "kind": "client",
            "tags": {},
        }
        otel_redis = {
            "service": "cartservice", "operation": "GET SET", "kind": "client",
            "tags": {"db.operation.name": "GET SET", "db.system.name": "redis",
                     "server.address": "redis-cart"},
        }
        self.assertEqual(semantic_operation_signature(conventional_redis),
                         semantic_operation_signature(otel_redis))

    def test_classifies_health_metrics_and_exporter_spans_as_raw_only(self):
        health = {"service": "emailservice", "operation": "/grpc.health.v1.Health/Check",
                  "kind": "server", "tags": {}}
        otel_health = {"service": "shippingservice", "operation": "health.v1.Health/Check",
                       "kind": "server", "tags": {}}
        http_health = {"service": "frontend", "operation": "GET /_healthz",
                       "kind": "server", "tags": {"http.route": "/_healthz"}}
        scrape = {"service": "adservice", "operation": "GET /metrics",
                  "kind": "server", "tags": {"url.path": "/metrics"}}
        exporter = {"service": "adservice", "operation": "POST", "kind": "client",
                    "tags": {"otel.scope.name": "io.opentelemetry.java-http-client",
                             "url.full": "http://jaeger-a.observability-a.svc.cluster.local:9411/api/v2/spans"}}
        self.assertTrue(is_raw_only_trace(health))
        self.assertTrue(is_raw_only_trace(otel_health))
        self.assertTrue(is_raw_only_trace(http_health))
        self.assertTrue(is_raw_only_trace(scrape))
        self.assertTrue(is_raw_only_trace(exporter))

    def test_trace_index_ignores_services_response_and_deduplicates_identical_spans(self):
        with tempfile.TemporaryDirectory() as directory:
            trace_dir = Path(directory) / "datasetA" / "traces"
            trace_dir.mkdir(parents=True)
            trace = {
                "traceID": "a" * 32,
                "spans": [{"traceID": "a" * 32, "spanID": "b" * 16,
                           "operationName": "Charge", "processID": "p1",
                           "tags": [{"key": "span.kind", "value": "server"}],
                           "startTime": 1, "duration": 2}],
                "processes": {"p1": {"serviceName": "paymentservice"}},
            }
            (trace_dir / "paymentservice.json").write_text(json.dumps({"data": [trace]}))
            (trace_dir / "checkoutservice.json").write_text(json.dumps({"data": [trace]}))
            (trace_dir / "services.json").write_text(json.dumps({"data": ["paymentservice"]}))
            spans, conflicts = trace_spans(Path(directory), "A")
            self.assertEqual(len(spans), 1)
            self.assertEqual(conflicts, [])

    def test_bundle_hash_validation_fails_on_mutated_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "data.json").write_text("original")
            (root / "data.json").chmod(0o444)
            digest = "0" * 64
            (root / "hashes.sha256").write_text(f"{digest}  data.json\n")
            (root / "hashes.sha256").chmod(0o444)
            (root / "manifest.json").write_text("{}\n")
            (root / "manifest.json").chmod(0o444)
            for item in (root,):
                item.chmod(0o555)
            with self.assertRaises(ValidationError):
                verify_raw_bundle(root)


if __name__ == "__main__":
    unittest.main()
