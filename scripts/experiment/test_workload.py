import unittest

from workload import validate_phase_counts


class WorkloadPhaseTests(unittest.TestCase):
    def test_warmup_only_phase_is_valid(self):
        validate_phase_counts(1, 0)

    def test_empty_workload_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            validate_phase_counts(0, 0)

    def test_negative_phase_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_phase_counts(1, -1)


if __name__ == "__main__":
    unittest.main()
