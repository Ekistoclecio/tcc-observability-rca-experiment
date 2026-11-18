import ast
import json
import unittest
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from run_integrated_validation import (
    IntegratedRunError,
    PreserveWorkOnError,
    dataset_evidence,
    external_inventory_paths,
    finalize_results,
    fresh_prometheus_instances,
    hash_inventory,
    observe_filesystem,
    package_global_raw,
    publish_finalization,
    remove_temporary_tree,
    reserve_local_port,
    run,
    run_normal_baseline,
    validate_fault_facts,
    verify_global_raw_hashes,
    verify_hash_manifest,
    write_hashes,
    _export_llm_responses,
    _write_json,
)
from scripts.experiment.evaluation.evaluator import write_evaluation
from scripts.experiment.result_layout import (
    create_run_tree,
    outer_hashes,
    publish_atomic,
    verify_run,
    write_json,
)


def metric(name, service, value):
    if name == "request_duration":
        value = {"availability": "OBSERVED", "mean_seconds": value}
    else:
        value = value
    result = {"signal": "metric", "metric": name, "service": service,
              "value": value, "availability": "OBSERVED"}
    if name == "request_duration":
        result["operation"] = "checkout/PlaceOrder"
    return result


def span(service, target, outcome="SUCCESS", duration=20, operation="call"):
    return {"signal": "trace", "service": service, "kind": "CLIENT", "target": target,
            "outcome": outcome, "duration_ms": duration, "operation": operation}


