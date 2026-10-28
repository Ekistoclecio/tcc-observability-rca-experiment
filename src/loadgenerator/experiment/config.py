"""Configuration and validation for one open-loop workload run."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re


KNOWN_PRODUCT_IDS = {
    "0PUK6V6EV0", "1YMWWN1N4O", "2ZYFJ3GM2N", "66VCHSJNUP",
    "6E92ZMYYFZ", "9SIQT8TOJO", "L9ECAV7KIM", "LS4PSXUNUM",
    "OLJCESPC7Z",
}


def _int(env: dict[str, str], key: str, default: int) -> int:
    try:
        return int(env.get(key, str(default)))
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer") from exc


def _float(env: dict[str, str], key: str, default: float) -> float:
    try:
        return float(env.get(key, str(default)))
    except ValueError as exc:
        raise ValueError(f"{key} must be a number") from exc


@dataclass(frozen=True)
class WorkloadConfig:
    run_id: str
    frontend_url: str
    target_flow_rate: float
    max_in_flight_flows: int
    duration_seconds: float
    warmup_seconds: float
    cooldown_seconds: float
    seed: int
    product_id: str
    request_timeout_seconds: float
    drain_timeout_seconds: float
    artifact_dir: Path
    checkout: dict[str, str]

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "WorkloadConfig":
        env = os.environ if env is None else env
        for legacy in ("TCC_USERS", "TCC_SPAWN_RATE", "TCC_WAIT_SECONDS"):
            if env.get(legacy):
                raise ValueError(f"{legacy} is not used by the open-loop runner; remove it from the environment")
        now = datetime.now(timezone.utc)
        run_id = env.get("TCC_RUN_ID", f"workload-{now:%Y%m%dT%H%M%SZ}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", run_id):
            raise ValueError("TCC_RUN_ID must be 1-100 safe filename characters")

        product_id = env.get("TCC_PRODUCT_ID", "OLJCESPC7Z")
        if product_id not in KNOWN_PRODUCT_IDS:
            raise ValueError(f"TCC_PRODUCT_ID is not in the v0.10.7 catalog: {product_id}")

        config = cls(
            run_id=run_id,
            frontend_url=env.get("TCC_FRONTEND_URL", "http://127.0.0.1:28080").rstrip("/"),
            target_flow_rate=_float(env, "TCC_TARGET_FLOW_RATE", 0.5),
            max_in_flight_flows=_int(env, "TCC_MAX_IN_FLIGHT_FLOWS", 4),
            duration_seconds=_float(env, "TCC_DURATION", 10.0),
            warmup_seconds=_float(env, "TCC_WARMUP_DURATION", 2.0),
            cooldown_seconds=_float(env, "TCC_COOLDOWN_DURATION", 2.0),
            seed=_int(env, "TCC_WORKLOAD_SEED", 42),
            product_id=product_id,
            request_timeout_seconds=_float(env, "TCC_REQUEST_TIMEOUT_SECONDS", 10.0),
            drain_timeout_seconds=_float(env, "TCC_DRAIN_TIMEOUT_SECONDS", 60.0),
            artifact_dir=Path(env.get("TCC_ARTIFACT_DIR", "artifacts/workload/runs")),
            checkout={
                "email": env.get("TCC_CHECKOUT_EMAIL", "tcc@example.com"),
                "street_address": env.get("TCC_CHECKOUT_STREET_ADDRESS", "1600 Amphitheatre Parkway"),
                "zip_code": env.get("TCC_CHECKOUT_ZIP_CODE", "94043"),
                "city": env.get("TCC_CHECKOUT_CITY", "Mountain View"),
                "state": env.get("TCC_CHECKOUT_STATE", "CA"),
                "country": env.get("TCC_CHECKOUT_COUNTRY", "United States"),
                "credit_card_number": env.get("TCC_CHECKOUT_CARD_NUMBER", "4432801561520454"),
                "credit_card_expiration_month": env.get("TCC_CHECKOUT_CARD_MONTH", "1"),
                "credit_card_expiration_year": env.get("TCC_CHECKOUT_CARD_YEAR", "2030"),
                "credit_card_cvv": env.get("TCC_CHECKOUT_CARD_CVV", "672"),
            },
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.target_flow_rate <= 0:
            raise ValueError("TCC_TARGET_FLOW_RATE must be greater than 0 flows per second")
        if self.max_in_flight_flows < 1:
            raise ValueError("TCC_MAX_IN_FLIGHT_FLOWS must be at least 1")
        if self.duration_seconds <= 0:
            raise ValueError("TCC_DURATION must be greater than 0")
        for key, value in (
            ("TCC_WARMUP_DURATION", self.warmup_seconds),
            ("TCC_COOLDOWN_DURATION", self.cooldown_seconds),
        ):
            if value < 0:
                raise ValueError(f"{key} cannot be negative")
        if self.request_timeout_seconds <= 0 or self.drain_timeout_seconds <= 0:
            raise ValueError("request and drain timeouts must be greater than 0")
        if not self.frontend_url.startswith(("http://", "https://")):
            raise ValueError("TCC_FRONTEND_URL must be an http(s) URL")
        if not self.checkout["email"] or "@" not in self.checkout["email"]:
            raise ValueError("TCC_CHECKOUT_EMAIL must be a valid test email")
        if not self.checkout["credit_card_number"].isdigit():
            raise ValueError("TCC_CHECKOUT_CARD_NUMBER must contain digits only")
        for key in ("credit_card_expiration_month", "credit_card_expiration_year", "credit_card_cvv", "zip_code"):
            if not self.checkout[key].isdigit():
                raise ValueError(f"checkout field {key} must contain digits only")
