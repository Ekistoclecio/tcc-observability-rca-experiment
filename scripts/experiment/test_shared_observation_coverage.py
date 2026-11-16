import json
import unittest

from scripts.experiment.shared_observation_coverage import CONTRACT, audit


class SharedObservationCoverageAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = json.loads(CONTRACT.read_text(encoding="utf-8"))

    def test_ten_services_have_complete_explicit_coverage_and_source_markers(self):
        report = audit(self.contract)
        self.assertEqual(report["result"], "PASS")
        self.assertTrue(report["fault_execution_allowed"])
        self.assertEqual(report["service_count"], 10)
        self.assertEqual(report["partial"], 0)
        self.assertEqual(report["complete"] + report["complete_with_na"], 10)
        self.assertTrue(all(all(marker["found"] for marker in row["source_markers"])
                            for row in report["services"]))
        self.assertTrue(all(row["request_count"] and row["request_duration"]
                            and row["metric_adapters"] == ["conventional", "opentelemetry"]
                            for row in report["services"]))

    def test_partial_service_blocks_fault_execution(self):
        contract = json.loads(json.dumps(self.contract))
        contract["service_coverage"]["emailservice"]["status"] = "PARTIAL"
        report = audit(contract)
        self.assertEqual(report["result"], "FAIL")
        self.assertFalse(report["fault_execution_allowed"])
        self.assertIn("emailservice: status is 'PARTIAL'", report["issues"])

    def test_n_a_requires_a_reason(self):
        contract = json.loads(json.dumps(self.contract))
        contract["service_coverage"]["frontend"]["na_reasons"].pop("datastore")
        report = audit(contract)
        self.assertEqual(report["result"], "FAIL")
        self.assertIn("frontend: datastore=N/A has no reason", report["issues"])


if __name__ == "__main__":
    unittest.main()
