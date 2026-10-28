from __future__ import annotations

import argparse
import os
import sys

from .builder import build_run, collect_run, normalize_run
from .core import DatasetError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect and normalize same-window telemetry snapshots for scenarios A/B")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("collect", "normalize", "build"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--run-id", required=True)
        if name != "normalize":
            sub.add_argument("--pre-buffer", type=float,
                             default=float(os.environ.get("TCC_DATASET_PRE_BUFFER_SECONDS", "10")))
            sub.add_argument("--post-buffer", type=float,
                             default=float(os.environ.get("TCC_DATASET_POST_BUFFER_SECONDS", "10")))
            sub.add_argument("--kubectl", default=os.environ.get("TCC_KUBECTL", "kubectl"))
            sub.add_argument("--context", default=os.environ.get("TCC_KUBE_CONTEXT"))
            sub.add_argument("--loki-limit", type=int, default=100000)
            sub.add_argument("--jaeger-limit", type=int, default=10000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "collect":
            output = collect_run(args.run_id, args.pre_buffer, args.post_buffer, args.kubectl,
                                 args.context, args.loki_limit, args.jaeger_limit)
        elif args.command == "normalize":
            output = normalize_run(args.run_id)
        else:
            output = build_run(args.run_id, args.pre_buffer, args.post_buffer, args.kubectl,
                               args.context, args.loki_limit, args.jaeger_limit)
        print(output)
        return 0
    except DatasetError as exc:
        print(f"dataset builder: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
