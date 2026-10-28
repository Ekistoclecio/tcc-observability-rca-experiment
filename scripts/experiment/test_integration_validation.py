from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.experiment import integration_validation as iv


class IntegrationValidationUnitTests(unittest.TestCase):
    def test_fault_order_and_single_selection(self):
        self.assertEqual(iv.select_faults(True, None), ("F1", "F2", "F3", "F4", "F5"))
        self.assertEqual(iv.select_faults(False, "f4"), ("F4",))
        with self.assertRaises(iv.ValidationError):
            iv.select_faults(False, "f6")
        with self.assertRaises(iv.ValidationError):
            iv.select_faults(True, "f1")

    def test_run_ids_are_fixed_and_do_not_overwrite_other_faults(self):
        self.assertEqual(iv.run_id_for("integration-smoke-001", "F1"), "iv-integration-smoke-001-f1")
        self.assertNotEqual(iv.run_id_for("integration-smoke-001", "F1"), iv.run_id_for("integration-smoke-001", "F2"))

    def test_dataset_window_chain_uses_only_confirmed_times(self):
        gt = {"timing": {"confirmed_start": "2026-10-28T12:00:00Z", "confirmed_end": "2026-10-28T12:00:10Z"},
              "fault": {"class": "F4", "location": {"component": "paymentservice"}}}
        window = {"start": "2026-10-28T11:59:50.000000Z", "end": "2026-10-28T12:00:20.000000Z"}
        result = iv.check_window_chain(gt, {"window": window}, {"window": window}, {"window": window}, 10, 10)
        self.assertTrue(result["match"])
        gt["fault"] = {"class": "F5", "location": {"component": "redis-cart"}}
        self.assertTrue(iv.check_window_chain(gt, {"window": window}, {"window": window}, {"window": window}, 10, 10)["match"])

    def test_evidence_id_comparison_and_counts(self):
        dataset = {"metrics": [{"evidence_id": "metric-000001"}], "logs": [{"evidence_id": "log-000001"}],
                   "traces": [{"evidence_id": "trace-000001", "spans": [{"evidence_id": "span-000001"}]}]}
        request = {"telemetry": json.loads(json.dumps(dataset))}
        self.assertTrue(iv.evidence_comparison(dataset, request)["match"])
        self.assertTrue(iv.request_counts(dataset, request)["match"])
        request["telemetry"]["logs"].append({"evidence_id": "log-foreign"})
        self.assertFalse(iv.evidence_comparison(dataset, request)["match"])

    def test_jaeger_query_window_is_checked_for_every_service_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            obj = iv.IntegrationValidation.__new__(iv.IntegrationValidation)
            obj.recorder = iv.EvidenceRecorder(root)
            start, end = "2026-10-28T12:00:00.000000Z", "2026-10-28T12:00:30.000000Z"
            start_us = int(iv.parse_time(start).timestamp() * 1_000_000)
            end_us = int(iv.parse_time(end).timestamp() * 1_000_000)
            services_regex = "^(" + "|".join(iv.SUT) + ")$"
            queries = {}
            for scenario in ("a", "b"):
                base = root / f"scenario-{scenario}" / "raw"
                base.mkdir(parents=True)
                for signal, source in (("metrics", "prometheus"), ("logs", "loki"), ("traces", "jaeger")):
                    query = {"window": {"start": start, "end": end}, "http_status": 200,
                             "params": {"query": "{service_name=~\"" + services_regex + "\"}"}, "records_returned": 0,
                             "samples_returned": 0, "pages": [{"http_status": 200}],
                             "searches": [{"params": {"start": start_us, "end": end_us}, "http_status": 200}],
                             "service_discovery": {"sut_services_available": ["frontend"]}}
                    if signal == "traces":
                        query["http_status"] = None
                    raw = {"source": source, "query": query}
                    (base / f"{signal}.json").write_text(json.dumps(raw), encoding="utf-8")
            datasets = {"A": {"window": {"start": start, "end": end}}, "B": {"window": {"start": start, "end": end}}}
            manifest = {"window": {"start": start, "end": end}}
            result = obj.audit_queries(root, manifest, datasets)
            self.assertTrue(result["same_window"])
            bad = json.loads((root / "scenario-b/raw/traces.json").read_text())
            bad["query"]["searches"][0]["params"]["end"] += 1
            (root / "scenario-b/raw/traces.json").write_text(json.dumps(bad), encoding="utf-8")
            self.assertFalse(obj.audit_queries(root, manifest, datasets)["same_window"])

    def test_validation_artifact_directory_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(iv, "ARTIFACT_ROOT", Path(tmp)):
                iv.IntegrationValidation("validation-1", ("F1",), 10, 10)
                with self.assertRaises(iv.ValidationError):
                    iv.IntegrationValidation("validation-1", ("F1",), 10, 10)

    def test_command_recorder_resolves_repo_relative_artifacts_without_nesting_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            validation_root = repo / "artifacts/integration-validation/v1"
            validation_root.mkdir(parents=True)
            with patch.object(iv, "REPO", repo):
                recorder = iv.EvidenceRecorder(validation_root)
                stdout = "artifacts/integration-validation/v1/f1/audit/check.stdout"
                stderr = "artifacts/integration-validation/v1/f1/audit/check.stderr"
                result = recorder.command(["python3", "-c", "print('evidence')"], stage="test-paths",
                                          stdout_relative=stdout, stderr_relative=stderr)
                self.assertEqual(result.returncode, 0)
                self.assertEqual((repo / stdout).read_text().strip(), "evidence")
                self.assertTrue((repo / stderr).is_file())
                self.assertFalse((validation_root / stdout).exists())

    def test_f3_injection_requires_zero_replicas_readiness_and_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            obj = iv.IntegrationValidation.__new__(iv.IntegrationValidation)
            obj.recorder = iv.EvidenceRecorder(root)
            audit = root / "audit/injection"
            audit.mkdir(parents=True)
            (audit / "deployment.json").write_text(json.dumps({"spec": {"replicas": 0}, "status": {"readyReplicas": 0}}))
            (audit / "endpoints.json").write_text(json.dumps({"subsets": []}))
            (audit / "fault-confirmation-event.json").write_text(json.dumps({"evidence": {"desired_replicas": 0, "ready_replicas": 0, "service_endpoints": 0}}))
            gt_path = root / "ground-truth.json"
            gt_path.write_text("{}")
            self.assertTrue(obj.audit_injection("F3", root, gt_path)["passed"])
            (audit / "endpoints.json").write_text(json.dumps({"subsets": [{"addresses": [{"ip": "10.0.0.1"}]}]}))
            self.assertFalse(obj.audit_injection("F3", root, gt_path)["passed"])

    def test_request_audit_checks_static_parity_hashes_leakage_and_completeness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            obj = iv.IntegrationValidation.__new__(iv.IntegrationValidation)
            obj.validation_id = "validation-1"
            obj.recorder = iv.EvidenceRecorder(root)
            dataset_dir, request_root = root / "datasets/run-1", root / "requests/run-1"
            datasets, requests, manifests = {}, {}, {}
            for scenario, suffix in (("A", "a"), ("B", "b")):
                scenario_dir = dataset_dir / f"scenario-{suffix}"
                request_dir = request_root / f"scenario-{suffix}"
                scenario_dir.mkdir(parents=True)
                request_dir.mkdir(parents=True)
                dataset = {"schema_version": "1.0", "run_id": "run-1", "scenario": scenario,
                           "window": {"start": "2026-10-28T12:00:00Z", "end": "2026-10-28T12:00:10Z"},
                           "metrics": [{"evidence_id": f"metric-{suffix}"}], "logs": [{"evidence_id": f"log-{suffix}"}],
                           "traces": [{"evidence_id": f"trace-{suffix}", "spans": [{"evidence_id": f"span-{suffix}"}]}]}
                request = {"schema_version": "1.0", "task": "root_cause_analysis", "instructions": "same",
                           "observation_window": dataset["window"], "telemetry": {k: dataset[k] for k in ("metrics", "logs", "traces")}}
                ds_path, rq_path = scenario_dir / "dataset.json", request_dir / "request.json"
                preview_path = request_dir / "request-preview.md"
                ds_path.write_text(json.dumps(dataset), encoding="utf-8")
                rq_path.write_text(json.dumps(request), encoding="utf-8")
                preview_path.write_text("preview", encoding="utf-8")
                datasets[scenario], requests[scenario] = dataset, request
                manifests[scenario] = {"source_dataset_sha256": iv.sha256_file(ds_path), "request_sha256": iv.sha256_file(rq_path),
                                       "preview_sha256": iv.sha256_file(preview_path), "prompt_sha256": "p" * 64,
                                       "request_schema_sha256": "r" * 64, "output_schema_sha256": "o" * 64,
                                       "model_visible_sanitization": {"removed_fields": {"telemetry_scenario": 0}}}
            result = obj.audit_requests("run-1", root, datasets, dataset_dir, request_root,
                                        requests["A"], requests["B"], manifests)
            self.assertTrue(result["checks"]["request_counts_match"])
            self.assertTrue(result["checks"]["request_evidence_ids_match"])
            self.assertTrue(result["checks"]["static_contract_parity"])
            self.assertTrue(result["checks"]["hash_provenance"])
            self.assertTrue(result["checks"]["no_direct_control_leakage"])
            requests["B"]["run_id"] = "run-1"
            result = obj.audit_requests("run-1", root, datasets, dataset_dir, request_root,
                                        requests["A"], requests["B"], manifests)
            self.assertFalse(result["leakage"]["B"]["passed"])

    def test_safe_continue_requires_recovered_cluster_and_no_residual_chaos(self):
        obj = iv.IntegrationValidation.__new__(iv.IntegrationValidation)
        with tempfile.TemporaryDirectory() as tmp:
            obj.root = Path(tmp)
            snapshot = {"ok": True}
            obj.capture_cluster_snapshot = lambda *args, **kwargs: snapshot
            obj.health_from_snapshot = lambda state: {"passed": state["ok"],
                                                       "residual_chaos": {"stresschaos": 0, "networkchaos": 0},
                                                       "f4_flags_absent": True}
            self.assertTrue(obj.safe_to_continue("F1"))
            obj.health_from_snapshot = lambda state: {"passed": True,
                                                       "residual_chaos": {"stresschaos": 1, "networkchaos": 0},
                                                       "f4_flags_absent": True}
            self.assertFalse(obj.safe_to_continue("F1"))


if __name__ == "__main__":
    unittest.main()
