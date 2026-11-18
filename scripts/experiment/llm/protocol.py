"""Offline protocol artifacts and request construction. This module never performs I/O over a network."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[3]
ARTIFACTS = ROOT / "scripts/experiment/llm/resources"
PROMPT_PATH = ARTIFACTS / "rca-prompt-v1.txt"
SCHEMA_PATH = ARTIFACTS / "rca-schema-v1.json"
PROTOCOL_PATH = ARTIFACTS / "protocol-v1.json"
INPUT_HASHES_PATH = ROOT / "artifacts/experiment/current-inputs/rca-inputs.sha256"
RESULTS_ROOT = ROOT / "artifacts/experiment/current-inputs"


def configure_protocol(path: Path | str) -> dict[str, Any]:
    """Select a frozen protocol manifest and its input namespace for this process."""
    global ARTIFACTS, PROMPT_PATH, SCHEMA_PATH, PROTOCOL_PATH, INPUT_HASHES_PATH, RESULTS_ROOT
    selected = Path(path)
    if not selected.is_absolute():
        selected = ROOT / selected
    selected = selected.resolve(strict=True)
    protocol = json.loads(selected.read_text(encoding="utf-8"))
    if not isinstance(protocol, dict):
        raise ProtocolError("protocol manifest must be a JSON object")
    PROTOCOL_PATH = selected
    ARTIFACTS = selected.parent
    def configured_artifact(value: str, legacy_name: str) -> Path:
        selected_path = Path(value or legacy_name)
        return selected_path if selected_path.is_absolute() else ARTIFACTS / selected_path
    PROMPT_PATH = configured_artifact(protocol.get("prompt_file", ""), "rca-prompt-v1.txt")
    SCHEMA_PATH = configured_artifact(protocol.get("schema_file", ""), "rca-schema-v1.json")
    RESULTS_ROOT = ROOT / protocol.get("input_root", "artifacts/experiment/current-inputs")
    input_manifest = Path(protocol.get("input_hash_manifest", "rca-inputs.sha256"))
    INPUT_HASHES_PATH = input_manifest if input_manifest.is_absolute() else RESULTS_ROOT / input_manifest
    return protocol


class ProtocolError(ValueError):
    """Raised when frozen protocol artifacts or inputs do not match their hashes."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _load_protocol() -> dict[str, Any]:
    try:
        protocol = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"cannot read protocol manifest: {exc}") from exc
    if not isinstance(protocol, dict):
        raise ProtocolError("protocol manifest must be a JSON object")
    return protocol


def verify_protocol_artifacts() -> tuple[str, dict[str, Any]]:
    """Verify the sole prompt/schema source of truth and return their contents."""
    protocol = _load_protocol()
    try:
        prompt_bytes = PROMPT_PATH.read_bytes()
        schema_bytes = SCHEMA_PATH.read_bytes()
        schema = json.loads(schema_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"cannot read frozen prompt/schema: {exc}") from exc

    actual_prompt_hash = sha256(prompt_bytes)
    if actual_prompt_hash != protocol.get("prompt_sha256"):
        raise ProtocolError("frozen prompt SHA-256 does not match protocol manifest")
    actual_schema_hash = sha256(canonical_json_bytes(schema))
    if actual_schema_hash != protocol.get("schema_sha256"):
        raise ProtocolError("frozen schema SHA-256 does not match protocol manifest")
    if protocol.get("prompt_version") != "rca-prompt-v1":
        raise ProtocolError("unexpected prompt version")
    if protocol.get("schema_version") != "rca-schema-v1":
        raise ProtocolError("unexpected schema version")
    if protocol.get("structured_output_name") != "rca_diagnosis" or protocol.get("structured_output_strict") is not True:
        raise ProtocolError("structured-output metadata differs from the frozen contract")
    return prompt_bytes.decode("utf-8"), schema


def _input_hash_entries() -> dict[str, str]:
    try:
        raw = INPUT_HASHES_PATH.read_bytes()
        lines = raw.decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"cannot read RCA Input hash manifest: {exc}") from exc
    entries: dict[str, str] = {}
    for line in lines:
        try:
            digest, relative = line.split("  ", 1)
        except ValueError as exc:
            raise ProtocolError(f"invalid RCA Input hash entry: {line!r}") from exc
        posix = PurePosixPath(relative)
        if (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                or not relative or posix.is_absolute() or ".." in posix.parts
                or relative in entries):
            raise ProtocolError(f"invalid or duplicate RCA Input path/hash: {relative!r}")
        entries[relative] = digest
    return entries