class IntegratedValidationTests(unittest.TestCase):
    def test_integrated_pipeline_uses_only_resolved_json_writer_symbol(self):
        module_path = Path(__file__).with_name("run_integrated_validation.py")
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        unresolved_calls = [
            node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "write_json"
        ]
        self.assertEqual(unresolved_calls, [])

    def test_integrated_final_manifest_writer_uses_canonical_utf8_json_helper(self):
        self.assertIs(_write_json, write_json)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            value = {"status": "FINALIZING", "evaluation_status": "PASS",
                     "label": "validação", "nested": {"b": 2, "a": 1}}
            _write_json(path, value)
            encoded = path.read_bytes()
            self.assertEqual(json.loads(encoded), value)
            self.assertTrue(encoded.endswith(b"\n"))
            self.assertIn("validação".encode("utf-8"), encoded)
            self.assertEqual(encoded, json.dumps(value, ensure_ascii=False, sort_keys=True,
                                                 indent=2, allow_nan=False).encode("utf-8") + b"\n")

    def test_export_llm_responses_preserves_valid_and_semantically_invalid_outcomes(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            temporary_run = root / "private-llm"
            staged_run = root / "staged-run"
            protocol_path = root / "protocol.json"
            (staged_run / "llm" / "responses").mkdir(parents=True)
            case_rows = [
                ("case-valid", "F1-R01-A.json", "COMPLETED", "PASS"),
                ("case-invalid", "F1-R01-B.json", "SEMANTICALLY_INVALID", "FAIL"),
            ]
            private_map = []
            plan_cases = []
            for opaque_id, filename, status, semantic_status in case_rows:
                relative_input = f"inputs/{filename}"
                source_input = root / relative_input
                source_input.parent.mkdir(parents=True, exist_ok=True)
                source_input.write_text(json.dumps({"_request_id": opaque_id}) + "\n",
                                        encoding="utf-8")
                private_map.append({"opaque_case_id": opaque_id,
                                    "input_path": relative_input})
                plan_cases.append({"opaque_case_id": opaque_id})

                case_dir = temporary_run / "cases" / opaque_id
                case_dir.mkdir(parents=True)
                request_meta = {
                    "status": status,
                    "latency_ms": 1250.0,
                    "latency_clean": True,
                    "attempt_count": 1,
                    "attempts": [{"attempt": 1, "status": "COMPLETED"}],
                    "request_fingerprint": f"fingerprint-{opaque_id}",
                }
                (case_dir / "request-metadata.json").write_text(
                    json.dumps(request_meta), encoding="utf-8")
                response_meta = {
                    "response_id": f"resp-{opaque_id}",
                    "requested_model": "gpt-6-luna",
                    "returned_model": "gpt-6-luna",
                    "usage": {"input_tokens": 100, "cached_input_tokens": 10,
                              "output_tokens": 20, "reasoning_tokens": 5,
                              "total_tokens": 120, "estimated_cost_usd": "0.001"},
                }
                (case_dir / "response-metadata.json").write_text(
                    json.dumps(response_meta), encoding="utf-8")
                validation = {"status": semantic_status,
                              "errors": [] if semantic_status == "PASS" else ["UNKNOWN_EVIDENCE_ID"]}
                (case_dir / "validation.json").write_text(
                    json.dumps(validation), encoding="utf-8")
                raw_output = f"original raw output for {opaque_id}\n"
                (case_dir / "raw-output.txt").write_text(raw_output, encoding="utf-8")
                if semantic_status == "PASS":
                    prediction = {"hypotheses": [{"rank": 1}, {"rank": 2}, {"rank": 3}]}
                    (case_dir / "prediction.json").write_text(
                        json.dumps(prediction), encoding="utf-8")

                archived_dir = staged_run / "llm" / "inputs"
                archived_dir.mkdir(parents=True, exist_ok=True)
                request = {"request": opaque_id}
                archived_text = json.dumps(request, ensure_ascii=False, sort_keys=True,
                                           indent=2, allow_nan=False) + "\n"
                (archived_dir / filename).write_text(archived_text, encoding="utf-8")

            (temporary_run / "run-manifest.json").write_text(json.dumps({
                "case_map_private": private_map,
            }), encoding="utf-8")
            (temporary_run / "execution-plan.json").write_text(json.dumps({
                "cases": plan_cases,
            }), encoding="utf-8")

            with patch("run_integrated_validation.ROOT", root), \
                    patch("scripts.experiment.llm.protocol.build_openai_rca_request",
                          side_effect=lambda value: {"request": value["_request_id"]}):
                result = _export_llm_responses(temporary_run, staged_run, protocol_path)

            self.assertEqual(result["outcomes"], {
                "F1-R01-A": "COMPLETED",
                "F1-R01-B": "SEMANTICALLY_INVALID",
            })
            responses = staged_run / "llm" / "responses"
            self.assertEqual(sorted(path.name for path in responses.glob("*.json")),
                             ["F1-R01-A.json", "F1-R01-B.json"])
            valid = json.loads((responses / "F1-R01-A.json").read_text(encoding="utf-8"))
            self.assertEqual(valid["status"], "COMPLETED")
            self.assertEqual(valid["prediction"]["hypotheses"][0]["rank"], 1)
            self.assertEqual(valid["usage"]["input_tokens"], 100)
            self.assertEqual(valid["latency_ms"], 1250.0)
            invalid = json.loads((responses / "F1-R01-B.json").read_text(encoding="utf-8"))
            self.assertEqual(invalid["status"], "SEMANTICALLY_INVALID")
            self.assertEqual(invalid["validation"]["semantic"], "FAIL")
            self.assertEqual(invalid["validation"]["detail"]["errors"],
                             ["UNKNOWN_EVIDENCE_ID"])
            self.assertNotIn("prediction", invalid)
            self.assertEqual(invalid["raw_model_output"],
                             "original raw output for case-invalid\n")
            self.assertEqual(invalid["attempt_count"], 1)

    def test_offline_export_evaluation_manifest_hash_and_publication_path(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / "global-raw"
            for side in "AB":
                base = raw / f"dataset{side}"
                for modality in ("logs", "traces"):
                    (base / modality).mkdir(parents=True)
                    (base / modality / "chunk-0001.json").write_text("{}\n", encoding="utf-8")
                (base / "metrics.json").write_text("{}\n", encoding="utf-8")
            (raw / "manifest.json").write_text(json.dumps({"window": {"kind": "synthetic"}}),
                                                encoding="utf-8")
            (raw / "hashes.sha256").write_text("synthetic fixture\n", encoding="utf-8")

            temp_llm = root / "mock-llm"
            private_inputs = root / "private-inputs"
            input_paths_by_case = {}
            instances = {}
            for fault_number in range(1, 6):
                identity = f"F{fault_number}-R01"
                gt = {
                    "schema_version": "1.0", "fault_id": identity,
                    "fault_type": "cpu_saturation",
                    "root_location": {"kind": "service", "value": "service-alpha"},
                    "status": "RECOVERED", "execution_eligible": True,
                    "requested_start": "2026-01-01T00:00:00Z",
                    "confirmed_start": "2026-01-01T00:00:01Z",
                    "confirmed_end": "2026-01-01T00:00:20Z",
                }
                fault_root = root / "physical" / identity
                dataset_root = fault_root / "dataset"
                dataset_root.mkdir(parents=True)
                datasets = {}
                input_cases = {}
                for side in "AB":
                    dataset_path = dataset_root / f"dataset-{side}.json"
                    dataset_path.write_text(json.dumps({"schema_version": "1.0", "facts": []}),
                                            encoding="utf-8")
                    write_json(dataset_root / "build-manifest.json", {
                        "builder_script_sha256": "a" * 64,
                    })
                    datasets[side] = dataset_path
                    case_id = f"{identity}-{side}"
                    input_path = private_inputs / f"{case_id}.json"
                    payload = {"_test_payload_id": case_id}
                    write_json(input_path, payload)
                    input_paths_by_case[case_id] = input_path
                    input_cases[side] = input_path

                instances[identity] = {
                    "ground_truth": gt, "raw_bundle": raw,
                    "datasets": datasets, "inputs": input_cases,
                }

            staged = root / "staged"
            create_run_tree(staged, "run-001", 1, instances, raw, {
                "purpose": "offline_finalization_regression",
                "git_commit": "a" * 40,
                "started_at": "2026-01-01T00:00:00Z",
                "finished_at": "2026-01-01T00:01:00Z",
                "llm": {"provider": "OpenAI", "model": "synthetic-model"},
            })

            private_map = []
            plan_cases = []
            outcomes = {}
            for fault_number in range(1, 6):
                identity = f"F{fault_number}-R01"
                for side in "AB":
                    case = f"{identity}-{side}"
                    opaque = f"case-{fault_number:02d}-{side.lower()}"
                    source = input_paths_by_case[case]
                    relative_input = source.relative_to(root).as_posix()
                    private_map.append({"opaque_case_id": opaque, "input_path": relative_input})
                    plan_cases.append({"opaque_case_id": opaque})
                    case_dir = temp_llm / "cases" / opaque
                    case_dir.mkdir(parents=True)
                    invalid = case == "F5-R01-B"
                    status = "SEMANTICALLY_INVALID" if invalid else "COMPLETED"
                    write_json(case_dir / "request-metadata.json", {
                        "status": status, "attempt_count": 1, "attempts": [],
                        "latency_ms": 1000.0, "latency_clean": True,
                        "request_fingerprint": f"fingerprint-{case}",
                    })
                    write_json(case_dir / "response-metadata.json", {
                        "response_id": f"response-{case}",
                        "requested_model": "synthetic-model", "returned_model": "synthetic-model",
                        "usage": {"input_tokens": 100, "cached_input_tokens": 0,
                                  "output_tokens": 20, "reasoning_tokens": 5,
                                  "total_tokens": 120, "estimated_cost_usd": "0.001"},
                    })
                    write_json(case_dir / "validation.json", {
                        "status": "FAIL" if invalid else "PASS",
                        "code": "UNKNOWN_EVIDENCE_ID" if invalid else None,
                    })
                    (case_dir / "raw-output.txt").write_text(
                        f"original model output {case}\n", encoding="utf-8")
                    if not invalid:
                        write_json(case_dir / "prediction.json", {"hypotheses": [
                            {"rank": rank,
                             "location": {"kind": "service", "service": "service-alpha",
                                          "peer_service": None, "operation": None, "datastore": None},
                             "fault_type": "cpu_saturation"}
                            for rank in (1, 2, 3)
                        ]})
                    with patch("run_integrated_validation.ROOT", root), \
                            patch("scripts.experiment.llm.protocol.build_openai_rca_request",
                                  side_effect=lambda value: {"model": "synthetic-model",
                                                             "input": value["_test_payload_id"]}):
                        archived = staged / "llm" / "inputs" / f"{case}.json"
                        archived.write_text(json.dumps(
                            {"model": "synthetic-model", "input": case},
                            ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
                            encoding="utf-8")
                    outcomes[case] = status

            write_json(temp_llm / "run-manifest.json", {"case_map_private": private_map})
            write_json(temp_llm / "execution-plan.json", {"cases": plan_cases})
            with patch("run_integrated_validation.ROOT", root), \
                    patch("scripts.experiment.llm.protocol.build_openai_rca_request",
                          side_effect=lambda value: {"model": "synthetic-model",
                                                     "input": value["_test_payload_id"]}):
                exported = _export_llm_responses(temp_llm, staged, root / "protocol.json")
            self.assertEqual(exported["outcomes"], outcomes)
            self.assertEqual(len(list((staged / "llm/responses").glob("*.json"))), 10)

            manifest_path = staged / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "FINALIZING"
            _write_json(manifest_path, manifest)
            evaluation = write_evaluation(staged)
            self.assertEqual(evaluation["overall"]["treatment_B"]["accuracy_at_1_valid_cases"], 4)
            self.assertEqual(evaluation["cases"]["F5"]["R01"]["B"]["response_status"],
                             "SEMANTICALLY_INVALID")
            manifest["evaluation_status"] = "PASS"
            manifest["status"] = "SUCCESS"
            _write_json(manifest_path, manifest)
            outer_hashes(staged)
            self.assertEqual(verify_run(staged, 1)["hash_entries"],
                             len([p for p in staged.rglob("*") if p.is_file() and p.name != "hashes.sha256"]))
            published = publish_atomic(staged, root / "results", "run-001")
            self.assertTrue((published / "evaluation/results.json").is_file())
            self.assertEqual(json.loads((published / "manifest.json").read_text())["status"], "SUCCESS")
            self.assertEqual(len(list((published / "llm/responses").glob("*.json"))), 10)

    def make_global_raw_fixture(self, root, native_count=44):
        import hashlib

        source = root / "global-raw"
        (source / "datasetA").mkdir(parents=True)
        (source / "datasetB").mkdir()
        (source / "control").mkdir()
        native_responses = []
        for index in range(native_count):
            side = "A" if index % 2 == 0 else "B"
            relative = f"dataset{side}/sample-{index:02d}.json"
            path = source / relative
            path.write_bytes(f"native response {index}\n".encode())
            native_responses.append({
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "content_type": "application/json",
                "http_status": 200,
            })
        control = source / "control/workload.json"
        control.write_bytes(b'{"workload":"original bytes"}\n')
        control_artifacts = [{
            "path": "control/workload.json",
            "bytes": control.stat().st_size,
            "sha256": hashlib.sha256(control.read_bytes()).hexdigest(),
            "content_type": "application/json",
            "kind": "workload-control-record",
        }]
        manifest = source / "manifest.json"
        manifest.write_text(json.dumps({
            "format": "tcc-experiment-v2-raw-collection-v1",
            "native_responses": native_responses,
            "control_artifacts": control_artifacts,
        }, sort_keys=True) + "\n")
        entries = []
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            entries.append(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(source).as_posix()}"
            )
        (source / "hashes.sha256").write_text("\n".join(entries) + "\n")
        return source

    def test_reserves_ephemeral_local_port_for_prometheus_forward(self):
        self.assertGreater(reserve_local_port(), 0)

    def test_normal_measurement_starts_only_after_warmup_and_fresh_baseline(self):
        warmup = {
            "status": "PASS", "base_url": "http://127.0.0.1:18080",
            "started_at": "warmup-start", "warmup_finished_at": "warmup-end",
            "finished_at": "warmup-finish", "configuration": {"warmup_flows": 1},
            "warmup": [{"flow": 1}], "successful_warmup_flows": 1,
        }
        measured = {
            "status": "PASS", "base_url": "http://127.0.0.1:18080",
            "started_at": "measurement-start", "steady_started_at": "measurement-start",
            "finished_at": "measurement-end", "configuration": {"steady_flows": 3},
            "steady": [{"flow": 1}, {"flow": 2}, {"flow": 3}],
            "successful_steady_flows": 3,
        }
        readiness_report = {"status": "PASS", "ready_at": "baseline-ready"}
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "workload.json"
            with patch("run_integrated_validation.run_normal_workload_with_forward",
                       side_effect=[warmup, measured]) as workload, patch(
                           "run_integrated_validation.wait_for_duration_baseline",
                           return_value=readiness_report,
                       ):
                result = run_normal_baseline({}, {}, output)
            self.assertEqual(workload.call_args_list[0].args[-1], ("--steady-flows", "0"))
            self.assertEqual(workload.call_args_list[1].args[-1], ("--warmup-flows", "0"))
            self.assertEqual(result["measurement_started_at"], "measurement-start")
            self.assertEqual(result["warmup_finished_at"], "warmup-end")
            self.assertEqual(result["baseline_readiness"], readiness_report)
            self.assertEqual(json.loads(output.read_text())["measurement_started_at"],
                             "measurement-start")

    def test_baseline_readiness_uses_fresh_pod_or_exported_instance_samples(self):
        response = {
            "status": "success",
            "data": {"result": [
                {"metric": {"pod": "old"}, "value": [100, "99.5"]},
                {"metric": {"pod": "current"}, "value": [100, "100.2"]},
                {"metric": {}, "value": [100, "100.3"]},
            ]},
        }
        self.assertEqual(
            fresh_prometheus_instances(response, 100.0, "pod"),
            {"current": 100.2},
        )

    def test_baseline_readiness_rejects_missing_prometheus_identity(self):
        response = {
            "status": "success",
            "data": {"result": [{"metric": {}, "value": [100, "100.2"]}]},
        }
        self.assertEqual(fresh_prometheus_instances(response, 100.0, "exported_instance"), {})

    def test_integrated_runner_rejects_fault_windows_shorter_than_scrape_gate(self):
        from run_integrated_validation import main

        with patch("sys.argv", ["run_integrated_validation.py", "--output-dir", "/tmp/not-used",
                                "--active-seconds", "14"]):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)

    def test_fault_runner_package_entrypoint_imports_without_running_faults(self):
        result = run([sys.executable, "-m", "scripts.experiment.fault_smoke", "--help"], check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--fault", result.stdout)

    def test_f1_requires_cpu_and_request_duration_above_normal_baseline(self):
        a = [metric("cpu_utilization", "checkoutservice", 0.9),
             metric("request_duration", "checkoutservice", 0.8)]
        b = [metric("cpu_utilization", "checkoutservice", 0.8),
             metric("request_duration", "checkoutservice", 0.7)]
        baseline_a = [metric("cpu_utilization", "checkoutservice", 0.1),
                      metric("request_duration", "checkoutservice", 0.2)]
        baseline_b = [metric("cpu_utilization", "checkoutservice", 0.1),
                      metric("request_duration", "checkoutservice", 0.3)]
        result = validate_fault_facts("F1", a, b, baseline_a, baseline_b)
        self.assertEqual(result["scenarios"]["A"]["status"], "PASS")
        with self.assertRaises(IntegratedRunError):
            validate_fault_facts("F1", baseline_a, b, baseline_a, baseline_b)

    def test_f1_fails_when_normal_baseline_is_unavailable(self):
        fault = [metric("cpu_utilization", "checkoutservice", 0.8),
                 metric("request_duration", "checkoutservice", 0.8)]
        with self.assertRaisesRegex(IntegratedRunError, "normal baseline"):
            validate_fault_facts("F1", fault, fault, [], [])

    def test_f2_requires_delayed_checkout_to_payment_client_span(self):
        a = [{**span("checkoutservice", None, duration=150),
              "operation": "hipstershop.PaymentService/Charge"}]
        b = [{**span("checkoutservice", None, duration=180),
              "operation": "grpc.hipstershop.PaymentService/Charge"}]
        self.assertEqual(validate_fault_facts("F2", a, b)["scenarios"]["A"]["max_duration_ms"], 150)
        with self.assertRaises(IntegratedRunError):
            validate_fault_facts("F2", [span("checkoutservice", "paymentservice", duration=90)], b)

    def test_f3_and_f5_require_error_on_expected_dependency_edge(self):
        self.assertEqual(validate_fault_facts(
            "F3", [span("checkoutservice", "paymentservice", "ERROR")],
            [span("checkoutservice", "paymentservice", "ERROR")])['scenarios']['A']['status'], "PASS")
        self.assertEqual(validate_fault_facts(
            "F5", [span("cartservice", "redis-cart", "ERROR")],
            [span("cartservice", "redis-cart", "ERROR")])['scenarios']['B']['status'], "PASS")
        with self.assertRaises(IntegratedRunError):
            validate_fault_facts("F5", [span("cartservice", "redis-cart")],
                                 [span("cartservice", "redis-cart", "ERROR")])

    def test_edge_peer_encoded_in_operation_is_recognized(self):
        a = [span("cartservice", None, "ERROR", operation="redis-cart/Client.Get")]
        b = [span("cartservice", "redis-cart", "ERROR")]
        self.assertEqual(validate_fault_facts("F5", a, b)["scenarios"]["A"]["status"], "PASS")

    def test_f5_audit_evidence_accepts_peer_encoded_operation(self):
        with TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset-A.json"
            dataset.write_text(json.dumps({"schema_version": "1.0", "facts": [
                {"signal": "trace", "service": "cartservice", "operation": "redis-cart/Client.Get",
                 "outcome": "ERROR"},
            ]}))
            root = Path(temporary)
            fault_dir = "005_datastore_unavailable_r01"
            (root / fault_dir / "dataset").mkdir(parents=True)
            dataset.replace(root / fault_dir / "dataset/dataset-A.json")
            facts = dataset_evidence(root, fault_dir, "A", "F5", "trace")
            self.assertEqual(len(facts), 1)

    def test_global_raw_hash_verification_maps_manifest_name_after_packaging(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "datasetA").mkdir()
            data_path = root / "datasetA/metrics.json"
            data_path.write_text("native response")
            manifest = root / "global-raw-manifest.json"
            import hashlib
            manifest.write_text(json.dumps({
                "native_responses": [{
                    "path": "datasetA/metrics.json",
                    "bytes": data_path.stat().st_size,
                    "sha256": hashlib.sha256(data_path.read_bytes()).hexdigest(),
                }],
                "control_artifacts": [],
            }))
            entries = [
                f"{hashlib.sha256(data_path.read_bytes()).hexdigest()}  datasetA/metrics.json",
                f"{hashlib.sha256(manifest.read_bytes()).hexdigest()}  manifest.json",
            ]
            (root / "global-raw-hashes.sha256").write_text("\n".join(entries) + "\n")
            self.assertEqual(verify_global_raw_hashes(root), 2)
            data_path.write_text("changed")
            with self.assertRaises(IntegratedRunError):
                verify_global_raw_hashes(root)

    def test_global_raw_packaging_preserves_every_hash_listed_file_byte_for_byte(self):
        import hashlib

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_global_raw_fixture(root, native_count=44)
            staging = root / "staging"
            staging.mkdir()

            self.assertEqual(package_global_raw(source, staging), 46)
            original = (source / "control/workload.json").read_bytes()
            self.assertEqual((staging / "control/workload.json").read_bytes(), original)

            hash_entries = (staging / "global-raw-hashes.sha256").read_text().splitlines()
            self.assertEqual(len(hash_entries), 46)
            for line in hash_entries:
                expected, relative = line.split("  ", 1)
                path = staging / relative
                if relative == "manifest.json":
                    path = staging / "global-raw-manifest.json"
                self.assertTrue(path.exists(), relative)
                self.assertTrue(path.is_file(), relative)
                self.assertFalse(path.is_symlink(), relative)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), expected, relative)

    def test_global_raw_hash_verification_rejects_missing_or_modified_listed_files(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.make_global_raw_fixture(root, native_count=2)
            staging = root / "staging"
            staging.mkdir()
            package_global_raw(source, staging)

            (staging / "control/workload.json").unlink()
            with self.assertRaisesRegex(IntegratedRunError, "control/workload.json"):
                verify_global_raw_hashes(staging)
            (staging / "control/workload.json").write_bytes(b"changed bytes\n")
            with self.assertRaisesRegex(IntegratedRunError, "control/workload.json"):
                verify_global_raw_hashes(staging)

    def test_final_results_are_hash_checked_before_atomic_publication(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "stage"
            staging.mkdir()
            (staging / "evidence.json").write_text('{"status":"PASS"}\n')
            output = root / "results"

            result = finalize_results(staging, output, {"tested_commit": "test-sha"})

            self.assertEqual(result["status"], "PASS")
            self.assertTrue(output.is_dir())
            self.assertEqual(verify_hash_manifest(output), 2)

    def test_final_results_are_not_published_when_staging_copy_fails_integrity(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "stage"
            staging.mkdir()
            (staging / "evidence.json").write_text('{"status":"PASS"}\n')
            output = root / "results"
            verify = verify_hash_manifest

            def reject_publish_copy(path):
                if path.name.startswith(".results.publish-"):
                    raise IntegratedRunError("synthetic staging-copy hash failure")
                return verify(path)

            with patch("run_integrated_validation.verify_hash_manifest", side_effect=reject_publish_copy):
                with self.assertRaisesRegex(IntegratedRunError, "staging-copy hash failure"):
                    finalize_results(staging, output, {"tested_commit": "test-sha"})

            self.assertFalse(output.exists())
            self.assertEqual(list(root.glob(".results.publish-*")), [])

    def test_external_inventory_excludes_only_root_manifest_and_hashes_nested_manifests(self):
        import hashlib

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifacts = {
                "bundle-a/hashes.sha256": b"bundle a hash list\n",
                "bundle-a/raw.json": b"raw a\n",
                "bundle-b/hashes.sha256": b"bundle b hash list\n",
                "bundle-c/nested/hashes.sha256": b"nested bundle hash list\n",
            }
            for relative, payload in artifacts.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(payload)

            root_hash_list = root / "hashes.sha256"
            expected_paths = sorted(artifacts)
            self.assertEqual(
                [path.relative_to(root).as_posix() for path in external_inventory_paths(root)],
                expected_paths,
            )
            inventory = hash_inventory(root)
            self.assertEqual([entry["path"] for entry in inventory], expected_paths)
            for entry in inventory:
                self.assertEqual(
                    entry["sha256"], hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest()
                )
            self.assertNotIn(root_hash_list, external_inventory_paths(root))

            lines = write_hashes(root)
            self.assertEqual([line.split("  ", 1)[1] for line in lines], expected_paths)
            self.assertEqual(verify_hash_manifest(root), len(artifacts))
            self.assertNotIn("hashes.sha256", [line.split("  ", 1)[1] for line in lines])

    def test_outer_verifier_detects_modified_nested_hash_manifest(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "bundle/hashes.sha256"
            nested.parent.mkdir()
            nested.write_bytes(b"original nested manifest\n")
            (nested.parent / "raw.json").write_bytes(b"raw\n")
            write_hashes(root)

            nested.write_bytes(b"modified nested manifest\n")
            with self.assertRaisesRegex(IntegratedRunError, "artifact hashes mismatch.*bundle/hashes.sha256"):
                verify_hash_manifest(root)

    def test_final_results_publish_and_protect_nested_hash_manifests(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            staging = root / "stage"
            staging.mkdir()
            (staging / "evidence.json").write_text('{"status":"PASS"}\n')
            nested = staging / "fault/raw/hashes.sha256"
            nested.parent.mkdir(parents=True)
            nested.write_text("nested RAW manifest\n")
            (nested.parent / "chunk.json").write_text('{"raw":true}\n')
            output = root / "results"

            result = finalize_results(staging, output, {"tested_commit": "test-sha"})

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(verify_hash_manifest(output), 4)
            outer_paths = {
                line.split("  ", 1)[1]
                for line in (output / "hashes.sha256").read_text().splitlines()
            }
            self.assertIn("fault/raw/hashes.sha256", outer_paths)
            self.assertNotIn("hashes.sha256", outer_paths)
            declared = {entry["path"] for entry in json.loads((output / "manifest.json").read_text())["artifacts"]}
            self.assertIn("fault/raw/hashes.sha256", declared)

    def test_filesystem_diagnostic_preserves_and_reraises_original_permission_error(self):
        import run_integrated_validation as runner

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "teste/hashes.sha256"
            path.parent.mkdir()
            path.write_text("hash list\n")
            events = []
            error = PermissionError(13, "Permission denied", str(path))
            old_events = runner.FINALIZATION_EVENTS
            runner.FINALIZATION_EVENTS = events
            try:
                with self.assertRaises(PermissionError) as caught:
                    observe_filesystem("read audit root hash manifest", path, lambda: (_ for _ in ()).throw(error))
            finally:
                runner.FINALIZATION_EVENTS = old_events

            self.assertIs(caught.exception, error)
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["status"], "FAIL")
            self.assertEqual(event["exception_type"], "PermissionError")
            self.assertEqual(event["errno"], 13)
            self.assertEqual(event["before"]["absolute_path"], str(path.resolve()))
            self.assertEqual(event["before"]["cwd"], str(Path.cwd()))
            self.assertTrue(event["before"]["is_file"])
            self.assertIn("PermissionError", event["traceback"])
            self.assertIn("uid", event["before"]["process"])

    def test_finalization_stage_success_is_published_after_packages_and_json_is_last(self):
        import run_integrated_validation as runner

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            scratch = root / "scratch"
            scratch.mkdir()
            for name in ("results", "teste"):
                package = scratch / name
                package.mkdir()
                (package / "evidence.json").write_text('{"status":"PASS"}\n')
                write_hashes(package)
            publication_root = root / "experiment"
            (publication_root / "stages").mkdir(parents=True)
            real_replace = runner.os.replace
            calls = []

            def ordered_replace(source, target):
                calls.append(Path(target).name)
                return real_replace(source, target)

            with patch("run_integrated_validation.os.replace", side_effect=ordered_replace):
                result = publish_finalization(
                    scratch, publication_root,
                    {"tested_commit": "test-sha"}, {},
                    {"files_hashed": 1, "results_files_hashed": 1, "bytes": 128},
                )

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(verify_hash_manifest(publication_root / "results"), 1)
            self.assertEqual(verify_hash_manifest(publication_root / "teste"), 1)
            self.assertEqual(calls[-1], "12-final-experiment-validation.json")
            stage = json.loads((publication_root / "stages/12-final-experiment-validation.json").read_text())
            self.assertEqual(stage["overall"], "SUCCESS")
            self.assertFalse(scratch.exists())

    def test_finalization_failure_does_not_publish_stage_success(self):
        import run_integrated_validation as runner

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            scratch = root / "scratch"
            scratch.mkdir()
            for name in ("results", "teste"):
                package = scratch / name
                package.mkdir()
                (package / "evidence.json").write_text("evidence\n")
                write_hashes(package)
            publication_root = root / "experiment"
            stages = publication_root / "stages"
            stages.mkdir(parents=True)
            real_verify = verify_hash_manifest

            def fail_published_results(path):
                if Path(path) == publication_root / "results":
                    raise PermissionError(13, "Permission denied", "hashes.sha256")
                return real_verify(path)

            with patch("run_integrated_validation.verify_hash_manifest", side_effect=fail_published_results):
                with self.assertRaises(PermissionError):
                    publish_finalization(
                        scratch, publication_root, {"tested_commit": "test-sha"}, {},
                        {"files_hashed": 1, "results_files_hashed": 1, "bytes": 128},
                    )

            self.assertFalse((stages / "12-final-experiment-validation.json").exists())
            self.assertFalse((stages / "12-final-experiment-validation.md").exists())

    def test_temporary_cleanup_removes_raw_read_only_tree_without_changing_file_bytes(self):
        import hashlib

        manager = PreserveWorkOnError()
        root = manager.__enter__()
        try:
            raw = root / "raw/datasetA/traces/frontend"
            raw.mkdir(parents=True)
            manifest = raw / "hashes.sha256"
            chunk = raw / "chunk-000000.json"
            manifest.write_bytes(b"immutable manifest bytes\n")
            chunk.write_bytes(b'{"native":"response"}\n')
            original = {
                manifest.relative_to(root).as_posix():
                    (hashlib.sha256(manifest.read_bytes()).hexdigest(), manifest.stat().st_mode & 0o7777),
                chunk.relative_to(root).as_posix():
                    (hashlib.sha256(chunk.read_bytes()).hexdigest(), chunk.stat().st_mode & 0o7777),
            }
            for directory in (raw, raw.parent, raw.parent.parent, root / "raw"):
                directory.chmod(0o555)
            import run_integrated_validation as runner
            real_rmtree = runner.shutil.rmtree
            observed_before_delete = {}

            def capture_modes_then_remove(path):
                for relative, (expected_digest, expected_mode) in original.items():
                    candidate = Path(path) / relative
                    observed_before_delete[relative] = (
                        hashlib.sha256(candidate.read_bytes()).hexdigest(),
                        candidate.stat().st_mode & 0o7777,
                    )
                return real_rmtree(path)

            with patch("run_integrated_validation.shutil.rmtree", side_effect=capture_modes_then_remove):
                self.assertEqual(manager.__exit__(None, None, None), False)
            self.assertFalse(root.exists())
            self.assertEqual(observed_before_delete, original)
        except BaseException as exc:
            if root.exists():
                try:
                    remove_temporary_tree(root)
                except BaseException:
                    pass
            raise

    def test_temporary_cleanup_preserves_fatal_removal_error_without_retry(self):
        import run_integrated_validation as runner

        manager = PreserveWorkOnError()
        root = manager.__enter__()
        (root / "raw").mkdir()
        error = PermissionError(13, "Permission denied", "hashes.sha256")
        events = []
        previous_events = runner.FINALIZATION_EVENTS
        previous_failure = runner.LAST_FINALIZATION_FAILURE
        runner.FINALIZATION_EVENTS = events
        try:
            with patch("run_integrated_validation.shutil.rmtree", side_effect=error) as remove:
                with self.assertRaises(PermissionError) as caught:
                    manager.__exit__(None, None, None)
            self.assertIs(caught.exception, error)
            self.assertEqual(remove.call_count, 1)
            self.assertEqual(events[-1]["operation"], "remove disposable temporary tree")
            self.assertEqual(events[-1]["exception_type"], "PermissionError")
            self.assertIn("PermissionError", events[-1]["traceback"])
            self.assertEqual(runner.LAST_FINALIZATION_FAILURE["errno"], 13)
        finally:
            runner.FINALIZATION_EVENTS = previous_events
            runner.LAST_FINALIZATION_FAILURE = previous_failure
            if root.exists():
                remove_temporary_tree(root)

    def test_f4_requires_error_log_and_real_b_log_span_correlation(self):
        log_a = {"signal": "log", "service": "paymentservice", "message": "Payment charge failed",
                 "severity": "ERROR"}
        log_b = {**log_a, "trace_id": "a" * 32, "span_id": "b" * 16}
        trace_a = {"signal": "trace", "service": "paymentservice", "operation": "Charge",
                   "kind": "SERVER", "outcome": "ERROR"}
        trace_b = {**trace_a, "trace_id": "a" * 32, "span_id": "b" * 16}
        result = validate_fault_facts("F4", [log_a, trace_a], [log_b, trace_b])
        self.assertEqual(result["scenarios"]["B"]["log_trace_correlated"], True)
        with self.assertRaises(IntegratedRunError):
            validate_fault_facts("F4", [log_a, trace_a], [log_b, trace_a])


if __name__ == "__main__":
    unittest.main()
