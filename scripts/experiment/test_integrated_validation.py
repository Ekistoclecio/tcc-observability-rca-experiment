import unittest
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from run_integrated_validation import (
    IntegratedRunError,
    dataset_evidence,
    external_inventory_paths,
    finalize_results,
    fresh_prometheus_instances,
    hash_inventory,
    package_global_raw,
    reserve_local_port,
    run,
    run_normal_baseline,
    validate_fault_facts,
    verify_global_raw_hashes,
    verify_hash_manifest,
    write_hashes,
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
