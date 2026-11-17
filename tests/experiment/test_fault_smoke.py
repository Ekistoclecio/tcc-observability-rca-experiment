import unittest
import json
from subprocess import CompletedProcess
from unittest.mock import patch

from scripts.experiment.fault_smoke import (
    CLUSTER,
    FAULT_CONFIG,
    FaultSmokeError,
    chaos_manifest,
    active_target_sets,
    parse_classifier_packets,
    parse_ipset_blocks,
    parse_netem_stats,
    ready_endpoint_count,
    require_deployment_provenance,
    require_exclusive_environment,
    resolve_service_cluster_ip_target,
    run_workload,
    service_pods,
    target_service_is_fully_unavailable,
    wait_for_normal_workload_recovery,
)
from scripts.experiment.workload import parse_expected_statuses


class FaultSmokeTests(unittest.TestCase):
    def test_service_unavailability_waits_for_terminating_pods(self) -> None:
        state = {
            "desired_replicas": 0,
            "ready_replicas": 0,
            "ready_endpoints": 0,
            "pods": 1,
        }
        self.assertFalse(target_service_is_fully_unavailable(state))
        state["pods"] = 0
        self.assertTrue(target_service_is_fully_unavailable(state))

    @patch("scripts.experiment.fault_smoke.kube_json", return_value={"items": None})
    def test_empty_pod_list_is_normalized_to_empty(self, _kube_json) -> None:
        self.assertEqual(service_pods("paymentservice"), [])

    @patch(
        "scripts.experiment.fault_smoke.kube_json",
        return_value={"items": [{"endpoints": None}]},
    )
    def test_empty_endpoint_slice_is_normalized_to_zero(self, _kube_json) -> None:
        self.assertEqual(ready_endpoint_count("paymentservice"), 0)

    @patch("scripts.experiment.fault_smoke.time.sleep")
    @patch("scripts.experiment.fault_smoke.run_workload")
    def test_post_recovery_probe_waits_for_transient_grpc_reconnect(
        self, run_workload_mock, _sleep_mock
    ) -> None:
        recovered_probe = {"status": "PASS"}
        run_workload_mock.side_effect = [
            FaultSmokeError("temporary unavailable"),
            recovered_probe,
        ]
        result = wait_for_normal_workload_recovery(18080, timeout=5)
        self.assertEqual(result, {"attempts": 2, "probe": recovered_probe})

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

    def test_f2_targets_only_checkoutservice_to_dynamically_resolved_service_vip(self) -> None:
        definition = {
            "mechanism": {
                "latency_ms": 100,
                "correlation_percent": 100,
                "jitter_ms": 0,
                "failsafe_duration": "2m",
            }
        }
        manifest = chaos_manifest(
            "F2", definition, f2_external_target="10.96.106.113:50051"
        )
        self.assertEqual(manifest["kind"], "NetworkChaos")
        self.assertEqual(manifest["spec"]["selector"]["labelSelectors"], {"app": "checkoutservice"})
        self.assertEqual(manifest["spec"]["externalTargets"], ["10.96.106.113:50051"])
        self.assertNotIn("target", manifest["spec"])
        self.assertEqual(manifest["spec"]["direction"], "to")
        self.assertEqual(manifest["spec"]["delay"], {
            "latency": "100ms", "correlation": "100", "jitter": "0ms"
        })

    def test_f2_manifest_requires_resolved_external_target(self) -> None:
        definition = {"mechanism": {"latency_ms": 100, "correlation_percent": 100,
                                    "jitter_ms": 0, "failsafe_duration": "2m"}}
        with self.assertRaises(FaultSmokeError):
            chaos_manifest("F2", definition)

    @patch("scripts.experiment.fault_smoke.kube_json")
    def test_f2_target_resolves_service_cluster_ip_and_port_dynamically(self, kube_json_mock) -> None:
        kube_json_mock.return_value = {"spec": {
            "clusterIP": "10.96.106.113",
            "ports": [{"port": 50051, "protocol": "TCP"}],
        }}
        self.assertEqual(resolve_service_cluster_ip_target("paymentservice", 50051),
                         "10.96.106.113:50051")
        kube_json_mock.assert_called_once_with(
            "-n", "online-boutique", "get", "service", "paymentservice", "-o", "json"
        )

    @patch("scripts.experiment.fault_smoke.kube_json")
    def test_f2_rejects_missing_or_headless_service_cluster_ip(self, kube_json_mock) -> None:
        for cluster_ip in (None, "", "None"):
            with self.subTest(cluster_ip=cluster_ip):
                kube_json_mock.return_value = {"spec": {"clusterIP": cluster_ip, "ports": []}}
                with self.assertRaises(FaultSmokeError):
                    resolve_service_cluster_ip_target("paymentservice", 50051)

    @patch("scripts.experiment.fault_smoke.kube_json")
    def test_f2_rejects_invalid_service_ip_or_missing_tcp_port(self, kube_json_mock) -> None:
        kube_json_mock.return_value = {"spec": {
            "clusterIP": "not-an-ip", "ports": [{"port": 50051, "protocol": "TCP"}],
        }}
        with self.assertRaises(FaultSmokeError):
            resolve_service_cluster_ip_target("paymentservice", 50051)
        kube_json_mock.return_value = {"spec": {
            "clusterIP": "10.96.106.113", "ports": [{"port": 50052, "protocol": "TCP"}],
        }}
        with self.assertRaises(FaultSmokeError):
            resolve_service_cluster_ip_target("paymentservice", 50051)

    def test_f2_network_counter_parsers_select_exact_vip_rule_and_netem(self) -> None:
        ipsets = parse_ipset_blocks("""Name: vip_port
Type: hash:net,port
Members:
10.96.106.113,tcp:50051

Name: parent
Type: list:set
Members:
vip_port
""")
        self.assertEqual([item["name"] for item in ipsets], ["vip_port", "parent"])
        ipsets.extend([
            {"name": "vip_port_old", "type": "hash:net,port",
             "members": ["10.96.106.113,tcp:50051"]},
            {"name": "parent-old", "type": "list:set", "members": ["vip_port_old"]},
        ])
        self.assertEqual(active_target_sets(
            ipsets,
            "Chain TC-TABLES-0\n 5 458 CLASSIFY 0 -- * eth0 0.0.0.0/0 0.0.0.0/0 "
            "match-set parent dst,dst CLASSIFY set 1:4\n",
            "10.96.106.113,tcp:50051")[1], "parent")
        self.assertEqual(parse_classifier_packets(
            "Chain TC-TABLES-0\n 5 458 CLASSIFY 0 -- * eth0 0.0.0.0/0 0.0.0.0/0 "
            "match-set parent dst,dst CLASSIFY set 1:4\n", "parent"), 5)
        self.assertEqual(parse_netem_stats(
            "qdisc prio 1: root\nqdisc netem 5: parent 1:4 limit 1000 delay 100ms\n"
            " Sent 528 bytes 5 pkt (dropped 0, overlimits 0 requeues 0)\n", 100),
            {"bytes": 528, "packets": 5})

    def test_expected_statuses_are_explicit_and_scoped_by_request(self) -> None:
        parsed = parse_expected_statuses(
            ["POST:/cart/checkout=500", "GET:/cart=500"]
        )
        self.assertEqual(parsed, {("POST", "/cart/checkout"): 500, ("GET", "/cart"): 500})

    @patch("scripts.experiment.fault_smoke.command")
    def test_f5_expects_application_errors_on_all_cart_dependent_routes(self, command) -> None:
        command.return_value = CompletedProcess([], 0, '{"status":"PASS"}', "")

        run_workload("F5", 18080)

        args = command.call_args.args[0]
        configured = [args[index + 1] for index, value in enumerate(args[:-1]) if value == "--expect-status"]
        self.assertEqual(
            configured,
            [
                "GET:/=500",
                "GET:/product/OLJCESPC7Z=500",
                "POST:/cart=500",
                "GET:/cart=500",
                "POST:/cart/checkout=500",
            ],
        )

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
