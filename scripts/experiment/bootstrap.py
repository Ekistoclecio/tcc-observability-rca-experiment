"""Stdlib-only bootstrap so missing pinned Python packages are reported clearly."""

from __future__ import annotations

import runpy
import sys

from scripts.experiment.prerequisites import (
    DependencyPreflightError,
    check_python_dependencies,
)


def main() -> None:
    try:
        check_python_dependencies()
    except DependencyPreflightError as exc:
        print(f"DEPENDENCY PREFLIGHT FAILED:\n{exc}", file=sys.stderr)
        raise SystemExit(2) from None
    runpy.run_module("scripts.experiment.cli", run_name="__main__")


if __name__ == "__main__":
    main()
