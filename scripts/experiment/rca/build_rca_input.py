from __future__ import annotations

import argparse
from pathlib import Path
import sys

from .builder import RCAInputError, generate_run
from ..dataset.core import DatasetError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build model-agnostic canonical RCA request files from Phase 5 datasets")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--run-id")
    selection.add_argument("--all-datasets", action="store_true", help="build both requests for every dataset run directory")
    parser.add_argument("--scenario", choices=("a", "b"), help="build one scenario; default builds A and B")
    parser.add_argument("--replace", action="store_true", help="replace existing output snapshot (development only)")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[3])
    args = parser.parse_args(argv)
    if args.all_datasets and args.scenario:
        parser.error("--scenario cannot be combined with --all-datasets")
    scenarios = (args.scenario.upper(),) if args.scenario else ("A", "B")
    try:
        repo_root = args.repo_root.resolve()
        if args.all_datasets:
            dataset_root = repo_root / "artifacts" / "datasets"
            run_ids = sorted(path.name for path in dataset_root.iterdir()
                             if path.is_dir() and all((path / f"scenario-{suffix}" / "dataset.json").is_file() for suffix in ("a", "b")))
            if not run_ids:
                raise RCAInputError(f"no complete A/B datasets found under {dataset_root}")
            results_by_run = {run_id: generate_run(repo_root, run_id, ("A", "B"), replace=args.replace) for run_id in run_ids}
        else:
            results_by_run = {args.run_id: generate_run(repo_root, args.run_id, scenarios, replace=args.replace)}
    except (RCAInputError, DatasetError, OSError, ValueError) as exc:
        print(f"RCA input builder: {exc}", file=sys.stderr)
        return 2
    for run_id, results in results_by_run.items():
        for scenario, manifest in results.items():
            print(f"{run_id} / Scenario {scenario}: {repo_root / 'artifacts/rca-inputs' / run_id / ('scenario-a' if scenario == 'A' else 'scenario-b')}")
            print(f"  request_sha256={manifest['request_sha256']} bytes={manifest['request_bytes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
