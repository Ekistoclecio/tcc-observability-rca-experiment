import json
from pathlib import Path
import tempfile
import unittest

from scripts.experiment.ground_truth import GroundTruth, GroundTruthError


REQUESTED = "2026-01-01T12:00:00Z"
INJECTED = "2026-01-01T12:00:02Z"
RECOVERED = "2026-01-01T12:00:12Z"


def pending_record() -> GroundTruth:
    return GroundTruth.request(
        "F4-r01",
        "application_exception",
        "operation",
        "paymentservice/Charge",
        REQUESTED,
    )


class GroundTruthTests(unittest.TestCase):
    def test_confirmations_round_trip_and_enable_eligibility(self) -> None:
        record = pending_record()
        record.confirm_injection(INJECTED, "fault_hook_present", "control-plane check")
        self.assertEqual(record.status, "ACTIVE")
        self.assertFalse(record.execution_eligible)
        record.confirm_recovery(RECOVERED, "fault_hook_absent", "control-plane check")
        self.assertTrue(record.execution_eligible)
        self.assertEqual(record.status, "RECOVERED")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ground-truth.json"
            record.write_json(path)
            loaded = GroundTruth.read_json(path)
        self.assertEqual(loaded.to_dict(), record.to_dict())

    def test_injection_before_requested_start_is_rejected(self) -> None:
        with self.assertRaisesRegex(GroundTruthError, "precedes requested_start"):
            pending_record().confirm_injection(
                "2026-01-01T11:59:59Z", "replica_count", "observed replicas"
            )

    def test_recovery_cannot_precede_or_skip_injection(self) -> None:
        with self.assertRaisesRegex(GroundTruthError, "only be confirmed"):
            pending_record().confirm_recovery(
                RECOVERED, "replica_count", "replicas restored"
            )

        record = pending_record()
        record.confirm_injection(INJECTED, "replica_count", "replicas zero")
        with self.assertRaisesRegex(GroundTruthError, "precedes confirmed_start"):
            record.confirm_recovery(
                "2026-01-01T12:00:01Z", "replica_count", "replicas restored"
            )

    def test_missing_injection_confirmation_makes_run_ineligible(self) -> None:
        record = pending_record()
        record.mark_ineligible("fault was not confirmed by control plane")
        self.assertEqual(record.status, "INELIGIBLE")
        self.assertFalse(record.execution_eligible)
        with self.assertRaisesRegex(GroundTruthError, "only be confirmed for an ACTIVE"):
            record.confirm_recovery(RECOVERED, "check", "evidence")

    def test_missing_recovery_never_marks_run_eligible(self) -> None:
        record = pending_record()
        record.confirm_injection(INJECTED, "fault_hook_present", "flag exists")
        self.assertEqual(record.confirmed_end, None)
        self.assertFalse(record.execution_eligible)
        record.mark_ineligible("recovery was not confirmed")
        self.assertEqual(record.status, "INELIGIBLE")
        self.assertFalse(record.execution_eligible)

    def test_schema_and_serialized_record_have_same_required_fields(self) -> None:
        root = Path(__file__).resolve().parents[2]
        schema = json.loads(
            (root / "config/experiment/ground-truth.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(schema["required"]), set(pending_record().to_dict()))


if __name__ == "__main__":
    unittest.main()
