from __future__ import annotations

import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.experiment.faults.chaos_mesh import ChaosMeshInjector
from scripts.experiment.faults.application import ApplicationFaultInjector
from scripts.experiment.faults.kubernetes import KubernetesScaleInjector
from scripts.experiment.orchestrator.config import ConfigurationError, ExperimentConfig, read_env_file
from scripts.experiment.orchestrator.ground_truth import add_reason, initial_ground_truth
from scripts.experiment.orchestrator.run_experiment import classify_exception, classify_run_status


ROOT = Path(__file__).resolve().parents[3]
CONFIGS = ROOT / "configs/experiment/faults"


class ConfigTests(unittest.TestCase):
    def test_all_five_configs_match_fixed_fault_matrix(self):
        for fault_class in ("F1", "F2", "F3", "F4", "F5"):
            path = next(CONFIGS.glob(f"{fault_class.lower()}-*.json"))
            config = ExperimentConfig.load(path, f"{fault_class.lower()}-test", ROOT)
            self.assertEqual(config.fault_class, fault_class)
            self.assertLess(
                config.schedule["start_after_steady_seconds"]
                + config.schedule["duration_seconds"]
                + config.schedule["post_fault_steady_seconds"],
                config.steady_duration,
            )

    def test_rejects_moved_fault_target_and_f5_degradation(self):
        path = CONFIGS / "f5-redis-unavailable.json"
        raw = json.loads(path.read_text())
        raw["fault"]["parameters"]["latency_ms"] = 25
        temporary = ROOT / "configs/experiment/faults/.invalid-f5-test.json"
        try:
            temporary.write_text(json.dumps(raw))
            with self.assertRaises(ConfigurationError):
                ExperimentConfig.load(temporary, "invalid-test", ROOT)
        finally:
            temporary.unlink(missing_ok=True)

    def test_env_parser_keeps_quoted_checkout_values(self):
        values = read_env_file(ROOT / "src/loadgenerator/experiment/workload-validation.env.example")
        self.assertEqual(values["TCC_CHECKOUT_STREET_ADDRESS"], "1600 Amphitheatre Parkway")
        self.assertEqual(values["TCC_CHECKOUT_CITY"], "Mountain View")


class GroundTruthTests(unittest.TestCase):
    def test_ground_truth_starts_aborted_and_separates_timing(self):
        config = ExperimentConfig.load(CONFIGS / "f2-checkout-payment-latency.json", "f2-test", ROOT)
        document = initial_ground_truth(config)
        self.assertEqual(document["status"], "aborted")
        self.assertIsNone(document["timing"]["scheduled_start"])
        self.assertIsNone(document["timing"]["confirmed_start"])
        self.assertEqual(document["fault"]["location"]["kind"], "service_edge")
        add_reason(document, "unit", "example")
        self.assertEqual(document["reasons"], [{"code": "unit", "message": "example"}])

    def test_run_validity_and_f4_effect_transitions(self):
        common = {"fault_class": "F3", "injection_confirmed": True, "recovery_confirmed": True,
                  "workload_valid": True, "control_plane_valid": True, "workload_reasons": []}
        self.assertEqual(classify_run_status(**common), "valid")
        self.assertEqual(classify_run_status(**{**common, "control_plane_valid": False}), "invalid_control_plane")
        self.assertEqual(classify_run_status(**{**common, "injection_confirmed": False}), "invalid_injection")
        self.assertEqual(classify_run_status(**{**common, "recovery_confirmed": False}), "invalid_recovery")
        self.assertEqual(classify_run_status(**{**common, "workload_valid": False}), "invalid_workload")
        f4 = {**common, "fault_class": "F4", "workload_valid": False,
              "workload_reasons": [{"code": "f4_effect_not_observed"}]}
        self.assertEqual(classify_run_status(**f4), "invalid_injection")
        f4["workload_reasons"] = [{"code": "f4_recovery_not_observed"}]
        self.assertEqual(classify_run_status(**f4), "invalid_recovery")

    def test_exception_operations_map_to_explicit_invalid_statuses(self):
        self.assertEqual(classify_exception("preflight"), "invalid_precondition")
        self.assertEqual(classify_exception("inject_fault"), "invalid_injection")
        self.assertEqual(classify_exception("recover_fault"), "invalid_recovery")
        self.assertEqual(classify_exception("unexpected"), "invalid_control_plane")


