#!/usr/bin/env python3
"""Deterministic host-side Online Boutique workload for experiment-v2."""

from __future__ import annotations

import argparse
import concurrent.futures
import http.cookiejar
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


PRODUCT_ID = "OLJCESPC7Z"
CHECKOUT_FORM = {
    "email": "shopper@example.com",
    "street_address": "1 Main Street",
    "zip_code": "94043",
    "city": "Mountain View",
    "state": "CA",
    "country": "United States",
    "credit_card_number": "4111111111111111",
    "credit_card_expiration_month": "12",
    "credit_card_expiration_year": "2030",
    "credit_card_cvv": "123",
}


class FlowFailure(RuntimeError):
    pass


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def request(
    opener: urllib.request.OpenerDirector,
    base_url: str,
    method: str,
    path: str,
    timeout: float,
    form: dict[str, str] | None = None,
    expected_status: int = 200,
) -> dict[str, Any]:
    data = urllib.parse.urlencode(form).encode("ascii") if form is not None else None
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/x-www-form-urlencoded"}
        if data is not None
        else {},
    )
    started = time.monotonic()
    try:
        with opener.open(req, timeout=timeout) as response:
            status = response.status
            response.read()
            final_url = response.geturl()
    except urllib.error.HTTPError as exc:
        status = exc.code
        final_url = exc.geturl()
        exc.read()
    elapsed = time.monotonic() - started
    result = {
        "method": method,
        "path": path,
        "status": status,
        "duration_seconds": round(elapsed, 6),
    }
    if status != expected_status:
        raise FlowFailure(
            f"{method} {path} returned HTTP {status}, expected {expected_status}"
        )
    if not final_url.startswith(base_url.rstrip("/") + "/"):
        raise FlowFailure(f"unexpected redirect destination for {method} {path}")
    return result


def run_flow(
    base_url: str,
    timeout: float,
    flow_number: int,
    expected_statuses: dict[tuple[str, str], int] | None = None,
) -> dict[str, Any]:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    expected_statuses = expected_statuses or {}

    def do(method: str, path: str, form: dict[str, str] | None = None) -> dict[str, Any]:
        return request(
            opener,
            base_url,
            method,
            path,
            timeout,
            form,
            expected_statuses.get((method, path), 200),
        )

    steps = [
        do("GET", "/"),
        do("GET", f"/product/{PRODUCT_ID}"),
        do("POST", "/cart", {"product_id": PRODUCT_ID, "quantity": "1"}),
        do("GET", "/cart"),
        do("POST", "/cart/checkout", CHECKOUT_FORM),
    ]
    return {"flow": flow_number, "status": "PASS", "steps": steps}


def parse_expected_statuses(values: list[str]) -> dict[tuple[str, str], int]:
    expected: dict[tuple[str, str], int] = {}
    for value in values:
        try:
            operation, code = value.rsplit("=", 1)
            method, path = operation.split(":", 1)
            status = int(code)
        except ValueError as exc:
            raise ValueError(
                f"invalid expected status {value!r}; use METHOD:/path=HTTP_CODE"
            ) from exc
        method = method.upper()
        if method not in {"GET", "POST"} or not path.startswith("/"):
            raise ValueError(f"invalid expected request in {value!r}")
        if status < 100 or status > 599:
            raise ValueError(f"invalid HTTP status in {value!r}")
        key = (method, path)
        if key in expected:
            raise ValueError(f"duplicate expected status for {method} {path}")
        expected[key] = status
    return expected


def validate_phase_counts(warmup_flows: int, steady_flows: int) -> None:
    if warmup_flows < 0 or steady_flows < 0 or warmup_flows + steady_flows < 1:
        raise ValueError("warmup and steady flows must be non-negative and at least one must be positive")


def main() -> int:
    default_config = (
        Path(__file__).resolve().parents[2]
        / "config/experiment/workload.json"
    )
    config_parser = argparse.ArgumentParser(add_help=False)
    config_parser.add_argument("--config", type=Path, default=default_config)
    config_args, _ = config_parser.parse_known_args()
    config = json.loads(config_args.config.read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__, parents=[config_parser])
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--warmup-flows", type=int, default=config["warmup_flows"])
    parser.add_argument("--steady-flows", type=int, default=config["steady_flows"])
    parser.add_argument(
        "--arrival-interval-seconds",
        type=float,
        default=config["arrival_interval_seconds"],
    )
    parser.add_argument(
        "--max-concurrent-flows", type=int, default=config["max_concurrent_flows"]
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=float,
        default=config["request_timeout_seconds"],
    )
    parser.add_argument(
        "--drain-timeout-seconds",
        type=float,
        default=config["drain_timeout_seconds"],
    )
    parser.add_argument(
        "--expect-status",
        action="append",
        default=[],
        metavar="METHOD:/path=HTTP_CODE",
        help="expected response for a request (default: HTTP 200)",
    )
    args = parser.parse_args()

    try:
        validate_phase_counts(args.warmup_flows, args.steady_flows)
    except ValueError as exc:
        parser.error(str(exc))
    if args.arrival_interval_seconds <= 0 or args.max_concurrent_flows < 1:
        parser.error("arrival interval and concurrency must be positive")
    try:
        expected_statuses = parse_expected_statuses(args.expect_status)
    except ValueError as exc:
        parser.error(str(exc))

    result: dict[str, Any] = {
        "base_url": args.base_url.rstrip("/"),
        "started_at": timestamp(),
        "configuration": {
            "warmup_flows": args.warmup_flows,
            "steady_flows": args.steady_flows,
            "arrival_interval_seconds": args.arrival_interval_seconds,
            "configured_arrival_rate_flows_per_second": round(
                1 / args.arrival_interval_seconds, 6
            ),
            "max_concurrent_flows": args.max_concurrent_flows,
            "request_timeout_seconds": args.request_timeout_seconds,
            "drain_timeout_seconds": args.drain_timeout_seconds,
            "expected_statuses": {
                f"{method} {path}": status
                for (method, path), status in sorted(expected_statuses.items())
            },
        },
        "warmup": [],
        "steady": [],
        "status": "FAIL",
    }
    try:
        for number in range(1, args.warmup_flows + 1):
            result["warmup"].append(
                run_flow(
                    args.base_url,
                    args.request_timeout_seconds,
                    number,
                    expected_statuses,
                )
            )

        result["warmup_finished_at"] = timestamp()
        result["steady_started_at"] = timestamp()

        futures: list[concurrent.futures.Future[dict[str, Any]]] = []
        schedule_start = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.max_concurrent_flows
        ) as executor:
            for index in range(args.steady_flows):
                due = schedule_start + index * args.arrival_interval_seconds
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                futures.append(
                    executor.submit(
                        run_flow,
                        args.base_url,
                        args.request_timeout_seconds,
                        index + 1,
                        expected_statuses,
                    )
                )
            done, pending = concurrent.futures.wait(
                futures, timeout=args.drain_timeout_seconds
            )
            if pending:
                for future in pending:
                    future.cancel()
                raise FlowFailure(
                    f"drain timed out with {len(pending)} steady flow(s) unfinished"
                )
            result["steady"] = [future.result() for future in futures]
        result["status"] = "PASS"
    except Exception as exc:  # Include transport and flow errors in machine-readable output.
        result["error"] = str(exc)
    result["finished_at"] = timestamp()
    result["successful_warmup_flows"] = sum(
        flow.get("status") == "PASS" for flow in result["warmup"]
    )
    result["successful_steady_flows"] = sum(
        flow.get("status") == "PASS" for flow in result["steady"]
    )
    json.dump(result, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
