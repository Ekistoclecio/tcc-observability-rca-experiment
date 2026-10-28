from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from jsonschema import Draft202012Validator

from .builder import (
    OUTPUT_SCHEMA_PATH, RCAInputError, build_request, generate_run, _source_provenance_markers,
    render_preview, validate_pair, validate_request, validate_response,
)


ROOT = Path(__file__).resolve().parents[3]
RUN_ID = "f4-r001"


def dataset(scenario: str) -> dict:
    corr = {"trace_id": "abc123", "span_id": "span123"} if scenario == "B" else {"trace_id": None, "span_id": None}
    return {
        "schema_version": "1.0", "run_id": RUN_ID, "scenario": scenario,
        "window": {"start": "2026-10-28T05:00:00.000000Z", "end": "2026-10-28T05:01:00.000000Z"},
        "services": ["frontend", "checkoutservice", "cartservice", "productcatalogservice", "currencyservice", "paymentservice", "shippingservice", "emailservice", "recommendationservice", "adservice", "redis-cart"],
        "metrics": [{"evidence_id": "metric-000001", "service": "checkoutservice", "native_name": "requests_total", "unit": "{request}", "timestamp": "2026-10-28T05:00:01.000000Z", "value": 1, "labels": {}}],
        "logs": [{"evidence_id": "log-000001", "timestamp": "2026-10-28T05:00:01.100000Z", "service": "checkoutservice", "severity": "ERROR", "body": "payment operation failed", **corr, "attributes": {}, "labels": {}}],
        "traces": [{"evidence_id": "trace-000001", "trace_id": "a-trace" if scenario == "A" else "b-trace", "start": "2026-10-28T05:00:01.000000Z", "duration_ms": 12, "services": ["checkoutservice"], "spans": [{"evidence_id": "span-000001", "span_id": "a-span" if scenario == "A" else "b-span", "parent_span_id": None, "service": "checkoutservice", "operation": "Checkout", "start": "2026-10-28T05:00:01.000000Z", "duration_ms": 12, "status": "ERROR", "attributes": {}}]}],
        "summary": {"internal-stat": scenario},
    }


def valid_response() -> dict:
    return {"hypotheses": [
        {"rank": 1, "fault_type": "cpu_saturation", "location": {"kind": "service", "component": "adservice", "source": None, "target": None, "operation": None}, "confidence": 0.6, "evidence": ["span-000001"], "explanation": "Evidence indicates a delay."},
        {"rank": 2, "fault_type": "network_latency", "location": {"kind": "service_edge", "component": None, "source": "adservice", "target": "frontend", "operation": None}, "confidence": 0.3, "evidence": [], "explanation": "A second possibility."},
        {"rank": 3, "fault_type": "application_exception", "location": {"kind": "operation", "component": "shippingservice", "source": None, "target": None, "operation": "Quote"}, "confidence": 0.1, "evidence": [], "explanation": "A less likely possibility."},
    ]}