class InjectorTests(unittest.TestCase):
    def test_scale_injector_restores_original_replica_count(self):
        state = {"replicas": 2}

        def fake_status(name, namespace="default"):
            return {"name": name, "namespace": namespace, "desired_replicas": state["replicas"], "ready_replicas": state["replicas"], "available_replicas": state["replicas"], "ready": state["replicas"] > 0}

        def fake_kubectl(*args, **kwargs):
            if args[0] == "scale":
                state["replicas"] = int(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--replicas=")))
            return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

        with patch("scripts.experiment.faults.kubernetes.deployment_status", side_effect=fake_status), \
             patch("scripts.experiment.faults.kubernetes.endpoint_count", side_effect=lambda *_: state["replicas"]), \
             patch("scripts.experiment.faults.kubernetes.run_kubectl", side_effect=fake_kubectl):
            injector = KubernetesScaleInjector("paymentservice")
            evidence = injector.prepare()
            self.assertEqual(evidence["original_replicas"], 2)
            injector.apply()
            injector.confirm_active(0.2)
            self.assertEqual(state["replicas"], 0)
            injector.recover()
            injector.confirm_recovered(0.2)
            self.assertEqual(state["replicas"], 2)

    def test_network_chaos_manifest_targets_fixed_edge(self):
        injector = ChaosMeshInjector("F2", "f2-r001", {"latency_ms": 25, "correlation_percent": 100, "jitter_ms": 0})
        manifest = injector.manifest()
        self.assertEqual(manifest["kind"], "NetworkChaos")
        self.assertEqual(manifest["spec"]["direction"], "to")
        self.assertEqual(manifest["spec"]["selector"]["labelSelectors"]["app"], "checkoutservice")
        self.assertEqual(manifest["spec"]["target"]["selector"]["labelSelectors"]["app"], "paymentservice")
        self.assertEqual(manifest["spec"]["delay"]["latency"], "25ms")

    def test_stress_chaos_uses_stressors_without_network_action_field(self):
        injector = ChaosMeshInjector("F1", "f1-r001", {"workers": 2, "load_percent": 30})
        spec = injector.manifest()["spec"]
        self.assertNotIn("action", spec)
        self.assertEqual(spec["stressors"]["cpu"], {"workers": 2, "load": 30})
        self.assertEqual(spec["selector"]["labelSelectors"]["app"], "checkoutservice")

    def test_application_hook_lifecycle_toggles_file_without_replacing_pod(self):
        pod = {"name": "payment-0", "uid": "uid-0", "restarts": 0, "ready": True, "labels": {"app": "paymentservice"}}
        flag = {"exists": False}

        def fake_exec(*args, check=True, **kwargs):
            if args[0] == "exec":
                command = args[7:]
                if command[:2] == ("sh", "-c") and "printf present" in command[2]:
                    state = "present" if flag["exists"] else "absent"
                    return type("Result", (), {"returncode": 0, "stderr": "", "stdout": state})()
                if any("enabled >" in part for part in command):
                    flag["exists"] = True
                elif command and command[0] == "rm":
                    flag["exists"] = False
                    return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()
                elif command and command[0] == "test":
                    if "!" in command:
                        return type("Result", (), {"returncode": 0 if not flag["exists"] else 1, "stderr": "", "stdout": ""})()
                    if "-e" in command:
                        return type("Result", (), {"returncode": 0 if flag["exists"] else 1, "stderr": "", "stdout": ""})()
            return type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()

        status = {"desired_replicas": 1, "ready_replicas": 1, "ready": True}
        with patch("scripts.experiment.faults.application.deployment_status", return_value=status), \
             patch("scripts.experiment.faults.application.ready_pods", return_value=[pod]), \
             patch("scripts.experiment.faults.application.run_kubectl", side_effect=fake_exec):
            injector = ApplicationFaultInjector()
            injector.prepare()
            injector.apply()
            self.assertTrue(injector.confirm_active(0.2)["flags_present_by_pod"]["payment-0"])
            injector.recover()
            evidence = injector.confirm_recovered(0.2)
            self.assertFalse(evidence["flags_present_by_pod"]["payment-0"])
            self.assertEqual(evidence["pods"][0]["uid"], "uid-0")


if __name__ == "__main__":
    unittest.main()
