import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from build_rca_input import ContractError, build


def log_fact(identifier, correlated=False):
    fact = {"id": identifier, "signal": "log", "service": "paymentservice",
            "timestamp": "2026-01-01T00:00:00Z", "severity": "ERROR",
            "message": "Payment charge failed"}
    if correlated:
        fact["trace_id"] = "a" * 32
        fact["span_id"] = "b" * 16
    return fact


class RcaInputContractTests(unittest.TestCase):
    def test_identity_projection_keeps_b_correlation_and_separate_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset.json"
            payload = {"schema_version": "1.0", "facts": [log_fact("ev_" + "1" * 20, True)]}
            dataset.write_text(json.dumps(payload))
            output, provenance = root / "input.json", root / "provenance.json"
            result = build(dataset, output, provenance)
            model_input = json.loads(output.read_text())
            sidecar = json.loads(provenance.read_text())
            self.assertEqual(model_input, {"schema_version": "1.0", "evidence": payload["facts"]})
            self.assertNotIn("scenario", json.dumps(model_input).lower())
            self.assertEqual(sidecar["dataset_sha256"], hashlib.sha256(dataset.read_bytes()).hexdigest())
            self.assertEqual(sidecar["rca_input_sha256"], hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(set(sidecar["evidence_sources"]), {payload["facts"][0]["id"]})
            self.assertEqual(result["status"], "PASS")

    def test_conventional_a_input_does_not_gain_correlation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset.json"
            dataset.write_text(json.dumps({"schema_version": "1.0",
                                           "facts": [log_fact("ev_" + "2" * 20)]}))
            output = root / "input.json"
            build(dataset, output, root / "provenance.json")
            fact = json.loads(output.read_text())["evidence"][0]
            self.assertNotIn("trace_id", fact)
            self.assertNotIn("span_id", fact)

    def test_ground_truth_or_scenario_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset.json"
            fact = log_fact("ev_" + "3" * 20)
            fact["fault_type"] = "application_exception"
            dataset.write_text(json.dumps({"schema_version": "1.0", "facts": [fact]}))
            with self.assertRaises(ContractError):
                build(dataset, root / "input.json", root / "provenance.json")

    def test_control_metadata_in_message_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset.json"
            fact = log_fact("ev_" + "4" * 20)
            fact["message"] = "fault injection F4"
            dataset.write_text(json.dumps({"schema_version": "1.0", "facts": [fact]}))
            with self.assertRaises(ContractError):
                build(dataset, root / "input.json", root / "provenance.json")

    def test_duplicate_evidence_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fact = log_fact("ev_" + "5" * 20)
            dataset = root / "dataset.json"
            dataset.write_text(json.dumps({"schema_version": "1.0", "facts": [fact, fact]}))
            with self.assertRaises(ContractError):
                build(dataset, root / "input.json", root / "provenance.json")

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset.json"
            dataset.write_text(json.dumps({"schema_version": "1.0", "facts": []}))
            output = root / "input.json"
            output.write_text("preserve")
            with self.assertRaises(ContractError):
                build(dataset, output, root / "provenance.json")
            self.assertEqual(output.read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
