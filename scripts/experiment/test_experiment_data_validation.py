from datetime import datetime, timezone
import unittest

from scripts.experiment.experiment_data_validation import (
    PackageError, count_logs, count_metrics, resolve_capture_bounds, unique_traces_and_spans,
)


class ExperimentDataValidationTests(unittest.TestCase):
    def test_capture_window_contains_workload_and_rca_buffer(self):
        start, end = resolve_capture_bounds(
            "2026-10-28T10:00:00Z", "2026-10-28T10:01:00Z",
            "2026-10-28T10:00:10Z", "2026-10-28T10:00:50Z",
        )
        self.assertEqual(start, datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 10, 28, 10, 1, tzinfo=timezone.utc))

    def test_capture_window_expands_for_rca_buffer_outside_workload(self):
        start, end = resolve_capture_bounds(
            "2026-10-28T10:00:10Z", "2026-10-28T10:00:50Z",
            "2026-10-28T10:00:00Z", "2026-10-28T10:01:00Z",
        )
        self.assertEqual(start, datetime(2026, 10, 28, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 10, 28, 10, 1, tzinfo=timezone.utc))

    def test_invalid_capture_windows_are_rejected(self):
        with self.assertRaises(PackageError):
            resolve_capture_bounds(
                "2026-10-28T10:01:00Z", "2026-10-28T10:00:00Z",
                "2026-10-28T10:00:10Z", "2026-10-28T10:00:20Z",
            )

    def test_full_raw_record_counts_use_backend_payloads(self):
        metrics = {"response": {"data": {"result": [
            {"values": [[1, "1"], [2, "2"]]}, {"values": [[3, "3"]]},
        ]}}}
        logs = {"response": {"data": {"result": [{"values": [[1, "a"], [2, "b"]]}]}}}
        traces = {"search_results": [
            {"response": {"data": [{"traceID": "x", "spans": [{}, {}]}, {"traceID": "y", "spans": [{}]}]}},
            {"response": {"data": [{"traceID": "x", "spans": [{}, {}]}]}},
        ]}
        self.assertEqual(count_metrics(metrics), (2, 3))
        self.assertEqual(count_logs(logs), 2)
        self.assertEqual(unique_traces_and_spans(traces), (2, 3))


if __name__ == "__main__":
    unittest.main()