class RCAInputTests(unittest.TestCase):
    def test_request_schema_valid_and_dataset_telemetry_is_preserved(self):
        for scenario in ("A", "B"):
            source = dataset(scenario)
            request = build_request(source)
            validate_request(request)
            self.assertEqual(request["telemetry"], {key: source[key] for key in ("metrics", "logs", "traces")})
            self.assertEqual(len(request["telemetry"]["metrics"]), len(source["metrics"]))
            self.assertEqual(len(request["telemetry"]["logs"]), len(source["logs"]))
            self.assertEqual(len(request["telemetry"]["traces"]), len(source["traces"]))
            self.assertEqual(request["telemetry"]["traces"][0]["spans"], source["traces"][0]["spans"])

    def test_output_schema_requires_exactly_three_hypotheses_and_validation_checks_order_and_uniqueness(self):
        schema = json.loads(OUTPUT_SCHEMA_PATH.read_text())
        Draft202012Validator.check_schema(schema)
        request = build_request(dataset("A"))
        result = valid_response()
        Draft202012Validator(schema).validate(result)
        validate_response(result, request)
        for count in (2, 4):
            invalid = {"hypotheses": result["hypotheses"][:count] if count < 3 else result["hypotheses"] + [result["hypotheses"][0]]}
            with self.assertRaises(RCAInputError):
                validate_response(invalid, request)
        invalid = valid_response()
        invalid["hypotheses"][1]["rank"] = 3
        with self.assertRaisesRegex(RCAInputError, "ranks"):
            validate_response(invalid, request)
        invalid = valid_response()
        invalid["hypotheses"][2]["location"] = invalid["hypotheses"][0]["location"].copy()
        invalid["hypotheses"][2]["fault_type"] = invalid["hypotheses"][0]["fault_type"]
        with self.assertRaisesRegex(RCAInputError, "distinct"):
            validate_response(invalid, request)

    def test_fault_vocabulary_location_and_evidence_ids_are_validated(self):
        request = build_request(dataset("A"))
        invalid = valid_response()
        invalid["hypotheses"][0]["fault_type"] = "F4"
        with self.assertRaises(RCAInputError):
            validate_response(invalid, request)
        invalid = valid_response()
        invalid["hypotheses"][0]["confidence"] = 1.01
        with self.assertRaises(RCAInputError):
            validate_response(invalid, request)
        invalid = valid_response()
        invalid["hypotheses"][0]["location"] = {"kind": "service_edge", "component": None, "source": "checkoutservice", "target": None, "operation": None}
        with self.assertRaises(RCAInputError):
            validate_response(invalid, request)
        invalid = valid_response()
        invalid["hypotheses"][0]["evidence"] = ["log-999999"]
        with self.assertRaisesRegex(RCAInputError, "absent"):
            validate_response(invalid, request)

    def test_static_contract_parity_and_scenario_run_ground_truth_exclusions(self):
        request_a, request_b = build_request(dataset("A")), build_request(dataset("B"))
        self.assertTrue(validate_pair(request_a, request_b))
        self.assertEqual(json.dumps({k: v for k, v in request_a.items() if k != "telemetry"}, sort_keys=True),
                         json.dumps({k: v for k, v in request_b.items() if k != "telemetry"}, sort_keys=True))
        for request in (request_a, request_b):
            encoded = json.dumps(request, sort_keys=True)
            self.assertNotIn(RUN_ID, encoded)
            self.assertNotIn('"scenario"', encoded)
            self.assertNotIn("ground_truth", encoded)
            self.assertNotIn("fault_id", encoded)
            self.assertNotIn("fault_class", encoded)
            self.assertNotIn("fault_target", encoded)
            self.assertNotIn("F1", encoded)
            self.assertNotIn("F2", encoded)
            self.assertNotIn("F3", encoded)
            self.assertNotIn("F4", encoded)
            self.assertNotIn("F5", encoded)

    def test_builder_needs_only_dataset_and_static_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for scenario, suffix in (("A", "a"), ("B", "b")):
                source = root / "artifacts" / "datasets" / RUN_ID / f"scenario-{suffix}" / "dataset.json"
                source.parent.mkdir(parents=True)
                source.write_text(json.dumps(dataset(scenario)), encoding="utf-8")
            # No Ground Truth, run metadata, orchestrator events, or backend access exists here.
            manifests = generate_run(root, RUN_ID)
            self.assertEqual(set(manifests), {"A", "B"})
            self.assertEqual(manifests["A"]["prompt_sha256"], manifests["B"]["prompt_sha256"])
            self.assertEqual(manifests["A"]["output_schema_sha256"], manifests["B"]["output_schema_sha256"])
            self.assertEqual(manifests["A"]["request_schema_sha256"], manifests["B"]["request_schema_sha256"])
            for suffix in ("a", "b"):
                output = root / "artifacts" / "rca-inputs" / RUN_ID / f"scenario-{suffix}"
                request = json.loads((output / "request.json").read_text())
                preview = (output / "request-preview.md").read_text()
                manifest = json.loads((output / "manifest.json").read_text())
                self.assertEqual(preview, "# Canonical Full RCA Request\n\n```json\n" + json.dumps(request, sort_keys=True, ensure_ascii=False, indent=2) + "\n```\n")
                self.assertNotIn(RUN_ID, (output / "request.json").read_text())
                self.assertNotIn("scenario", request)
                self.assertTrue(manifest["content_policy_checks"]["run_id_absent_from_request"])
                self.assertTrue(manifest["content_policy_checks"]["ground_truth_and_control_plane_not_loaded"])
                self.assertIn("run_id", manifest)
                self.assertNotEqual(manifest["builder_commit"], "unknown")
            with self.assertRaisesRegex(RCAInputError, "already exists"):
                generate_run(root, RUN_ID)

    def test_evidence_ids_and_counts_are_complete_and_scenario_telemetry_not_mixed(self):
        for scenario in ("A", "B"):
            source = dataset(scenario)
            request = build_request(source)
            all_ids = {item["evidence_id"] for signal in ("metrics", "logs", "traces") for item in request["telemetry"][signal]}
            all_ids.update(span["evidence_id"] for trace in request["telemetry"]["traces"] for span in trace["spans"])
            self.assertEqual(all_ids, {"metric-000001", "log-000001", "trace-000001", "span-000001"})
            self.assertEqual(request["telemetry"]["logs"][0]["trace_id"], None if scenario == "A" else "abc123")
            self.assertEqual(request["telemetry"]["traces"][0]["trace_id"], "a-trace" if scenario == "A" else "b-trace")

    def test_preview_is_deterministic_and_derived_from_request_only(self):
        request = build_request(dataset("A"))
        self.assertEqual(render_preview(request), render_preview(json.loads(json.dumps(request))))
        self.assertNotIn(RUN_ID.encode(), render_preview(request))
        self.assertNotIn(b"scenario-a", render_preview(request))
        self.assertNotIn(b"scenario-b", render_preview(request))

    def test_native_treatment_provenance_is_audited_and_not_silently_rewritten(self):
        source = dataset("B")
        source["logs"][0]["attributes"]["telemetry_scenario"] = "otel"
        request = build_request(source)
        markers = _source_provenance_markers(request)
        self.assertEqual(markers["key:telemetry_scenario"], 1)
        self.assertEqual(markers["value:otel"], 1)
        self.assertEqual(request["telemetry"]["logs"][0]["attributes"]["telemetry_scenario"], "otel")


if __name__ == "__main__":
    unittest.main()
