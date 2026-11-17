from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx2 as httpx
import openai

from scripts.experiment.llm import credentials, runner
from scripts.experiment.llm.accounting import account_usage
from scripts.experiment.llm.execution import build_execution_plan, request_identity_fields
from scripts.experiment.llm.protocol import ROOT, sha256, verify_frozen_inputs, verify_protocol_artifacts


def response_value(status="completed", prediction=None, usage=None, response_id="resp-test",
                   model="gpt-6-luna"):
    if prediction is None:
        prediction = {"hypotheses": [
            {"rank": 1, "location": {"kind": "service", "service": "service-alpha",
             "peer_service": None, "operation": None, "datastore": None},
             "fault_type": "cpu_saturation", "confidence": 0.9,
             "evidence_ids": [], "explanation": "Metric evidence supports this."},
            {"rank": 2, "location": {"kind": "operation", "service": "service-gamma",
             "peer_service": None, "operation": "OperationX", "datastore": None},
             "fault_type": "network_latency", "confidence": 0.5,
             "evidence_ids": [], "explanation": "A second signal supports this."},
            {"rank": 3, "location": {"kind": "datastore", "service": None,
             "peer_service": None, "operation": None, "datastore": "datastore-x"},
             "fault_type": "datastore_unavailable", "confidence": 0.2,
             "evidence_ids": [], "explanation": "This is less likely."},
        ]}
    if status == "incomplete":
        output = []
    elif status == "refusal":
        output = [{"type": "message", "content": [{"type": "refusal", "refusal": "Synthetic refusal."}]}]
    else:
        text = prediction if isinstance(prediction, str) else json.dumps(prediction)
        output = [{"type": "message", "content": [{"type": "output_text", "text": text}]}]
    return {"id": response_id, "status": "incomplete" if status == "incomplete" else "completed",
            "model": model, "service_tier": "default", "created_at": 123,
            "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
            "output": output,
            "usage": usage or {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 100,
                                         "cache_write_tokens_details": {"ttl_30m_tokens": 100,
                                                                         "ttl_12h_tokens": 0}},
                "output_tokens": 30, "output_tokens_details": {"reasoning_tokens": 10},
                "total_tokens": 130,
            }}


class FakeResponses:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **request):
        self.calls.append(request)
        if not self.outcomes:
            raise AssertionError("fake response sequence exhausted")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.responses = FakeResponses(outcomes)


def connection_error():
    return openai.APIConnectionError(request=httpx.Request("POST", "https://api.openai.com/v1/responses"))


def status_error(code):
    request = httpx.Request("POST", "https://api.openai.com/v1/responses")
    response = httpx.Response(code, request=request, headers={"x-request-id": "req-synthetic"})
    return openai.APIStatusError("synthetic status", response=response, body={"error": {"code": "synthetic"}})


