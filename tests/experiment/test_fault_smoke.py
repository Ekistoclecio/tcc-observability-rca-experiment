import unittest
import json
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.experiment.fault_smoke import (
    CLUSTER,
    FAULT_CONFIG,
    FaultSmokeError,
    chaos_manifest,
    require_deployment_provenance,
    require_exclusive_environment,
)
from scripts.experiment.workload import parse_expected_statuses


class FaultSmokeTests(unittest.TestCase):
    def test_fault_matrix_matches_the_five_declared_root_locations(self) -> None:
        faults = json.loads(FAULT_CONFIG.read_text(encoding="utf-8"))["faults"]
        expected = {
            "F1": ("cpu_saturation", "service", "checkoutservice"),
            "F2": ("network_latency", "service_edge", "checkoutservice -> paymentservice"),
            "F3": ("service_unavailable", "service", "paymentservice"),
            "F4": ("application_exception", "operation", "paymentservice/Charge"),
            "F5": ("datastore_unavailable", "datastore", "redis-cart"),
        }
        for fault, (fault_type, kind, value) in expected.items():
            with self.subTest(fault=fault):
                self.assertEqual(faults[fault]["fault_type"], fault_type)
                self.assertEqual(faults[fault]["root_location"]["kind"], kind)
                self.assertEqual(faults[fault]["root_location"]["value"], value)

    def test_f1_targets_checkoutservice_in_the_experiment_namespace(self) -> None:
        definition = {
            "mechanism": {
                "workers": 1,
                "load_percent": 100,
                "failsafe_duration": "2m",
            }
        }
        manifest = chaos_manifest("F1", definition)
        self.assertEqual(manifest["kind"], "StressChaos")
        self.assertEqual(manifest["spec"]["selector"]["namespaces"], ["online-boutique"])
        self.assertEqual(
            manifest["spec"]["selector"]["labelSelectors"],
            {"app": "checkoutservice"},
        )

    def test_f2_targets_only_checkoutservice_to_paymentservice_edge(self) -> None:
        definition = {
            "mechanism": {
                "latency_ms": 100,
                "correlation_percent": 100,
                "jitter_ms": 0,
                "failsafe_duration": "2m",
            }
        }
        manifest = chaos_manifest("F2", definition)
        self.assertEqual(manifest["kind"], "NetworkChaos")
        self.assertEqual(manifest["spec"]["selector"]["labelSelectors"], {"app": "checkoutservice"})
        self.assertEqual(
            manifest["spec"]["target"]["selector"]["labelSelectors"],
            {"app": "paymentservice"},
        )
        self.assertEqual(manifest["spec"]["delay"], {
            "latency": "100ms", "correlation": "100", "jitter": "0ms"
        })

    def test_expected_statuses_are_explicit_and_scoped_by_request(self) -> None:
        parsed = parse_expected_statuses(
            ["POST:/cart/checkout=500", "GET:/cart=500"]
        )
        self.assertEqual(parsed, {("POST", "/cart/checkout"): 500, ("GET", "/cart"): 500})

    def test_invalid_expected_status_spec_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            parse_expected_statuses(["500"])

    @patch("scripts.experiment.fault_smoke.command")
    def test_stopped_other_kind_cluster_does_not_break_exclusive_runtime(self, command) -> None:
        command.side_effect = [
            CompletedProcess([], 0, f"tcc-baseline\n{CLUSTER}\n", ""),
            CompletedProcess([], 0, f"{CLUSTER}\n", ""),
        ]

        require_exclusive_environment()

    @patch("scripts.experiment.fault_smoke.command")
    def test_other_running_kind_cluster_breaks_exclusive_runtime(self, command) -> None:
        command.side_effect = [
            CompletedProcess([], 0, f"{CLUSTER}\ntcc-baseline\n", ""),
            CompletedProcess([], 0, f"{CLUSTER}\ntcc-baseline\n", ""),
        ]

        with self.assertRaises(FaultSmokeError):
            require_exclusive_environment()

    @patch("scripts.experiment.fault_smoke.command")
    def test_deployment_provenance_failure_blocks_fault_runner(self, command):
        command.return_value = CompletedProcess(
            [], 1, '{"status":"FAIL"}\n', ""
        )

        with self.assertRaises(FaultSmokeError):
            require_deployment_provenance()


if __name__ == "__main__":
    unittest.main()
