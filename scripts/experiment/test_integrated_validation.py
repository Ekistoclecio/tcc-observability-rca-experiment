import unittest
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from run_integrated_validation import (
    IntegratedRunError,
    dataset_evidence,
    run,
    validate_fault_facts,
    verify_global_raw_hashes,
    write_hashes,
)


def metric(name, service, value):
    if name == "request_duration":
        value = {"availability": "OBSERVED", "mean_seconds": value}
    else:
        value = value
    result = {"signal": "metric", "metric": name, "service": service,
              "value": value, "availability": "OBSERVED"}
    if name == "request_duration":
        result["operation"] = "checkout/PlaceOrder"
    return result


def span(service, target, outcome="SUCCESS", duration=20, operation="call"):
    return {"signal": "trace", "service": service, "kind": "CLIENT", "target": target,
            "outcome": outcome, "duration_ms": duration, "operation": operation}


class IntegratedValidationTests(unittest.TestCase):
    def test_fault_runner_package_entrypoint_imports_without_running_faults(self):
        result = run([sys.executable, "-m", "scripts.experiment.fault_smoke", "--help"], check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--fault", result.stdout)

    def test_f1_requires_cpu_and_request_duration_above_normal_baseline(self):
        a = [metric("cpu_utilization", "checkoutservice", 0.9),
             metric("request_duration", "checkoutservice", 0.8)]
        b = [metric("cpu_utilization", "checkoutservice", 0.8),
             metric("request_duration", "checkoutservice", 0.7)]
        baseline_a = [metric("cpu_utilization", "checkoutservice", 0.1),
                      metric("request_duration", "checkoutservice", 0.2)]
        baseline_b = [metric("cpu_utilization", "checkoutservice", 0.1),
                      metric("request_duration", "checkoutservice", 0.3)]
        result = validate_fault_facts("F1", a, b, baseline_a, baseline_b)
        self.assertEqual(result["scenarios"]["A"]["status"], "PASS")
        with self.assertRaises(IntegratedRunError):
            validate_fault_facts("F1", baseline_a, b, baseline_a, baseline_b)

    def test_f1_fails_when_normal_baseline_is_unavailable(self):
        fault = [metric("cpu_utilization", "checkoutservice", 0.8),
                 metric("request_duration", "checkoutservice", 0.8)]
        with self.assertRaisesRegex(IntegratedRunError, "normal baseline"):
            validate_fault_facts("F1", fault, fault, [], [])

    def test_f2_requires_delayed_checkout_to_payment_client_span(self):
        a = [{**span("checkoutservice", None, duration=150),
              "operation": "hipstershop.PaymentService/Charge"}]
        b = [{**span("checkoutservice", None, duration=180),
              "operation": "grpc.hipstershop.PaymentService/Charge"}]
        self.assertEqual(validate_fault_facts("F2", a, b)["scenarios"]["A"]["max_duration_ms"], 150)
        with self.assertRaises(IntegratedRunError):
            validate_fault_facts("F2", [span("checkoutservice", "paymentservice", duration=90)], b)

    def test_f3_and_f5_require_error_on_expected_dependency_edge(self):
        self.assertEqual(validate_fault_facts(
            "F3", [span("checkoutservice", "paymentservice", "ERROR")],
            [span("checkoutservice", "paymentservice", "ERROR")])['scenarios']['A']['status'], "PASS")
        self.assertEqual(validate_fault_facts(
            "F5", [span("cartservice", "redis-cart", "ERROR")],
            [span("cartservice", "redis-cart", "ERROR")])['scenarios']['B']['status'], "PASS")
        with self.assertRaises(IntegratedRunError):
            validate_fault_facts("F5", [span("cartservice", "redis-cart")],
                                 [span("cartservice", "redis-cart", "ERROR")])

    def test_edge_peer_encoded_in_operation_is_recognized(self):
        a = [span("cartservice", None, "ERROR", operation="redis-cart/Client.Get")]
        b = [span("cartservice", "redis-cart", "ERROR")]
        self.assertEqual(validate_fault_facts("F5", a, b)["scenarios"]["A"]["status"], "PASS")

    def test_f5_audit_evidence_accepts_peer_encoded_operation(self):
        with TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset-A.json"
            dataset.write_text(json.dumps({"schema_version": "1.0", "facts": [
                {"signal": "trace", "service": "cartservice", "operation": "redis-cart/Client.Get",
                 "outcome": "ERROR"},
            ]}))
            root = Path(temporary)
            fault_dir = "005_datastore_unavailable_r01"
            (root / fault_dir / "dataset").mkdir(parents=True)
            dataset.replace(root / fault_dir / "dataset/dataset-A.json")
            facts = dataset_evidence(root, fault_dir, "A", "F5", "trace")
            self.assertEqual(len(facts), 1)

    def test_global_raw_hash_verification_maps_manifest_name_after_packaging(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "datasetA").mkdir()
            data_path = root / "datasetA/metrics.json"
            data_path.write_text("native response")
            manifest = root / "global-raw-manifest.json"
            manifest.write_text("collector manifest")
            import hashlib
            entries = [
                f"{hashlib.sha256(data_path.read_bytes()).hexdigest()}  datasetA/metrics.json",
                f"{hashlib.sha256(manifest.read_bytes()).hexdigest()}  manifest.json",
            ]
            (root / "global-raw-hashes.sha256").write_text("\n".join(entries) + "\n")
            self.assertEqual(verify_global_raw_hashes(root), 2)
            data_path.write_text("changed")
            with self.assertRaises(IntegratedRunError):
                verify_global_raw_hashes(root)

    def test_f4_requires_error_log_and_real_b_log_span_correlation(self):
        log_a = {"signal": "log", "service": "paymentservice", "message": "Payment charge failed",
                 "severity": "ERROR"}
        log_b = {**log_a, "trace_id": "a" * 32, "span_id": "b" * 16}
        trace_a = {"signal": "trace", "service": "paymentservice", "operation": "Charge",
                   "kind": "SERVER", "outcome": "ERROR"}
        trace_b = {**trace_a, "trace_id": "a" * 32, "span_id": "b" * 16}
        result = validate_fault_facts("F4", [log_a, trace_a], [log_b, trace_b])
        self.assertEqual(result["scenarios"]["B"]["log_trace_correlated"], True)
        with self.assertRaises(IntegratedRunError):
            validate_fault_facts("F4", [log_a, trace_a], [log_b, trace_a])


if __name__ == "__main__":
    unittest.main()