class ExecutionPlanTests(unittest.TestCase):
    def test_deterministic_hash_order_and_case_ids(self):
        first = build_execution_plan()
        second = build_execution_plan()
        self.assertEqual(first, second)
        self.assertEqual(first["logical_call_count"], 10)
        self.assertEqual([x["ordinal"] for x in first["cases"]], list(range(1, 11)))
        self.assertEqual([x["order_key"] for x in first["cases"]],
                         sorted(x["order_key"] for x in first["cases"]))
        for case in first["cases"]:
            input_hash = case["input_sha256"]
            self.assertEqual(case["order_key"], sha256(("llm-rca-pilot-v1-order:" + input_hash).encode()))
            self.assertEqual(case["opaque_case_id"], "case-" + sha256(
                ("llm-rca-pilot-v1-case:" + input_hash).encode())[:12])
            self.assertFalse(any("path" in key.lower() or "treatment" in key.lower()
                                 for key in case))

    def test_sol_protocol_isolated_inputs_and_model_only_configuration_change(self):
        sol_manifest = ROOT / "docs/experiment/validation/sol-e2e-v1/llm/protocol-sol-e2e-v1.json"
        luna_manifest = ROOT / "docs/experiment/llm/protocol-v1.json"
        sol = runner.configure_runner(sol_manifest)
        try:
            luna = json.loads(luna_manifest.read_text(encoding="utf-8"))
            self.assertEqual(sol["model"], "gpt-6.1-sol")
            for field in ("reasoning_mode", "reasoning_effort", "temperature",
                          "top_p", "service_tier", "store", "stream", "text_verbosity",
                          "max_output_tokens", "tools", "prompt_version", "prompt_sha256",
                          "schema_version", "schema_sha256", "structured_output_name",
                          "structured_output_strict"):
                self.assertEqual(sol[field], luna[field], field)
            self.assertEqual(len(verify_frozen_inputs()), 10)
            plan = build_execution_plan()
            self.assertEqual(len(plan["cases"]), 10)
            self.assertEqual(plan["protocol_version"], "llm-rca-sol-e2e-v1")
            self.assertEqual(len({row["opaque_case_id"] for row in plan["cases"]}), 10)
            for case in plan["cases"]:
                rca_input, _ = runner.load_case_input(case)
                request = runner.build_openai_rca_request(rca_input)
                self.assertEqual(request["model"], "gpt-6.1-sol")
                self.assertEqual(set(request), {
                    "model", "service_tier", "store", "stream", "reasoning",
                    "max_output_tokens", "instructions", "input", "text", "tools"})
                self.assertEqual(request["input"][0]["content"], json.dumps(
                    rca_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
                self.assertNotIn("opaque_case_id", request["input"][0]["content"])
                self.assertNotIn("ground-truth.json", request["input"][0]["content"])
        finally:
            runner.configure_runner(luna_manifest)

    def test_request_fingerprint_changes_only_with_scientific_input_identity(self):
        one = request_identity_fields("a" * 64)
        two = request_identity_fields("b" * 64)
        self.assertNotEqual(one["request_fingerprint"], two["request_fingerprint"])
        self.assertNotIn("api_key", json.dumps(one).lower())
        self.assertNotIn("timestamp", json.dumps(one).lower())


class CredentialTests(unittest.TestCase):
    def test_root_dotenv_loading_and_non_override_with_fake_secret(self):
        secret = "fake-secret-never-persist"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp)
            (root / ".env").write_text("OPENAI_API_KEY=fake\n", encoding="utf-8")
            def fake_load(dotenv_path, override, encoding):
                self.assertEqual(dotenv_path, root / ".env")
                self.assertFalse(override)
                os.environ.setdefault("OPENAI_API_KEY", secret)
            with patch.object(credentials, "verify_env_git_safety", return_value={
                "root_env_exists": True, "env_tracked": False, "env_ignored": True}), \
                 patch.object(credentials, "dotenv_values", return_value={"OPENAI_API_KEY": secret}), \
                 patch.object(credentials, "load_dotenv", side_effect=fake_load):
                result = credentials.load_root_credentials(root)
            self.assertTrue(result["openai_api_key_available"])
            self.assertTrue(result["root_env_key_present"])
            self.assertNotIn(secret, json.dumps(result))

    def test_existing_environment_value_is_not_overwritten(self):
        existing = "existing-fake-secret"
        file_secret = "different-fake-secret"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OPENAI_API_KEY": existing}, clear=True):
            root = Path(tmp)
            (root / ".env").write_text("OPENAI_API_KEY=fake\n", encoding="utf-8")
            def fake_load(dotenv_path, override, encoding):
                if override:
                    os.environ["OPENAI_API_KEY"] = file_secret
                else:
                    os.environ.setdefault("OPENAI_API_KEY", file_secret)
            with patch.object(credentials, "verify_env_git_safety", return_value={
                "root_env_exists": True, "env_tracked": False, "env_ignored": True}), \
                 patch.object(credentials, "dotenv_values", return_value={"OPENAI_API_KEY": file_secret}), \
                 patch.object(credentials, "load_dotenv", side_effect=fake_load):
                credentials.load_root_credentials(root)
            self.assertEqual(os.environ["OPENAI_API_KEY"], existing)

    def test_missing_env_or_key_fails_without_secret_detail(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with patch.object(credentials, "verify_env_git_safety", return_value={
                "root_env_exists": False, "env_tracked": False, "env_ignored": True}):
                with self.assertRaises(credentials.CredentialPreflightError) as raised:
                    credentials.require_openai_credential(Path(tmp))
            self.assertNotIn("fake-secret", str(raised.exception))

    def test_real_repository_env_is_ignored_and_not_tracked(self):
        status = credentials.verify_env_git_safety(ROOT)
        self.assertFalse(status["env_tracked"])
        self.assertTrue(status["env_ignored"])

    def test_runner_finds_repository_root_from_another_working_directory(self):
        entrypoint = ROOT / "scripts/experiment/llm/runner.py"
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, str(entrypoint), "freeze-plan"],
                                    cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["cases"], 10)
        self.assertEqual(report["event"], "EXECUTION_PLAN_FROZEN")

    def test_artifact_secret_scan_never_echoes_secret(self):
        secret = "fake-secret-marker-123"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"OPENAI_API_KEY": secret}, clear=True):
            root = Path(tmp)
            good = root / "safe.json"
            good.write_text('{"status":"PASS"}', encoding="utf-8")
            self.assertTrue(runner.assert_no_secret_in_artifacts([root]))
            good.write_text(secret, encoding="utf-8")
            with self.assertRaises(credentials.CredentialPreflightError) as raised:
                runner.assert_no_secret_in_artifacts([root])
            self.assertNotIn(secret, str(raised.exception))


class RetryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.rca_input = {"schema_version": "1.0", "evidence": [
            {"id": "evidence-001", "signal": "metric"}]}
        self.request = {"model": "gpt-6-luna", "input": []}

    def test_connection_error_retries_then_success(self):
        client = FakeClient([connection_error(), response_value()])
        sleeps = []
        outcome, attempts = runner._perform_logical_call(client, self.request,
                                                          self.rca_input, sleeper=sleeps.append)
        self.assertEqual(outcome["status"], "COMPLETED")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [1.0])

    def test_429_and_5xx_are_transient_but_401_is_not(self):
        for status, transient in ((429, True), (500, True), (408, True), (409, True), (401, False), (403, False)):
            self.assertEqual(runner._is_transient(status_error(status)), transient)
        quota_error = status_error(429)
        quota_error.body = {"error": {"code": "insufficient_quota"}}
        self.assertFalse(runner._is_transient(quota_error))

    def test_three_transient_failures_stop_and_preserve_transport_status(self):
        client = FakeClient([connection_error(), connection_error(), connection_error()])
        sleeps = []
        outcome, attempts = runner._perform_logical_call(client, self.request,
                                                          self.rca_input, sleeper=sleeps.append)
        self.assertEqual(outcome["status"], "TRANSPORT_FAILED")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_429_and_500_retry_once_then_succeed(self):
        for code in (429, 500):
            client = FakeClient([status_error(code), response_value()])
            outcome, attempts = runner._perform_logical_call(
                client, self.request, self.rca_input, sleeper=lambda _: None)
            self.assertEqual(outcome["status"], "COMPLETED")
            self.assertEqual(len(attempts), 2)

    def test_auth_invalid_request_and_quota_fail_without_retry(self):
        quota = status_error(429)
        quota.body = {"error": {"code": "insufficient_quota"}}
        for error in (status_error(401), status_error(403), status_error(400), quota):
            client = FakeClient([error])
            outcome, attempts = runner._perform_logical_call(
                client, self.request, self.rca_input, sleeper=lambda _: self.fail("should not sleep"))
            self.assertEqual(outcome["status"], "TRANSPORT_FAILED")
            self.assertEqual(len(attempts), 1)

    def test_refusal_incomplete_and_invalid_outputs_are_never_retried(self):
        for response, status in ((response_value("refusal"), "REFUSAL"),
                                 (response_value("incomplete"), "INCOMPLETE"),
                                 (response_value(prediction="not json"), "STRUCTURED_OUTPUT_INVALID")):
            client = FakeClient([response])
            outcome, attempts = runner._perform_logical_call(client, self.request, self.rca_input,
                                                              sleeper=lambda _: self.fail("should not sleep"))
            self.assertEqual(outcome["status"], status)
            self.assertEqual(len(attempts), 1)


