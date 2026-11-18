"""Synthetic, isolated protocol inputs for offline LLM runner tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from . import execution, protocol, runner


def install_synthetic_protocol(testcase: Any) -> Path:
    """Install ten artificial A/B inputs under a temporary root for one test."""
    temporary = tempfile.TemporaryDirectory(prefix="experiment-llm-test-")
    testcase.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    results = root / "results"
    prompt = protocol.ROOT / "scripts/experiment/llm/resources/rca-prompt-v1.txt"
    schema = protocol.ROOT / "scripts/experiment/llm/resources/rca-schema-v1.json"
    prompt_bytes = prompt.read_bytes()
    schema_obj = json.loads(schema.read_text(encoding="utf-8"))
    canonical_schema = json.dumps(schema_obj, sort_keys=True, separators=(",", ":"),
                                  ensure_ascii=False, allow_nan=False).encode("utf-8")
    input_rows = []
    for index in range(1, 6):
        for treatment in ("A", "B"):
            path = results / f"synthetic-{index:02d}" / f"rca-input-{treatment}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            value = {"schema_version": "1.0", "evidence": [
                {"id": f"evidence-{index:03d}-{treatment}", "signal": "metric",
                 "service": f"service-{index:02d}"}
            ]}
            raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
            path.write_bytes(raw)
            input_rows.append(f"{hashlib.sha256(raw).hexdigest()}  {path.relative_to(root).as_posix()}")
    (results / "rca-inputs.sha256").write_text("\n".join(input_rows) + "\n", encoding="utf-8")
    manifest = {
        "api": "Responses API", "api_calls_per_input": 1,
        "expected_hypotheses": 3, "external_tools_enabled": False,
        "ground_truth_visible_to_model": False, "treatment_visible_to_model": False,
        "llm_calls_performed_during_13a": 0, "input_hash_manifest": "rca-inputs.sha256",
        "input_root": str(results), "interpretation": "synthetic_offline_test",
        "max_output_tokens": 16384, "model": "gpt-6-luna",
        "pilot_fault_instances": 5, "pilot_rca_inputs": 10,
        "previous_response_id": "NONE", "prompt_file": str(prompt),
        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "prompt_version": "rca-prompt-v1", "protocol_version": "llm-rca-pilot-v1",
        "provider": "OpenAI", "reasoning_effort": "medium", "reasoning_mode": "standard",
        "run_output_root": str(root / "mock-run"),
        "control_output_root": str(root / "control-smoke"),
        "execution_plan_file": str(results / "execution-plan-v1.json"),
        "pricing_file": str(protocol.ROOT / "scripts/experiment/llm/resources/pricing-luna-default-v1.json"),
        "schema_file": str(schema), "schema_sha256": hashlib.sha256(canonical_schema).hexdigest(),
        "schema_version": "rca-schema-v1", "service_tier": "default", "store": False,
        "stream": False, "structured_output_name": "rca_diagnosis",
        "structured_output_strict": True, "temperature": None, "text_verbosity": "low",
        "tools": [], "top_p": None,
    }
    protocol_path = root / "protocol.json"
    protocol_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    previous_root = protocol.ROOT
    previous_execution_root = execution.ROOT
    protocol.ROOT = root
    runner.configure_runner(protocol_path)
    execution.ROOT = root

    def restore_runtime_configuration() -> None:
        protocol.ROOT = previous_root
        execution.ROOT = previous_execution_root
        runner.configure_runner(previous_root / "scripts/experiment/llm/resources/protocol-v1.json")

    testcase.addCleanup(restore_runtime_configuration)
    return root
