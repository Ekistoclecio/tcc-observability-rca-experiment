"""User-facing, offline-validatable experiment entrypoint."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from dotenv import dotenv_values, load_dotenv

from scripts.experiment.configuration import (
    ConfigurationError, ROOT, format_plan, resolve_config,
)
from scripts.experiment.llm.credentials import verify_env_git_safety
from scripts.experiment.prerequisites import (
    DependencyPreflightError, check_git_available, check_runtime_dependencies,
)


def _run(script: Path, env: dict[str, str], label: str) -> None:
    print(f"\n[{label}] Iniciando...", flush=True)
    subprocess.run(["bash", str(script)], cwd=ROOT, env=env, check=True)
    print(f"[{label}] Concluído.", flush=True)


def _active_kind_clusters(kind_bin: str, expected_version: str) -> list[str]:
    try:
        result = subprocess.run([kind_bin, "get", "clusters"], cwd=ROOT,
                                text=True, capture_output=True, check=True)
    except FileNotFoundError:
        raise ConfigurationError(
            f"Kind executable not found: {kind_bin!r}. Install the pinned Kind {expected_version} "
            "and ensure it is on PATH, or set KIND_BIN to its absolute path. "
            "See docs/experiment/README.md."
        ) from None
    return result.stdout.split()


def run_experiment(env_file: Path | None = None, dry_run: bool = False) -> int:
    selected_env = (env_file or ROOT / ".env").resolve()
    if not selected_env.is_relative_to(ROOT):
        raise ConfigurationError("configuration file must be inside the repository root")
    if not selected_env.is_file():
        raise ConfigurationError(f"repository environment file not found: {selected_env.name}; copy .env.example to .env")
    check_git_available(os.environ)
    try:
        verify_env_git_safety(ROOT)
    except Exception as exc:
        raise ConfigurationError(str(exc)) from None
    # Do not override values supplied by the invoking process.
    load_dotenv(dotenv_path=selected_env, override=False, encoding="utf-8")
    config = resolve_config(ROOT, selected_env, os.environ)
    print(format_plan(config), flush=True)
    if dry_run:
        print("\nDRY-RUN: no cluster, fault, OpenAI request, or result run created.")
        return 0

    env = os.environ.copy()
    kind_bin = env.get("KIND_BIN", "kind")
    versions = ROOT / "config/experiment/environment/versions.env"
    pinned = dotenv_values(versions, encoding="utf-8")
    if not pinned.get("KIND_VERSION") or not pinned.get("KIND_CLUSTER_NAME"):
        raise ConfigurationError("experiment environment version manifest is missing Kind settings")
    notes = check_runtime_dependencies(env, pinned)
    for note in notes:
        print(f"DEPENDENCY NOTE: {note}", flush=True)
    clusters = _active_kind_clusters(kind_bin, pinned["KIND_VERSION"])
    if clusters:
        raise ConfigurationError(
            "exclusive Kind runtime required; remove conflicting clusters before running: "
            + ", ".join(clusters)
        )
    print("\n[Preflight] Dependências válidas; runtime Kind exclusivo confirmado.", flush=True)

    cluster_name = pinned["KIND_CLUSTER_NAME"]
    creation_started = False
    try:
        creation_started = True
        _run(ROOT / "scripts/experiment/environment/create.sh", env, "Ambiente 1/5 — Kind")
        _run(ROOT / "scripts/experiment/environment/install-chaos-mesh.sh", env,
             "Ambiente 2/5 — Chaos Mesh")
        _run(ROOT / "scripts/experiment/environment/deploy.sh", env,
             "Ambiente 3/5 — aplicação e provenance")
        _run(ROOT / "scripts/experiment/observability-a/deploy.sh", env,
             "Ambiente 4/5 — observabilidade A")
        _run(ROOT / "scripts/experiment/observability-b/deploy.sh", env,
             "Ambiente 5/5 — observabilidade B")
        experiment_scripts = str(ROOT / "scripts/experiment")
        if experiment_scripts not in sys.path:
            sys.path.insert(0, experiment_scripts)
        from scripts.experiment.run_integrated_validation import execute
        result = execute((ROOT / "docs/experiment/results").resolve(),
                         fault_repetitions=config.fault_repetitions,
                         purpose="configured_experiment_run", experiment_config=config)
        print(f"\n[Concluído] Run {result['run_id']} publicada em {result['result_path']}.",
              flush=True)
        print(f"[Concluído] Fault instances: {result['physical_fault_instances']}; "
              f"RCA inputs: {result['rca_inputs']}; integrity: PASS.", flush=True)
        if config.rca.enabled:
            pilot = result.get("pilot", {})
            print("[Concluído] Diagnósticos: "
                  f"{pilot.get('completed_valid', 0)} válidos, "
                  f"{pilot.get('semantic_invalid', 0)} semanticamente inválidos, "
                  f"{pilot.get('refused', 0)} recusas, "
                  f"{pilot.get('incomplete', 0)} incompletos.", flush=True)
        print(f"[Concluído] Avaliação: {'PASS' if config.evaluation_enabled else 'desabilitada'}.",
              flush=True)
        return 0
    finally:
        if creation_started:
            try:
                current = _active_kind_clusters(kind_bin, pinned["KIND_VERSION"])
            except (ConfigurationError, OSError, subprocess.CalledProcessError):
                current = []
            if cluster_name in current:
                print(f"\n[Limpeza] Removendo cluster experimental efêmero {cluster_name}...",
                      flush=True)
                subprocess.run([kind_bin, "delete", "cluster", "--name", cluster_name],
                               cwd=ROOT, env=env, check=False)
                print("[Limpeza] Cluster experimental removido.", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="validate and print configuration without side effects")
    parser.add_argument("--env-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        return run_experiment(args.env_file, args.dry_run)
    except ConfigurationError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2
    except DependencyPreflightError as exc:
        print(f"DEPENDENCY PREFLIGHT FAILED:\n{exc}", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"EXPERIMENT FAILED: setup or integrated runner exited {exc.returncode}", file=sys.stderr)
        return exc.returncode or 1
    except Exception as exc:
        # Unknown exception text is not trusted for terminal output: SDK or
        # transport exceptions must never echo credential material.
        print(f"EXPERIMENT FAILED: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