def verify_frozen_inputs() -> dict[str, str]:
    """Verify the current execution's frozen RCA Inputs; never reads Ground Truth."""
    protocol = _load_protocol()
    entries = _input_hash_entries()
    expected_count = protocol.get("pilot_rca_inputs")
    if type(expected_count) is not int or expected_count < 1 or len(entries) != expected_count:
        raise ProtocolError(f"expected {expected_count!r} frozen RCA Inputs, found {len(entries)}")
    verified: dict[str, str] = {}
    for relative, expected in entries.items():
        path = (ROOT / relative).resolve(strict=True)
        if not path.is_relative_to(RESULTS_ROOT.resolve()):
            raise ProtocolError(f"RCA Input path escapes the frozen results tree: {relative}")
        try:
            actual = sha256(path.read_bytes())
        except OSError as exc:
            raise ProtocolError(f"cannot read frozen RCA Input {relative}: {exc}") from exc
        if actual != expected:
            raise ProtocolError(f"frozen RCA Input SHA-256 mismatch: {relative}")
        verified[relative] = actual
    return verified


def freeze_current_inputs() -> dict[str, str]:
    """Create the per-execution input hash list from newly generated RCA Inputs."""
    if INPUT_HASHES_PATH.exists():
        raise ProtocolError("RCA Input hash manifest already exists; refusing to replace frozen inputs")
    if not RESULTS_ROOT.is_dir():
        raise ProtocolError("current experiment results directory does not exist")
    paths = sorted(RESULTS_ROOT.rglob("*.json"))
    paths = [path for path in paths if (
        path.name in {"rca-input-A.json", "rca-input-B.json"}
        or re.fullmatch(r"F[1-5]-R[0-9]{2,}-[AB]\.json", path.name, re.ASCII)
    )]
    expected_count = _load_protocol().get("pilot_rca_inputs")
    if type(expected_count) is not int or len(paths) != expected_count:
        raise ProtocolError(f"expected {expected_count!r} current RCA Inputs, found {len(paths)}")
    lines = []
    for path in paths:
        relative = path.relative_to(ROOT).as_posix()
        lines.append(f"{sha256(path.read_bytes())}  {relative}")
    INPUT_HASHES_PATH.parent.mkdir(parents=True, exist_ok=True)
    INPUT_HASHES_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return verify_frozen_inputs()


def extract_evidence_ids(rca_input: Mapping[str, Any]) -> set[str]:
    if set(rca_input) != {"schema_version", "evidence"} or rca_input.get("schema_version") != "1.0":
        raise ProtocolError("RCA Input must contain only schema_version=1.0 and evidence")
    evidence = rca_input.get("evidence")
    if not isinstance(evidence, list):
        raise ProtocolError("RCA Input evidence must be an array")
    identifiers: set[str] = set()
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise ProtocolError(f"RCA Input evidence[{index}] must be an object")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise ProtocolError(f"RCA Input evidence[{index}] has no valid id")
        if identifier in identifiers:
            raise ProtocolError(f"RCA Input contains duplicate evidence id: {identifier}")
        identifiers.add(identifier)
    return identifiers


def build_openai_rca_request(rca_input: Mapping[str, Any]) -> dict[str, Any]:
    """Build a Responses API request value without constructing a client or making a call."""
    prompt, schema = verify_protocol_artifacts()
    extract_evidence_ids(rca_input)
    protocol = _load_protocol()
    user_content = json.dumps(rca_input, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False, allow_nan=False)
    return {
        "model": protocol["model"],
        "service_tier": protocol["service_tier"],
        "store": protocol["store"],
        "stream": protocol["stream"],
        "reasoning": {
            "mode": protocol["reasoning_mode"],
            "effort": protocol["reasoning_effort"],
        },
        "max_output_tokens": protocol["max_output_tokens"],
        "instructions": prompt,
        "input": [{"role": "user", "content": user_content}],
        "text": {
            "verbosity": protocol["text_verbosity"],
            "format": {
                "type": "json_schema",
                "name": protocol["structured_output_name"],
                "strict": protocol["structured_output_strict"],
                "schema": schema,
            },
        },
        "tools": [],
    }
