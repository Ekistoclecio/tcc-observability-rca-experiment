from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.experiment.package_final_validation import (
    AuditPackageError,
    clean_exact,
    deterministic_sample,
    semantic_views,
    span_failed,
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

    def test_validator_and_sampler_share_canonical_span_outcome(self) -> None:
        from scripts.experiment.dataset.contract import canonical_span_outcome
        error_span = {"service": "cartservice", "operation": "CartService/GetCart", "kind": "server",
                      "status": None, "attributes": {"grpc.status_code": 9}}
        self.assertEqual(canonical_span_outcome(error_span), "ERROR")
        self.assertTrue(span_failed(error_span))
        dataset = {"metrics": [], "logs": [], "traces": [
            {"trace_id": "z-error", "start": "2", "spans": [error_span]},
            {"trace_id": "a-success", "start": "1", "spans": [{**error_span, "status": "SUCCESS", "attributes": {"grpc.status_code": 0}}]},
            {"trace_id": "m-unknown", "start": "3", "spans": [{"status": None, "attributes": {}}]},
        ]}
        sampled = deterministic_sample(dataset)
        self.assertEqual({x["trace_id"] for x in sampled["traces"]}, {"a-success", "z-error"})
        self.assertIn(("cartservice", "CartService/GetCart", "server", "None", "ERROR"), semantic_views(dataset)["spans"])

    def test_sampler_preserves_logs_linked_to_selected_trace(self) -> None:
        dataset = {"metrics": [], "traces": [{"trace_id": "a-trace", "start": "1", "spans": [
            {"span_id": "s1", "status": "ERROR"}]}], "logs": [
            {"evidence_id": "log-linked", "trace_id": "a-trace", "service": "cartservice", "severity": "ERROR"},
            {"evidence_id": "log-other", "trace_id": "other", "service": "cartservice", "severity": "INFO"},
        ]}
        sampled = deterministic_sample(dataset)
        self.assertEqual([row["evidence_id"] for row in sampled["logs"]], ["log-linked", "log-other"])

    def test_log_parity_uses_canonical_event_and_ignores_native_correlation(self) -> None:
        left = {"metrics": [], "traces": [], "logs": [{"service": "paymentservice", "severity": "ERROR",
            "body": "charge failed", "trace_id": None, "span_id": None, "attributes": {"error.message": "charge failed"}}]}
        right = {"metrics": [], "traces": [], "logs": [{"service": "paymentservice", "severity": "Error",
            "body": "charge failed", "trace_id": "trace-b", "span_id": "span-b", "attributes": {"exception_message": "charge failed"}}]}
        self.assertEqual(semantic_views(left)["logs"], semantic_views(right)["logs"])


if __name__ == "__main__":
    unittest.main()
