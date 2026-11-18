from __future__ import annotations

import copy
import hashlib
import json
import socket
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.experiment.llm.protocol import (
    ROOT,
    build_openai_rca_request,
    canonical_json_bytes,
    verify_frozen_inputs,
    verify_protocol_artifacts,
)
from scripts.experiment.llm import protocol as protocol_module
from scripts.experiment.llm.response_validator import (
    RCAResponseValidationError,
    validate_rca_response,
)
from scripts.experiment.llm.test_support import install_synthetic_protocol
from scripts.experiment.llm.execution import build_execution_plan, write_or_verify_execution_plan


def artificial_input() -> dict:
    return {"schema_version": "1.0", "evidence": [
        {"id": "evidence-001", "signal": "metric", "service": "service-alpha"},
        {"id": "evidence-002", "signal": "trace", "service": "service-beta"},
    ]}


def hypothesis(rank: int, kind: str, fault: str = "network_latency",
               confidence: float = 0.8, evidence: list[str] | None = None) -> dict:
    locations = {
        "service": {"kind": "service", "service": "service-alpha", "peer_service": None,
                    "operation": None, "datastore": None},
        "service_edge": {"kind": "service_edge", "service": "service-alpha", "peer_service": "service-beta",
                         "operation": None, "datastore": None},
        "operation": {"kind": "operation", "service": "service-gamma", "peer_service": None,
                      "operation": "OperationX", "datastore": None},
        "datastore": {"kind": "datastore", "service": None, "peer_service": None,
                      "operation": None, "datastore": "datastore-x"},
    }
    return {"rank": rank, "location": locations[kind], "fault_type": fault,
            "confidence": confidence, "evidence_ids": evidence or [],
            "explanation": "The cited telemetry supports this localized hypothesis."}


def valid_response() -> dict:
    return {"hypotheses": [
        hypothesis(1, "service_edge", confidence=0.9, evidence=["evidence-001"]),
        hypothesis(2, "operation", fault="service_unavailable", confidence=0.5,
                   evidence=["evidence-002"]),
        hypothesis(3, "datastore", fault="datastore_unavailable", confidence=0.2),
    ]}


class ProtocolArtifactTests(unittest.TestCase):
    def setUp(self):
        install_synthetic_protocol(self)

    def test_prompt_schema_and_manifest_hashes_match(self):
        prompt, schema = verify_protocol_artifacts()
        protocol = json.loads(protocol_module.PROTOCOL_PATH.read_text())
        self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(), protocol["prompt_sha256"])
        self.assertEqual(hashlib.sha256(canonical_json_bytes(schema)).hexdigest(), protocol["schema_sha256"])
        self.assertEqual(protocol["prompt_version"], "rca-prompt-v1")
        self.assertEqual(protocol["schema_version"], "rca-schema-v1")
        self.assertEqual(protocol["structured_output_name"], "rca_diagnosis")
        self.assertIs(protocol["structured_output_strict"], True)
        self.assertEqual(protocol["protocol_version"], "llm-rca-pilot-v1")
        self.assertEqual(protocol["provider"], "OpenAI")
        self.assertEqual(protocol["api"], "Responses API")
        self.assertEqual(protocol["model"], "gpt-6-luna")
        self.assertEqual(protocol["reasoning_mode"], "standard")
        self.assertEqual(protocol["reasoning_effort"], "medium")
        self.assertIsNone(protocol["temperature"])
        self.assertIsNone(protocol["top_p"])
        self.assertEqual(protocol["service_tier"], "default")
        self.assertIs(protocol["store"], False)
        self.assertIs(protocol["stream"], False)
        self.assertEqual(protocol["text_verbosity"], "low")
        self.assertEqual(protocol["max_output_tokens"], 16384)
        self.assertEqual(protocol["tools"], [])
        self.assertEqual(protocol["pilot_fault_instances"], 5)
        self.assertEqual(protocol["pilot_rca_inputs"], 10)
        self.assertEqual(protocol["api_calls_per_input"], 1)
        self.assertIs(protocol["ground_truth_visible_to_model"], False)
        self.assertIs(protocol["treatment_visible_to_model"], False)
        self.assertIs(protocol["external_tools_enabled"], False)
        self.assertEqual(protocol["llm_calls_performed_during_13a"], 0)

    def test_frozen_prompt_has_no_case_specific_answer_or_treatment_label(self):
        prompt, _ = verify_protocol_artifacts()
        lowered = prompt.lower()
        for forbidden in ("scenario a", "scenario b", "treatment a", "treatment b",
                          "checkoutservice", "paymentservice", "redis-cart", "charge",
                          "f1", "f2", "f3", "f4", "f5"):
            self.assertNotIn(forbidden, lowered)
        # The generic term appears only in the instruction not to mention it.
        self.assertEqual(lowered.count("ground truth"), 1)
        self.assertNotIn("chain-of-thought field", lowered)

    def test_ten_frozen_inputs_match_byte_hash_manifest(self):
        verified = verify_frozen_inputs()
        self.assertEqual(len(verified), 10)
        lines = protocol_module.INPUT_HASHES_PATH.read_text().splitlines()
        self.assertEqual(len(lines), 10)
        self.assertEqual(len({line.split("  ", 1)[1] for line in lines}), 10)

    def test_blind_plan_is_deterministic_and_contains_no_private_mapping(self):
        plan = build_execution_plan()
        second = build_execution_plan()
        self.assertEqual(plan, second)
        self.assertEqual(len(plan["cases"]), 10)
        self.assertEqual([item["ordinal"] for item in plan["cases"]], list(range(1, 11)))
        self.assertNotIn("ground_truth", plan)
        self.assertNotIn("fault_id", json.dumps(plan))
        self.assertNotIn("treatment", json.dumps(plan))
        self.assertNotIn("path", json.dumps(plan))
        written, digest = write_or_verify_execution_plan()
        self.assertEqual(written, plan)
        self.assertEqual(len(digest), 64)


