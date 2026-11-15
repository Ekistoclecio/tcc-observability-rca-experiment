from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.experiment.dataset.core import DatasetError, SUT_SERVICES, build_query_plan, epoch_nanoseconds, iso_utc, sha256_file
from scripts.experiment.run_results_battery import (
    ResultsError, clean_results_only, collect_global_jaeger, collect_global_loki, collect_global_telemetry, fault_run_names,
    global_capture_window, publish_fault_run, validate_result_layout,
)


class FakePortForwardSet:
    def __enter__(self):
        return {f"{scenario}.{backend}": f"http://{scenario}-{backend}" for scenario in ("A", "B")
                for backend in ("prometheus", "loki", "jaeger")}

    def __exit__(self, *_args):
        return False


class ResultsBatteryTests(unittest.TestCase):
    @staticmethod
    def write_workload_events(source: Path):
        run_control = source / "audit/run-control"
        run_control.mkdir(parents=True, exist_ok=True)
        (run_control / "orchestrator-events.jsonl").write_text(
            '{"event":"warmup_started","timestamp":"2026-11-15T10:00:00Z"}\n'
            '{"event":"workload_finished","timestamp":"2026-11-15T10:00:30Z"}\n')

    def test_cleanup_removes_only_audit_results(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            results = repo / "audit/results"
            results.mkdir(parents=True)
            (results / "old.json").write_text("old")
            sibling = repo / "artifacts/datasets/keep.json"
            sibling.parent.mkdir(parents=True)
            sibling.write_text("keep")

            recreated = clean_results_only(repo)

            self.assertEqual(recreated, results)
            self.assertEqual(list(results.iterdir()), [])
            self.assertEqual(sibling.read_text(), "keep")

    def test_cleanup_rejects_outside_path_and_does_not_delete_it(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            outside = Path(temp) / "outside"
            repo.mkdir()
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_text("keep")

            with self.assertRaises(ResultsError):
                clean_results_only(repo, outside)

            self.assertEqual(sentinel.read_text(), "keep")

    def test_cleanup_rejects_symlinked_results_path(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp) / "repo"
            repo.mkdir()
            audit = repo / "audit"
            audit.mkdir()
            outside = Path(temp) / "outside"
            outside.mkdir()
            sentinel = outside / "keep.txt"
            sentinel.write_text("keep")
            (audit / "results").symlink_to(outside, target_is_directory=True)

            with self.assertRaises(ResultsError):
                clean_results_only(repo)

            self.assertEqual(sentinel.read_text(), "keep")

    def test_fault_sequence_and_repetition_names_are_deterministic(self):
        runs = fault_run_names(("F1", "F1", "F2", "F4", "F1"))
        self.assertEqual([item["directory"] for item in runs], [
            "001_cpu_saturation_r01", "002_cpu_saturation_r02", "003_network_latency_r01",
            "004_application_exception_r01", "005_cpu_saturation_r03",
        ])

    def test_global_window_uses_whole_experiment_and_fixed_sut_scope(self):
        start = "2026-11-15T10:00:00Z"
        end = "2026-11-15T11:00:00Z"
        window = global_capture_window(start, end)
        plan = build_query_plan(window)
        self.assertEqual(iso_utc(window.start), "2026-11-15T10:00:00.000000Z")
        self.assertEqual(iso_utc(window.end), "2026-11-15T11:00:00.000000Z")
        for scenario in ("A", "B"):
            for query in plan[scenario]:
                self.assertEqual(query["scenario"], scenario)
                if query["backend"] == "prometheus":
                    self.assertEqual(query["params"]["start"], "1794736800.000000")
                    self.assertEqual(query["params"]["end"], "1794740400.000000")
                    for service in SUT_SERVICES:
                        self.assertIn(service, query["params"]["query"])
                if query["backend"] == "jaeger":
                    self.assertEqual(query["search"]["services"], list(SUT_SERVICES))

    def test_global_collection_queries_each_scenario_once_for_same_window(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "results"
            root.mkdir()
            seen = []

            def metric(url, window, scenario, query):
                seen.append(("metrics", scenario, iso_utc(window.start), iso_utc(window.end), query["params"]["query"]))
                return {"source": "prometheus", "query": {"window": {"start": iso_utc(window.start), "end": iso_utc(window.end)}},
                        "response": {"data": {"result": [{"values": [[window.start.timestamp(), "1"]]}]}}}

            def logs(url, window, scenario, query, limit):
                seen.append(("logs", scenario, iso_utc(window.start), iso_utc(window.end), query["params"]["query"]))
                return {"source": "loki", "query": {"records_returned": 1,
                        "window": {"start": iso_utc(window.start), "end": iso_utc(window.end)}},
                        "response": {"data": {"result": [{"values": [["1794736800000000000", "message"]]}]}}}

            def traces(url, window, scenario, query, limit):
                seen.append(("traces", scenario, iso_utc(window.start), iso_utc(window.end), query["search"]["services"]))
                return {"source": "jaeger", "query": {"window": {"start": iso_utc(window.start), "end": iso_utc(window.end)},
                        "service_discovery": {"sut_services_available": ["frontend"]}, "searches": []},
                        "search_results": [{"response": {"data": [{"traceID": "t1", "spans": [
                            {"spanID": "s1", "startTime": 1794736800000000, "duration": 1000}]}]}}]}

            with patch("scripts.experiment.run_results_battery.PortForwardSet", FakePortForwardSet), \
                 patch("scripts.experiment.run_results_battery.collect_prometheus", side_effect=metric), \
                 patch("scripts.experiment.run_results_battery.collect_loki", side_effect=logs), \
                 patch("scripts.experiment.run_results_battery.collect_jaeger", side_effect=traces):
                manifest = collect_global_telemetry(root, "2026-11-15T10:00:00Z", "2026-11-15T10:00:30Z")

            self.assertEqual(len(seen), 8)  # Loki is queried in two segments per scenario; other signals use one global query
            self.assertEqual({(item[2], item[3]) for item in seen}, {
                ("2026-11-15T10:00:00.000000Z", "2026-11-15T10:00:30.000000Z"),
                ("2026-11-15T10:00:00.000000Z", "2026-11-15T10:00:15.000000Z"),
                ("2026-11-15T10:00:15.000000Z", "2026-11-15T10:00:30.000000Z")})
            self.assertEqual(set((root / "datasetA").iterdir()), {root / "datasetA/metrics.json", root / "datasetA/logs.json", root / "datasetA/traces.json"})
            self.assertEqual(set((root / "datasetB").iterdir()), {root / "datasetB/metrics.json", root / "datasetB/logs.json", root / "datasetB/traces.json"})
            self.assertEqual(manifest["scenarios"]["A"]["counts"]["traces"]["unique_traces"], 1)

    def test_global_loki_splits_timed_out_window_and_merges_without_duplicates(self):
        window = global_capture_window("2026-11-15T10:00:00Z", "2026-11-15T10:00:30Z")
        query = build_query_plan(window)["A"][1]
        calls = []

        def fake_collect(_url, part, scenario, part_query, limit):
            calls.append((part.start, part.end))
            self.assertEqual(int(part_query["params"]["start"]), epoch_nanoseconds(part.start))
            self.assertEqual(int(part_query["params"]["end"]), epoch_nanoseconds(part.end))
            if part.end - part.start > timedelta(seconds=8):
                raise DatasetError("backend IncompleteRead(0 bytes read)")
            midpoint_ns = str(int(part.start.timestamp() * 1_000_000_000))
            return {"schema_version": "1.0", "scenario": scenario, "source": "loki",
                    "query": {"records_returned": 1, "pages": [],
                              "window": {"start": iso_utc(part.start), "end": iso_utc(part.end)}},
                    "response": {"status": "success", "data": {"resultType": "streams",
                                 "result": [{"stream": {"service_name": "frontend"},
                                             "values": [[midpoint_ns, "same line"]]}]}},
                    "pages": []}

        with patch("scripts.experiment.run_results_battery.collect_loki", side_effect=fake_collect):
            result = collect_global_loki("http://loki", window, "A", query)

        self.assertEqual(len(calls), 6)  # two 15s slices fail; four 7.5s slices succeed
        self.assertEqual(result["query"]["window"], {"start": iso_utc(window.start), "end": iso_utc(window.end)})
        self.assertEqual(result["query"]["records_returned"], 4)
        self.assertEqual(len(result["query"]["segments"]), 4)
        self.assertEqual(sum(len(stream["values"]) for stream in result["response"]["data"]["result"]), 4)

    def test_global_jaeger_segments_and_deduplicates_full_traces(self):
        window = global_capture_window("2026-11-15T10:00:00Z", "2026-11-15T10:02:00Z")
        documents = []

        def fake_collect(_url, part, scenario, _query, limit):
            documents.append((part.start, part.end))
            span_id = "s1" if part.start.minute == 0 else "s2"
            spans = [{"spanID": "s1", "startTime": 1}]
            if span_id != "s1":
                spans.append({"spanID": span_id, "startTime": 2})
            trace = {"traceID": "trace-1", "spans": spans, "processes": {}}
            return {"schema_version": "1.0", "scenario": scenario, "source": "jaeger",
                    "query": {"window": {"start": iso_utc(part.start), "end": iso_utc(part.end)},
                              "service_discovery": {"sut_services_available": ["frontend"]},
                              "searches": [{"params": {"start": 1, "end": 2}, "http_status": 200}]},
                    "search_results": [{"service": "frontend", "response": {"data": [trace]}}]}

        with patch("scripts.experiment.run_results_battery.collect_jaeger", side_effect=fake_collect):
            result = collect_global_jaeger("http://jaeger", window, "A", {})

        self.assertEqual(len(documents), 2)
        self.assertEqual(result["query"]["window"], {"start": iso_utc(window.start), "end": iso_utc(window.end)})
        traces = result["search_results"][0]["response"]["data"]
        self.assertEqual(len(traces), 1)
        self.assertEqual({span["spanID"] for span in traces[0]["spans"]}, {"s1", "s2"})
        self.assertEqual(result["query"]["unique_traces_returned"], 1)
        self.assertEqual(result["query"]["unique_spans_returned"], 2)

    def test_fault_results_are_byte_copied_and_share_run_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            (source / "02-ground-truth").mkdir(parents=True)
            (source / "01-telemetry/scenario-a/rca-window").mkdir(parents=True)
            (source / "01-telemetry/scenario-b/rca-window").mkdir(parents=True)
            run_id = "run-001"
            gt = {"run_id": run_id, "status": "valid", "validation": {"injection_confirmed": True, "recovery_confirmed": True}}
            (source / "02-ground-truth/ground-truth.json").write_text(json.dumps(gt))
            for scenario in ("a", "b"):
                (source / f"01-telemetry/scenario-{scenario}/rca-window/dataset.json").write_text(json.dumps({"run_id": run_id, "scenario": scenario.upper()}))
            self.write_workload_events(source)
            results = root / "results"
            results.mkdir()
            run = fault_run_names(("F3",))[0]

            published = publish_fault_run(results, run, source, run_id)

            target = results / "001_service_unavailable_r01"
            self.assertEqual({item.name for item in target.iterdir()}, {"datasetA.json", "datasetB.json", "ground-truth.json"})
            self.assertEqual((target / "ground-truth.json").read_bytes(), (source / "02-ground-truth/ground-truth.json").read_bytes())
            self.assertEqual(published["run_id"], run_id)

    def test_fault_results_reject_mixed_source_run_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            (source / "02-ground-truth").mkdir(parents=True)
            (source / "01-telemetry/scenario-a/rca-window").mkdir(parents=True)
            (source / "01-telemetry/scenario-b/rca-window").mkdir(parents=True)
            gt = {"run_id": "run-1", "status": "valid", "validation": {"injection_confirmed": True, "recovery_confirmed": True}}
            (source / "02-ground-truth/ground-truth.json").write_text(json.dumps(gt))
            for scenario, run_id in (("a", "run-1"), ("b", "run-2")):
                (source / f"01-telemetry/scenario-{scenario}/rca-window/dataset.json").write_text(json.dumps({"run_id": run_id}))
            self.write_workload_events(source)
            results = root / "results"
            results.mkdir()
            with self.assertRaisesRegex(ResultsError, "run identity mismatch"):
                publish_fault_run(results, fault_run_names(("F1",))[0], source, "run-1")
            self.assertEqual(list(results.iterdir()), [])

    def test_result_layout_requires_flat_global_scenarios_and_three_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "results"
            root.mkdir()
            fault_dir = "001_cpu_saturation_r01"
            for name in ("README.md",):
                (root / name).write_text("readme")
            run_id = "run-001"
            for scenario in ("A", "B"):
                folder = root / f"dataset{scenario}"
                folder.mkdir()
                for signal in ("metrics", "logs", "traces"):
                    (folder / f"{signal}.json").write_text(json.dumps({"query": {"window": {"start": "2026-11-15T10:00:00.000000Z", "end": "2026-11-15T11:00:00.000000Z"}}}))
            fault = root / fault_dir
            fault.mkdir()
            for name, document in (("datasetA.json", {"run_id": run_id}), ("datasetB.json", {"run_id": run_id}),
                                   ("ground-truth.json", {"run_id": run_id})):
                (fault / name).write_text(json.dumps(document))
            scenarios = {}
            for scenario in ("A", "B"):
                files = {}
                for signal in ("metrics", "logs", "traces"):
                    path = root / f"dataset{scenario}" / f"{signal}.json"
                    files[signal] = {"sha256": sha256_file(path)}
                scenarios[scenario] = {"files": files}
            manifest = {"experiment_start": "2026-11-15T10:00:00.000000Z", "experiment_end": "2026-11-15T11:00:00.000000Z",
                        "fault_runs": [{"directory": fault_dir, "run_id": run_id}],
                        "global_telemetry": {"scenarios": scenarios}}
            (root / "manifest.json").write_text(json.dumps(manifest))

            validate_result_layout(root, manifest)
            (root / "datasetA/nested").mkdir()
            with self.assertRaisesRegex(ResultsError, "datasetA must contain exactly"):
                validate_result_layout(root, manifest)


if __name__ == "__main__":
    unittest.main()
