from __future__ import annotations

from copy import deepcopy
import unittest

from scripts.experiment.rca.projection_provenance import PROJECTION_VERSION, audit_projection


def fixture(scenario: str = "B"):
    dataset = {
        "run_id": "validation-f4-r01", "scenario": scenario,
        "metrics": [{"evidence_id": "metric-000001", "service": "paymentservice", "concept": "request_count",
                     "operation": "Charge", "kind": "server", "target": None,
                     "timestamp": "2026-11-15T21:22:13Z", "value": 1, "unit": "{request}",
                     "outcome": "error", "labels": {}, "delta_status": "valid", "counter_reset": False,
                     "native_names": ["native_metric_counter"], "telemetry_scenario": "otel",
                     "telemetry.sdk.version": "1.2", "os_version": "linux", "code_file_path": "/src/payment.go"}],
        "logs": [{"evidence_id": "log-000001", "service": "paymentservice", "timestamp": "2026-11-15T21:22:13Z",
                  "severity": "ERROR", "body": "charge failed", "trace_id": "trace-1" if scenario == "B" else None,
                  "span_id": "span-1" if scenario == "B" else None, "attributes": {"error.message": "charge failed"},
                  "scope_name": "payment.instrumentation"}],
        "traces": [{"evidence_id": "trace-000001", "trace_id": "trace-1", "start": "2026-11-15T21:22:13Z",
                     "duration_ms": 15.0, "services": ["paymentservice"],
                     "spans": [{"evidence_id": "span-000001-0001", "trace_id": "trace-1", "span_id": "span-1",
                                "parent_span_id": None, "service": "paymentservice", "operation": "Charge",
                                "kind": "server", "target": None, "start": "2026-11-15T21:22:13Z",
                                "duration_ms": 15.0, "status": "ERROR",
                                "attributes": {"rpc.method": "Charge", "error.message": "charge failed"},
                                "events": []}]}],
    }
    telemetry = deepcopy({key: dataset[key] for key in ("metrics", "logs", "traces")})
    telemetry["metrics"][0].pop("native_names")
    telemetry["metrics"][0].pop("telemetry_scenario")
    telemetry["metrics"][0].pop("telemetry.sdk.version")
    telemetry["metrics"][0].pop("os_version")
    telemetry["metrics"][0].pop("code_file_path")
    telemetry["logs"][0].pop("scope_name")
    telemetry["traces"][0]["spans"][0]["attributes"].pop("rpc.method")
    telemetry["traces"][0]["spans"][0].pop("events")
    request = {"telemetry": telemetry}
    manifest = {"model_visible_projection_version": PROJECTION_VERSION,
                "model_visible_sanitization": {"removed_fields": {
                    "common_view_metadata:native_names": 1,
                    "common_view_metadata:telemetry.sdk.version": 1,
                    "common_view_metadata:os_version": 1,
                    "common_view_metadata:code_file_path": 1,
                    "common_view_metadata:scope_name": 1,
                    "span_attributes:rpc.method": 1,
                    "telemetry_scenario": 1,
                    "telemetry.scenario": 0,
                }}}
    return dataset, request, manifest


class ProjectionProvenanceTests(unittest.TestCase):
    def test_intentionally_removed_markers_metadata_and_native_names_are_allowed(self):
        dataset, request, manifest = fixture()
        result = audit_projection(dataset, request, "B", manifest)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["unexpected_removals"], [])
        self.assertEqual(result["unexpected_additions"], [])
        self.assertEqual(result["unexpected_mutations"], [])
        self.assertEqual(result["allowed_removals"]["common_view_metadata:native_names"], 1)
        self.assertTrue(result["required_fields"]["checked"])
        self.assertTrue(result["evidence_ids"]["match"])

    def test_required_metric_concept_service_and_operation_remain_exact(self):
        dataset, request, manifest = fixture()
        result = audit_projection(dataset, request, "B", manifest)
        self.assertTrue(result["passed"])
        for field in ("concept", "service", "operation"):
            self.assertEqual(request["telemetry"]["metrics"][0][field], dataset["metrics"][0][field])

    def test_b_correlation_is_preserved_and_a_synthetic_correlation_is_rejected(self):
        dataset, request, manifest = fixture("B")
        self.assertTrue(audit_projection(dataset, request, "B", manifest)["passed"])
        dataset, request, manifest = fixture("A")
        self.assertTrue(audit_projection(dataset, request, "A", manifest)["passed"])
        request["telemetry"]["logs"][0]["trace_id"] = "fabricated"
        result = audit_projection(dataset, request, "A", manifest)
        self.assertFalse(result["passed"])
        self.assertTrue(result["log_correlation"]["A_synthetic_correlation_detected"])

    def test_unexpected_removal_fails(self):
        dataset, request, manifest = fixture()
        del request["telemetry"]["logs"][0]["body"]
        result = audit_projection(dataset, request, "B", manifest)
        self.assertFalse(result["passed"])
        self.assertTrue(any(path.endswith(".body") for path in result["unexpected_removals"]))

    def test_unexpected_addition_fails(self):
        dataset, request, manifest = fixture()
        request["telemetry"]["logs"][0]["fault_target"] = "paymentservice"
        result = audit_projection(dataset, request, "B", manifest)
        self.assertFalse(result["passed"])
        self.assertTrue(any(path.endswith(".fault_target") for path in result["unexpected_additions"]))

    def test_value_mutation_fails(self):
        dataset, request, manifest = fixture()
        request["telemetry"]["metrics"][0]["value"] = 2
        result = audit_projection(dataset, request, "B", manifest)
        self.assertFalse(result["passed"])
        self.assertIn("$.metrics[0].value", result["unexpected_mutations"])

    def test_counts_and_evidence_id_mismatch_fail(self):
        dataset, request, manifest = fixture()
        request["telemetry"]["logs"].append(deepcopy(request["telemetry"]["logs"][0]))
        result = audit_projection(dataset, request, "B", manifest)
        self.assertFalse(result["passed"])
        self.assertFalse(result["item_counts_match"])

    def test_unrecognized_span_attribute_removal_is_not_allowed(self):
        dataset, request, manifest = fixture()
        dataset["traces"][0]["spans"][0]["attributes"]["diagnostic_fact"] = "value"
        result = audit_projection(dataset, request, "B", manifest)
        self.assertFalse(result["passed"])
        self.assertTrue(any(path.endswith("diagnostic_fact") for path in result["unexpected_removals"]))

    def test_projection_contract_records_upstream_canonicalization_not_dataset_mutation(self):
        dataset, request, manifest = fixture()
        self.assertEqual(dataset["metrics"][0]["concept"], "request_count")
        result = audit_projection(dataset, request, "B", manifest)
        self.assertTrue(result["passed"])
        self.assertEqual(result["canonical_transformations_applied"], [])


if __name__ == "__main__":
    unittest.main()