class RuntimeResourceTests(unittest.TestCase):
    def test_runtime_resources_load_from_any_cwd_and_hashes_are_frozen(self):
        install_synthetic_protocol(self)
        import os
        from scripts.experiment.llm import protocol as protocol_module
        before = os.getcwd()
        try:
            os.chdir("/")
            prompt, schema = verify_protocol_artifacts()
        finally:
            os.chdir(before)
        protocol = json.loads(protocol_module.PROTOCOL_PATH.read_text())
        self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(), protocol["prompt_sha256"])
        self.assertEqual(hashlib.sha256(canonical_json_bytes(schema)).hexdigest(), protocol["schema_sha256"])
        self.assertTrue(protocol_module.PROMPT_PATH.is_file())
        self.assertTrue(protocol_module.SCHEMA_PATH.is_file())


class RequestConstructionTests(unittest.TestCase):
    def setUp(self):
        install_synthetic_protocol(self)

    def test_request_has_frozen_config_and_only_model_visible_input(self):
        rca_input = artificial_input()
        before = copy.deepcopy(rca_input)
        request = build_openai_rca_request(rca_input)
        self.assertEqual(rca_input, before)
        self.assertEqual(request["model"], "gpt-6-luna")
        self.assertEqual(request["service_tier"], "default")
        self.assertIs(request["store"], False)
        self.assertIs(request["stream"], False)
        self.assertEqual(request["reasoning"], {"mode": "standard", "effort": "medium"})
        self.assertEqual(request["max_output_tokens"], 16384)
        self.assertEqual(request["text"]["verbosity"], "low")
        self.assertEqual(request["text"]["format"]["name"], "rca_diagnosis")
        self.assertIs(request["text"]["format"]["strict"], True)
        self.assertEqual(request["text"]["format"]["schema"], verify_protocol_artifacts()[1])
        self.assertEqual(request["tools"], [])
        self.assertNotIn("temperature", request)
        self.assertNotIn("top_p", request)
        self.assertNotIn("previous_response_id", request)
        self.assertNotIn("conversation", request)
        self.assertNotIn("metadata", request)
        self.assertEqual(set(request), {"model", "service_tier", "store", "stream", "reasoning",
                                       "max_output_tokens", "instructions", "input", "text", "tools"})
        self.assertEqual(request["input"], [{"role": "user", "content": json.dumps(
            rca_input, sort_keys=True, separators=(",", ":"), ensure_ascii=False)}])

    def test_all_ten_synthetic_inputs_build_blind_requests_without_paths_or_labels(self):
        from scripts.experiment.llm import protocol as protocol_module
        for line in protocol_module.INPUT_HASHES_PATH.read_text().splitlines():
            digest, relative = line.split("  ", 1)
            source = protocol_module.RESULTS_ROOT / relative.removeprefix("results/")
            raw = source.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
            request = build_openai_rca_request(json.loads(raw))
            self.assertEqual(set(request["input"][0]), {"role", "content"})
            self.assertNotIn(relative, request["input"][0]["content"])
            model_input = json.loads(request["input"][0]["content"])
            self.assertEqual(set(model_input), {"schema_version", "evidence"})
            forbidden_keys = {"scenario", "scenario_id", "treatment", "ground_truth", "groundtruth",
                              "fault_id", "fault_type", "run_id", "experiment_id", "expected_diagnosis"}

            def check_keys(value):
                if isinstance(value, dict):
                    for key, nested in value.items():
                        normalized = "".join(char for char in str(key).lower() if char.isalnum() or char == "_")
                        self.assertNotIn(normalized, forbidden_keys)
                        check_keys(nested)
                elif isinstance(value, list):
                    for nested in value:
                        check_keys(nested)

            check_keys(model_input)
            self.assertNotIn("scenario", request)
            self.assertNotIn("ground_truth", request)
            self.assertNotIn("fault_id", request)
            self.assertNotIn("expected_diagnosis", request)

    def test_request_construction_does_not_open_network(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network access attempted")):
            request = build_openai_rca_request(artificial_input())
        self.assertEqual(request["model"], "gpt-6-luna")


class ResponseSemanticValidationTests(unittest.TestCase):
    def validate(self, response):
        return validate_rca_response(response, artificial_input())

    def test_valid_service_location(self):
        response = valid_response()
        response["hypotheses"][0] = hypothesis(1, "service", confidence=0.9)
        self.assertIs(self.validate(response), response)

    def test_valid_service_edge_location(self):
        response = valid_response()
        self.assertIs(self.validate(response), response)

    def test_valid_operation_location(self):
        response = valid_response()
        response["hypotheses"][0] = hypothesis(1, "operation", confidence=0.9)
        self.assertIs(self.validate(response), response)

    def test_valid_datastore_location(self):
        response = valid_response()
        response["hypotheses"][0] = hypothesis(1, "datastore", confidence=0.9)
        self.assertIs(self.validate(response), response)

    def test_hypothesis_count_must_be_exactly_three(self):
        response = valid_response()
        response["hypotheses"] = response["hypotheses"][:2]
        with self.assertRaisesRegex(RCAResponseValidationError, "INVALID_SCHEMA"):
            self.validate(response)
        response = valid_response()
        response["hypotheses"].append(hypothesis(4, "service"))
        with self.assertRaisesRegex(RCAResponseValidationError, "INVALID_SCHEMA"):
            self.validate(response)

    def test_schema_rejects_invalid_fault_and_additional_properties(self):
        response = valid_response()
        response["hypotheses"][0]["fault_type"] = "memory_leak"
        with self.assertRaisesRegex(RCAResponseValidationError, "INVALID_FAULT_TYPE"):
            self.validate(response)
        response = valid_response()
        response["secret"] = "unexpected"
        with self.assertRaisesRegex(RCAResponseValidationError, "INVALID_SCHEMA"):
            self.validate(response)
        response = valid_response()
        response["hypotheses"][0]["extra"] = True
        with self.assertRaisesRegex(RCAResponseValidationError, "INVALID_SCHEMA"):
            self.validate(response)

    def test_ranks_must_be_in_order_1_2_3(self):
        response = valid_response()
        self.assertIs(self.validate(response), response)
        for ranks in ([1, 3, 2], [1, 1, 3], [2, 3, 1]):
            response = valid_response()
            for item, rank in zip(response["hypotheses"], ranks):
                item["rank"] = rank
            with self.assertRaisesRegex(RCAResponseValidationError, "INVALID_RANK_ORDER"):
                self.validate(response)

    def test_confidence_bounds_and_nonincreasing_order(self):
        response = valid_response()
        self.assertIs(self.validate(response), response)
        response = valid_response()
        response["hypotheses"][0]["confidence"] = 0.5
        response["hypotheses"][1]["confidence"] = 0.8
        with self.assertRaisesRegex(RCAResponseValidationError, "INVALID_CONFIDENCE_ORDER"):
            self.validate(response)
        for bad in (-0.1, 1.1, float("nan"), True):
            response = valid_response()
            response["hypotheses"][0]["confidence"] = bad
            with self.assertRaisesRegex(RCAResponseValidationError, "INVALID_CONFIDENCE"):
                self.validate(response)

    def test_all_location_kinds_reject_inconsistent_nullability(self):
        invalid_cases = [
            ("service", "operation", "OperationX"),
            ("service_edge", "datastore", "datastore-x"),
            ("operation", "peer_service", "service-beta"),
            ("datastore", "service", "service-alpha"),
        ]
        for kind, field, value in invalid_cases:
            response = valid_response()
            response["hypotheses"][0] = hypothesis(1, kind, confidence=0.9)
            response["hypotheses"][0]["location"][field] = value
            with self.subTest(kind=kind, field=field):
                with self.assertRaisesRegex(RCAResponseValidationError, "INVALID_LOCATION"):
                    self.validate(response)

    def test_known_unknown_and_empty_evidence_ids(self):
        response = valid_response()
        response["hypotheses"][0]["evidence_ids"] = ["evidence-001"]
        self.validate(response)
        response["hypotheses"][0]["evidence_ids"] = []
        self.validate(response)
        response["hypotheses"][0]["evidence_ids"] = ["evidence-999"]
        with self.assertRaisesRegex(RCAResponseValidationError, "UNKNOWN_EVIDENCE_ID"):
            self.validate(response)

    def test_duplicate_location_and_fault_type_is_rejected(self):
        response = valid_response()
        response["hypotheses"][1]["location"] = copy.deepcopy(response["hypotheses"][0]["location"])
        response["hypotheses"][1]["fault_type"] = response["hypotheses"][0]["fault_type"]
        response["hypotheses"][1]["confidence"] = 0.8
        with self.assertRaisesRegex(RCAResponseValidationError, "DUPLICATE_HYPOTHESIS"):
            self.validate(response)

    def test_schema_has_no_hidden_reasoning_fields(self):
        schema = verify_protocol_artifacts()[1]
        rendered = json.dumps(schema).lower()
        for field in ("reasoning", "chain_of_thought", "analysis", "thinking"):
            self.assertNotIn(field, rendered)
        self.assertEqual(set(schema["properties"]["hypotheses"]["items"]["properties"]), {
            "rank", "location", "fault_type", "confidence", "evidence_ids", "explanation"})


if __name__ == "__main__":
    unittest.main()
