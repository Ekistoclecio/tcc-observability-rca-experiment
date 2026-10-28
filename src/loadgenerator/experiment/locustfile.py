"""Deterministic five-request business flow used by the host runner."""

from __future__ import annotations

from time import perf_counter

import requests


FLOW_STAGES = ("home", "product", "add_cart", "cart", "checkout")
EXPECTED_STATUS = {"home": 200, "product": 200, "add_cart": 302, "cart": 200, "checkout": 200}


class FlowFailure(Exception):
    def __init__(self, stage: str, http_status: int | None, message: str):
        super().__init__(message)
        self.stage = stage
        self.http_status = http_status


def new_flow_session():
    """Each invocation creates an independent cookie jar and connection pool."""
    return requests.Session()


def execute_checkout_flow(config, session_factory=new_flow_session, record_request=None) -> None:
    """Run one flow using its own normal HTTP session, without control headers."""
    session = session_factory()
    steps = (
        ("home", "GET", "/", None),
        ("product", "GET", f"/product/{config.product_id}", None),
        ("add_cart", "POST", "/cart", {
            "product_id": config.product_id,
            "quantity": "1",
        }),
        ("cart", "GET", "/cart", None),
        ("checkout", "POST", "/cart/checkout", config.checkout),
    )
    try:
        for stage, method, path, data in steps:
            started = perf_counter()
            try:
                response = session.request(
                    method,
                    f"{config.frontend_url}{path}",
                    data=data,
                    timeout=config.request_timeout_seconds,
                    allow_redirects=False,
                )
            except Exception as exc:
                if record_request:
                    record_request(stage, (perf_counter() - started) * 1000, False, None)
                raise FlowFailure(stage, None, f"{type(exc).__name__}: {exc}") from exc
            elapsed_ms = (perf_counter() - started) * 1000
            valid = response.status_code == EXPECTED_STATUS[stage]
            if record_request:
                record_request(stage, elapsed_ms, valid, response.status_code)
            response.close()
            if not valid:
                raise FlowFailure(
                    stage,
                    response.status_code,
                    f"expected HTTP {EXPECTED_STATUS[stage]}, got {response.status_code}",
                )
    finally:
        session.close()
