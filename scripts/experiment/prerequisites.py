"""Offline checks for tools and Python packages required by a real run."""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = ROOT / "scripts/experiment/llm/requirements.txt"


class DependencyPreflightError(RuntimeError):
    """Required runtime tools, versions, or packages are unavailable."""


def check_git_available(environment: Mapping[str, str]) -> None:
    """Git is needed to verify .env safety before configuration is loaded."""
    if _resolve_command("git", environment.get("PATH")) is None:
        raise DependencyPreflightError(
            "Runtime dependency preflight failed:\n"
            "- Git: executable 'git' not found; install Git and ensure it is on PATH."
        )


def _resolve_command(command: str, search_path: str | None) -> str | None:
    if os.sep in command:
        candidate = Path(command)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
        return None
    return shutil.which(command, path=search_path)


def _probe(argv: list[str], env: Mapping[str, str], timeout: float = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False,
                          timeout=timeout, env=dict(env))


def _run_version(argv: list[str], env: Mapping[str, str]) -> tuple[str | None, str | None]:
    try:
        result = _probe(argv, env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, type(exc).__name__
    if result.returncode:
        return None, result.stderr.strip() or f"exit {result.returncode}"
    return result.stdout.strip(), None


def _python_packages() -> list[tuple[str, str]]:
    packages: list[tuple[str, str]] = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        requirement = line.strip()
        if not requirement or requirement.startswith("#"):
            continue
        name, separator, version = requirement.partition("==")
        if not separator or not name or not version:
            raise DependencyPreflightError(
                f"cannot validate unpinned Python dependency in {REQUIREMENTS.relative_to(ROOT)}: {requirement}"
            )
        packages.append((name, version))
    return packages


def check_python_dependencies() -> None:
    """Check pinned packages before importing modules that depend on them."""
    issues: list[str] = []
    for distribution, expected_version in _python_packages():
        try:
            actual_version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            issues.append(
                f"Python package {distribution}: missing; install requirements from "
                f"{REQUIREMENTS.relative_to(ROOT)}."
            )
            continue
        if actual_version != expected_version:
            issues.append(
                f"Python package {distribution}: found {actual_version}, expected pinned "
                f"{expected_version}; reinstall requirements from {REQUIREMENTS.relative_to(ROOT)}."
            )
    if issues:
        raise DependencyPreflightError("Python dependency preflight failed:\n" +
                                       "\n".join(f"- {issue}" for issue in issues))


def check_runtime_dependencies(environment: Mapping[str, str], pinned: Mapping[str, Any]) -> list[str]:
    """Raise one error listing all missing/mismatched dependencies; otherwise return notes."""
    issues: list[str] = []
    notes: list[str] = []
    command_env = {key: value for key, value in environment.items() if key != "OPENAI_API_KEY"}
    search_path = environment.get("PATH")
    expected = {
        environment.get("KIND_BIN", "kind"): ("Kind", str(pinned["KIND_VERSION"])),
        environment.get("KUBECTL_BIN", "kubectl"): ("kubectl", str(pinned["KUBECTL_VERSION"])),
        "docker": ("Docker CLI/Engine", str(pinned["DOCKER_SERVER_VERSION"])),
        "python3": ("Python runtime", "3.10+"),
        "curl": ("curl", None),
        "git": ("Git", None),
        "bash": ("Bash", None),
        "grep": ("grep", None),
        "sed": ("sed", None),
        "sort": ("sort", None),
        "wc": ("wc", None),
        "seq": ("seq", None),
        "sleep": ("sleep", None),
        "sha256sum": ("sha256sum", None),
        "tar": ("tar", None),
        "uname": ("uname", None),
        "mkdir": ("mkdir", None),
        "mktemp": ("mktemp", None),
        "rm": ("rm", None),
        "cp": ("cp", None),
        "basename": ("basename", None),
        "dirname": ("dirname", None),
        "awk": ("awk", None),
        "find": ("find", None),
    }
    resolved: dict[str, str] = {}
    for command, (label, version) in expected.items():
        path = _resolve_command(command, search_path)
        if path is None:
            if label == "Kind":
                issues.append(
                    f"Kind executable not found: {command!r}; install pinned Kind {version} "
                    "and ensure it is on PATH or set KIND_BIN to its absolute path."
                )
            else:
                suffix = f" (expected {version})" if version else ""
                issues.append(f"{label}: executable {command!r} not found{suffix}.")
        else:
            resolved[command] = path

    if sys.version_info < (3, 10):
        issues.append(f"Experiment Python: expected 3.10+, found {platform.python_version()}.")

    try:
        check_python_dependencies()
    except DependencyPreflightError as exc:
        issues.extend(str(exc).splitlines()[1:])

    kind_bin = environment.get("KIND_BIN", "kind")
    if kind_bin in resolved:
        output, error = _run_version([kind_bin, "version"], command_env)
        if error:
            issues.append(f"Kind {pinned['KIND_VERSION']}: could not run {kind_bin!r} ({error}).")
        elif f"kind {pinned['KIND_VERSION']}" not in (output or ""):
            issues.append(f"Kind: found {output!r}; expected pinned {pinned['KIND_VERSION']}.")

    kubectl_bin = environment.get("KUBECTL_BIN", "kubectl")
    if kubectl_bin in resolved:
        output, error = _run_version([kubectl_bin, "version", "--client", "-o", "json"], command_env)
        if error:
            issues.append(f"kubectl {pinned['KUBECTL_VERSION']}: client version check failed ({error}).")
        else:
            try:
                actual = json.loads(output or "")["clientVersion"]["gitVersion"]
            except (json.JSONDecodeError, KeyError, TypeError):
                actual = "unparseable version output"
            if actual != pinned["KUBECTL_VERSION"]:
                issues.append(f"kubectl: found {actual!r}; expected pinned {pinned['KUBECTL_VERSION']}.")

    if "docker" in resolved:
        output, error = _run_version(["docker", "version", "--format", "{{.Server.Version}}"], command_env)
        if error or not output:
            issues.append("Docker daemon: unavailable; start Docker Desktop/Engine and verify it with `docker version`.")
        elif output != pinned["DOCKER_SERVER_VERSION"]:
            issues.append(f"Docker Engine: found {output!r}; expected pinned {pinned['DOCKER_SERVER_VERSION']}.")
        else:
            _, info_error = _run_version(["docker", "info"], command_env)
            if info_error:
                issues.append("Docker daemon: `docker info` failed; start the daemon and verify Docker access.")

    if "python3" in resolved:
        output, error = _run_version(["python3", "--version"], command_env)
        if error:
            issues.append(f"Python runtime: `python3 --version` failed ({error}).")
        else:
            match = re.search(r"Python\s+(\d+)\.(\d+)", output or "")
            if not match or tuple(map(int, match.groups())) < (3, 10):
                issues.append(f"Python runtime: found {output!r}; expected Python 3.10 or newer.")

    configured_helm = environment.get("HELM_BIN")
    helm_bin = configured_helm or _resolve_command("helm", search_path)
    if configured_helm and _resolve_command(configured_helm, search_path) is None:
        issues.append(f"Helm: HELM_BIN={configured_helm!r} does not identify an executable.")
    elif helm_bin:
        output, error = _run_version([helm_bin, "version", "--template", "{{.Version}}"], command_env)
        expected_helm = f"v{pinned['HELM_VERSION']}"
        if error:
            issues.append(f"Helm: could not run {helm_bin!r} ({error}); expected {expected_helm}.")
        elif output != expected_helm:
            issues.append(f"Helm: found {output!r}; expected pinned {expected_helm}.")
        else:
            notes.append(f"Helm {expected_helm}: available.")
    else:
        if platform.machine() not in {"x86_64", "aarch64", "arm64"}:
            issues.append(f"Helm: auto-install does not support architecture {platform.machine()!r}.")
        else:
            notes.append(f"Helm {pinned['HELM_VERSION']}: not installed; setup will download the pinned release.")

    if issues:
        raise DependencyPreflightError("Runtime dependency preflight failed:\n" +
                                       "\n".join(f"- {issue}" for issue in issues))
    return notes