class AccountingTests(unittest.TestCase):
    def test_cost_accounts_cached_uncached_write_and_output_once(self):
        usage = {"input_tokens": 1000,
                 "input_tokens_details": {"cached_tokens": 200, "cache_write_tokens": 300,
                    "cache_write_tokens_details": {"ttl_30m_tokens": 100, "ttl_12h_tokens": 200}},
                 "output_tokens": 400, "output_tokens_details": {"reasoning_tokens": 150},
                 "total_tokens": 1400}
        result = account_usage(usage, "default")
        self.assertEqual(result["reasoning_tokens"], 150)
        self.assertEqual(result["estimated_cost_usd"], "0.0003145")
        self.assertEqual(result["cost_status"], "ESTIMATED")

    def test_cost_uses_long_context_multiplier_and_missing_usage_stays_null(self):
        usage = {"input_tokens": 272001,
                 "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                 "output_tokens": 2, "total_tokens": 272003}
        result = account_usage(usage, "default")
        self.assertTrue(result["long_context_pricing_applied"])
        self.assertIsNotNone(result["estimated_cost_usd"])
        self.assertIsNone(account_usage(None, "default")["estimated_cost_usd"])

    def test_incomplete_cache_breakdown_keeps_cost_null(self):
        usage = {"input_tokens": 100, "input_tokens_details": {"cached_tokens": 0},
                 "output_tokens": 2, "total_tokens": 102}
        result = account_usage(usage, "default")
        self.assertIsNone(result["estimated_cost_usd"])
        self.assertEqual(result["cost_status"], "USAGE_BREAKDOWN_INCOMPLETE")


class MockedEndToEndTests(unittest.TestCase):
    def test_sol_protocol_ten_case_mocked_run_is_isolated(self):
        sol_manifest = ROOT / "docs/experiment/validation/sol-e2e-v1/llm/protocol-sol-e2e-v1.json"
        luna_manifest = ROOT / "docs/experiment/llm/protocol-v1.json"
        runner.configure_runner(sol_manifest)
        try:
            plan = build_execution_plan()
            fake = FakeClient([response_value(response_id=f"sol-resp-{i}", model="gpt-6.1-sol")
                               for i in range(10)])
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "sol-run"
                summary = runner.run_pilot(fake, output, plan, sleeper=lambda _: None)
                self.assertEqual(summary["logical_cases"], 10)
                self.assertEqual(summary["completed_valid"], 10)
                self.assertEqual(len(fake.responses.calls), 10)
                self.assertTrue(all(call["model"] == "gpt-6.1-sol" for call in fake.responses.calls))
                runner.verify_hash_manifest(output)
        finally:
            runner.configure_runner(luna_manifest)

    def setUp(self):
        verify_protocol_artifacts()
        verify_frozen_inputs()
        self.plan = build_execution_plan()

    def test_ten_case_mocked_e2e_persists_predictions_usage_and_hashes(self):
        client = FakeClient([response_value(response_id=f"resp-{i}") for i in range(10)])
        fake_secret = "offline-e2e-fake-secret"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            with patch.object(credentials, "verify_env_git_safety", return_value={
                "root_env_exists": True, "env_tracked": False, "env_ignored": True}), \
                 patch.object(credentials, "dotenv_values", return_value={"OPENAI_API_KEY": fake_secret}), \
                 patch.object(credentials, "load_dotenv", side_effect=lambda **kwargs:
                              os.environ.setdefault("OPENAI_API_KEY", fake_secret)):
                credential_status = credentials.load_root_credentials(ROOT)
            self.assertTrue(credential_status["openai_api_key_available"])
            run_dir = Path(tmp) / "pilot-v1-test-run"
            summary = runner.run_pilot(client, run_dir, self.plan)
            self.assertEqual(summary["logical_cases"], 10)
            self.assertEqual(summary["completed_valid"], 10)
            self.assertEqual(summary["total_transport_attempts"], 10)
            self.assertEqual(len(client.responses.calls), 10)
            self.assertEqual(summary["ground_truth_loaded"], False)
            self.assertEqual(summary["a_b_evaluation_performed"], False)
            runner.verify_hash_manifest(run_dir)
            self.assertTrue(runner.assert_no_secret_in_artifacts([run_dir]))
            manifest = json.loads((run_dir / "run-manifest.json").read_text())
            self.assertEqual(len(manifest["case_map_private"]), 10)
            for case in self.plan["cases"]:
                case_dir = run_dir / "cases" / case["opaque_case_id"]
                self.assertTrue((case_dir / "request-metadata.json").is_file())
                self.assertTrue((case_dir / "response-metadata.json").is_file())
                self.assertTrue((case_dir / "prediction.json").is_file())
                self.assertTrue((case_dir / "validation.json").is_file())
                request = client.responses.calls[case["ordinal"] - 1]
                self.assertNotIn(case["opaque_case_id"], json.dumps(request))
                self.assertNotIn("rca-input-", json.dumps(request))

    def test_mixed_outcomes_and_transient_retry(self):
        cases = [response_value(), response_value("refusal"), response_value("incomplete"),
                 response_value(prediction="invalid-json"), response_value()]
        # Make a complete semantic-invalid result, followed by a transport retry and valid outcomes.
        invalid = response_value()
        parsed = json.loads(invalid["output"][0]["content"][0]["text"])
        parsed["hypotheses"][0]["evidence_ids"] = ["evidence-999"]
        cases[4] = response_value(prediction=parsed)
        tail = [connection_error(), response_value(response_id="after-retry")]
        client = FakeClient(cases[:4] + [cases[4]] + tail + [response_value() for _ in range(4)])
        with tempfile.TemporaryDirectory() as tmp:
            summary = runner.run_pilot(client, Path(tmp) / "mixed", self.plan, sleeper=lambda _: None)
        self.assertEqual(summary["completed_valid"], 6)
        self.assertEqual(summary["refused"], 1)
        self.assertEqual(summary["incomplete"], 1)
        self.assertEqual(summary["structured_invalid"], 1)
        self.assertEqual(summary["semantic_invalid"], 1)
        self.assertEqual(summary["cases_with_retry"], 1)

    def test_transport_failure_stops_before_later_cases(self):
        client = FakeClient([connection_error(), connection_error(), connection_error()])
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "failed"
            with self.assertRaises(runner.PilotStopError):
                runner.run_pilot(client, run_dir, self.plan, sleeper=lambda _: None)
            self.assertEqual(len(client.responses.calls), 3)
            self.assertTrue((run_dir / "cases" / self.plan["cases"][0]["opaque_case_id"] /
                             "request-metadata.json").is_file())
            self.assertFalse((run_dir / "cases" / self.plan["cases"][1]["opaque_case_id"]).exists())
            runner.verify_hash_manifest(run_dir)

    def test_resume_skips_completed_case_and_continues_frozen_order(self):
        client = FakeClient([response_value(response_id="first"), status_error(401)])
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "resume"
            with self.assertRaises(runner.PilotStopError):
                runner.run_pilot(client, run_dir, self.plan, sleeper=lambda _: None)
            first_id, second_id = [item["opaque_case_id"] for item in self.plan["cases"][:2]]
            self.assertEqual(json.loads((run_dir / "cases" / first_id / "request-metadata.json").read_text())["status"],
                             "COMPLETED")
            self.assertEqual(json.loads((run_dir / "cases" / second_id / "request-metadata.json").read_text())["status"],
                             "TRANSPORT_FAILED")
            client2 = FakeClient([response_value(response_id=f"resumed-{i}") for i in range(9)])
            summary = runner.run_pilot(client2, run_dir, self.plan, resume=True, sleeper=lambda _: None)
            self.assertEqual(summary["logical_cases"], 10)
            self.assertEqual(len(client2.responses.calls), 9)
            self.assertEqual(client2.responses.calls[0]["model"], "gpt-6-luna")
            self.assertEqual(summary["total_transport_attempts"], 11)

    def test_control_smoke_is_separate_and_hashed(self):
        client = FakeClient([response_value()])
        with tempfile.TemporaryDirectory() as tmp:
            result = runner.run_control_smoke(client, Path(tmp) / "control-smoke")
            self.assertEqual(result["status"], "COMPLETED")
            self.assertFalse(result["included_in_pilot_results"])
            runner.verify_hash_manifest(Path(tmp) / "control-smoke")
            stored = json.loads((Path(tmp) / "control-smoke" / "control-smoke.json").read_text())
            self.assertFalse(stored["included_in_pilot_results"])
            preflight = json.loads((Path(tmp) / "control-smoke" / "preflight.json").read_text())
            self.assertEqual(preflight["execution_plan_cases"], 10)
            self.assertFalse(preflight["included_in_pilot_results"])

    def test_official_sdk_serializes_frozen_request_through_mock_transport_only(self):
        fake_secret = "sdk-test-key-must-not-be-persisted"
        payload = response_value()
        payload["object"] = "response"
        captured = {}
        def handler(request):
            captured["url"] = str(request.url)
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json=payload, request=request)
        http_client = httpx.Client(transport=httpx.MockTransport(handler))
        client = openai.OpenAI(api_key=fake_secret, max_retries=0, http_client=http_client)
        try:
            rca_input = {"schema_version": "1.0", "evidence": [
                {"id": "evidence-001", "signal": "metric"}]}
            response = client.responses.create(**runner.build_openai_rca_request(rca_input))
            self.assertEqual(response.status, "completed")
            self.assertEqual(captured["url"], "https://api.openai.com/v1/responses")
            body = captured["body"]
            self.assertEqual(body["reasoning"], {"mode": "standard", "effort": "medium"})
            self.assertTrue(body["text"]["format"]["strict"])
            self.assertNotIn("temperature", body)
            self.assertNotIn("top_p", body)
            self.assertNotIn(fake_secret, json.dumps(body))
        finally:
            client.close()


if __name__ == "__main__":
    unittest.main()
