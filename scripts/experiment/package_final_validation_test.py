from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.experiment.package_final_validation import (
    AuditPackageError,
    clean_exact,
    deterministic_sample,
)


class FinalValidationPackageTests(unittest.TestCase):
    def test_cleanup_removes_only_exact_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "teste"
            target.mkdir()
            (target / "old.txt").write_text("old")
            sibling = root / "artifacts"
            sibling.mkdir()
            (sibling / "keep.txt").write_text("keep")

            clean_exact(root, target, "teste")

            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual((sibling / "keep.txt").read_text(), "keep")

    def test_cleanup_rejects_path_outside_exact_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "artifacts"
            outside.mkdir()
            (outside / "keep.txt").write_text("keep")

            with self.assertRaises(AuditPackageError):
                clean_exact(root, outside, "teste")

            self.assertTrue((outside / "keep.txt").exists())

    def test_cleanup_rejects_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            link = root / "teste"
            link.symlink_to(outside, target_is_directory=True)

            with self.assertRaises(AuditPackageError):
                clean_exact(root, link, "teste")

            self.assertTrue(outside.is_dir())

    def test_sampling_is_deterministic_and_keeps_whole_selected_traces(self) -> None:
        dataset = {
            "metrics": [
                {"concept": "request_count", "service": "checkoutservice", "timestamp": "t2", "evidence_id": "metric-2"},
                {"concept": "request_count", "service": "checkoutservice", "timestamp": "t1", "evidence_id": "metric-1"},
            ],
            "logs": [
                {"service": "paymentservice", "severity": "ERROR", "timestamp": "t1", "evidence_id": "log-1"},
            ],
            "traces": [
                {"trace_id": "b", "start": "t1", "spans": [{"span_id": "s-b", "status": "ERROR"}]},
                {"trace_id": "a", "start": "t2", "spans": [{"span_id": "s-a1", "status": "OK"}, {"span_id": "s-a2", "status": "OK"}]},
            ],
        }

        first = deterministic_sample(dataset)
        second = deterministic_sample(dataset)

        self.assertEqual(first, second)
        self.assertEqual([x["evidence_id"] for x in first["metrics"]], ["metric-1", "metric-2"])
        self.assertEqual([x["trace_id"] for x in first["traces"]], ["b", "a"])
        self.assertEqual(len(first["traces"][1]["spans"]), 2)


if __name__ == "__main__":
    unittest.main()
