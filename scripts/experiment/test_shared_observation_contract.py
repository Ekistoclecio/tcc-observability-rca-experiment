"""Static checks for the cross-runtime Shared Observation Contract wiring."""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class SharedObservationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = json.loads((ROOT / "config/experiment/observation-contract.json").read_text())
        cls.payment_server = (ROOT / "src/paymentservice/server.js").read_text()
        cls.payment_fault = (ROOT / "src/paymentservice/fault-hook.js").read_text()
        cls.payment_wrapper = (ROOT / "src/paymentservice/conventional-telemetry.js").read_text()
        cls.checkout_wrapper = (ROOT / "src/checkoutservice/conventional.go").read_text()
        cls.checkout_otel = (ROOT / "src/checkoutservice/otel_telemetry.go").read_text()
        cls.cart_store = (ROOT / "src/cartservice/src/cartstore/RedisCartStore.cs").read_text()
        cls.cart_interceptor = (ROOT / "src/cartservice/src/ConventionalTelemetry.cs").read_text()
        cls.cart_startup = (ROOT / "src/cartservice/src/Startup.cs").read_text()
        cls.cart_telemetry = (ROOT / "src/cartservice/src/Telemetry.cs").read_text()

    def test_contract_declares_shared_observation_points_and_treatments(self):
        points = {item["concept"] for item in self.contract["observation_points"]}
        self.assertEqual(points, {"REQUEST", "DOWNSTREAM_CALL", "DATASTORE_OPERATION", "APPLICATION_EVENT"})
        self.assertTrue(self.contract["events_must_be_fault_independent"])
        self.assertIn("x-tcc-trace-id/x-tcc-span-id propagation", self.contract["treatment_differences"]["scenario_a"])
        self.assertIn("W3C Trace Context", self.contract["treatment_differences"]["scenario_b"])

    def test_payment_charge_error_is_one_neutral_shared_application_event(self):
        self.assertIn("dispatchApplicationEvent(event, [conventionalTelemetry, otelTelemetry])", self.payment_server)
        self.assertIn("eventName: 'payment_charge_failed'", self.payment_server)
        emitted_message = re.search(r"new Error\('([^']+)'\)", self.payment_fault).group(1)
        self.assertNotRegex(emitted_message, re.compile(r"controlled|injected|experiment|chaos|F[1-5]", re.I))

    def test_payment_rpc_completion_uses_one_measurement_for_adapters(self):
        self.assertIn("dispatchObservation(observation", self.payment_wrapper)
        self.assertIn("otelTelemetry.recordObservation", self.payment_server)
        self.assertNotIn("otelTelemetry.wrapUnary", self.payment_server)

    def test_cart_datastore_uses_one_duration_for_both_metric_adapters(self):
        self.assertIn("var elapsed = Stopwatch.GetElapsedTime(startedAt)", self.cart_store)
        self.assertIn("conventionalSpan.CompleteObservation(elapsed.TotalSeconds, failed)", self.cart_store)
        self.assertIn("CartTelemetry.RedisDuration.Record(elapsed.TotalSeconds, tags)", self.cart_store)

    def test_cart_error_event_comes_from_shared_datastore_operation(self):
        self.assertIn('_logger.LogError("datastore_operation_failed operation={Operation}", operation)', self.cart_store)

    def test_checkout_downstream_completion_is_shared_across_metric_adapters(self):
        self.assertIn('recordSharedRPCObservation(ctx, "CLIENT", method, targetService(method), statusCode, duration)', self.checkout_wrapper)
        self.assertIn('t.finish(parent.traceID, spanID, parent.spanID, method, "CLIENT", statusCode, start, duration)', self.checkout_wrapper)
        self.assertIn('sharedRPCClientDuration.Record(ctx, duration.Seconds(), options)', self.checkout_otel)
        self.assertIn('event_name", "downstream_call_failed"', self.checkout_wrapper)
        self.assertIn('otelmetric.Int64Counter', self.checkout_otel)

    def test_cart_rpc_has_single_shared_measurement_interceptor(self):
        self.assertEqual(self.cart_startup.count("options.Interceptors.Add<"), 1)
        self.assertNotIn("OTelMetricsInterceptor", self.cart_startup)
        self.assertIn("CartTelemetry.RecordRpcObservation(context.Method, durationSeconds, result)", self.cart_interceptor)
        self.assertIn("RecordRpcObservation", self.cart_telemetry)


if __name__ == "__main__":
    unittest.main()
