"""Token and cost accounting for the frozen Responses API pilot."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from .protocol import ROOT

PRICING_PATH = ROOT / "docs/experiment/llm/pricing-v1.json"
MILLION = Decimal(1_000_000)


def load_pricing() -> dict[str, Any]:
    from .protocol import _load_protocol, ROOT as REPO_ROOT
    protocol = _load_protocol()
    configured = protocol.get("pricing_file", "docs/experiment/llm/pricing-v1.json")
    return json.loads((REPO_ROOT / configured).read_text(encoding="utf-8"))


def _integer_or_none(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def account_usage(usage: Any, returned_service_tier: str | None,
                  pricing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Extract usage and estimate USD cost; unknown billing inputs stay null."""
    pricing = pricing or load_pricing()
    if usage is None:
        return {
            "input_tokens": None, "cached_input_tokens": None,
            "cache_write_tokens": None, "output_tokens": None,
            "reasoning_tokens": None, "total_tokens": None,
            "estimated_cost_usd": None, "cost_status": "USAGE_UNAVAILABLE",
            "pricing_version": pricing["pricing_version"],
        }

    usage_dict = usage if isinstance(usage, dict) else _model_dict(usage)
    input_tokens = _integer_or_none(usage_dict.get("input_tokens"))
    output_tokens = _integer_or_none(usage_dict.get("output_tokens"))
    total_tokens = _integer_or_none(usage_dict.get("total_tokens"))
    input_details = usage_dict.get("input_tokens_details")
    output_details = usage_dict.get("output_tokens_details")
    input_details = input_details if isinstance(input_details, dict) else _model_dict(input_details)
    output_details = output_details if isinstance(output_details, dict) else _model_dict(output_details)
    cached = _integer_or_none(input_details.get("cached_tokens"))
    cache_write = _integer_or_none(input_details.get("cache_write_tokens"))
    reasoning = _integer_or_none(output_details.get("reasoning_tokens"))
    write_details = input_details.get("cache_write_tokens_details")
    write_details = write_details if isinstance(write_details, dict) else _model_dict(write_details)
    write_30m = _integer_or_none(write_details.get("ttl_30m_tokens"))
    write_12h = _integer_or_none(write_details.get("ttl_12h_tokens"))

    record: dict[str, Any] = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "cache_write_tokens": cache_write,
        "cache_write_30m_tokens": write_30m,
        "cache_write_12h_tokens": write_12h,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning,
        "total_tokens": total_tokens,
        "estimated_cost_usd": None,
        "cost_status": "COST_UNAVAILABLE",
        "pricing_version": pricing["pricing_version"],
    }
    if returned_service_tier not in (None, "default"):
        record["cost_status"] = "UNSUPPORTED_RETURNED_SERVICE_TIER"
        return record
    if None in (input_tokens, output_tokens, cached, cache_write):
        record["cost_status"] = "USAGE_BREAKDOWN_INCOMPLETE"
        return record
    if cached + cache_write > input_tokens:
        record["cost_status"] = "USAGE_BREAKDOWN_INCONSISTENT"
        return record
    if cache_write == 0:
        if write_30m is None:
            write_30m = 0
        if write_12h is None:
            write_12h = 0
    elif None in (write_30m, write_12h) or write_30m + write_12h != cache_write:
        record["cost_status"] = "CACHE_WRITE_BREAKDOWN_INCOMPLETE"
        return record

    rates = pricing["short_context_rates"]
    long_context = input_tokens > pricing["long_context"]["threshold_input_tokens_exclusive"]
    input_multiplier = Decimal(pricing["long_context"]["input_and_cache_rate_multiplier"]) if long_context else Decimal(1)
    output_multiplier = Decimal(pricing["long_context"]["output_rate_multiplier"]) if long_context else Decimal(1)
    ordinary_input = input_tokens - cached - cache_write
    input_cost = (
        Decimal(ordinary_input) * Decimal(rates["input"]) * input_multiplier
        + Decimal(cached) * Decimal(rates["cached_input"]) * input_multiplier
        + Decimal(write_30m) * Decimal(rates["cache_write_30m"]) * input_multiplier
        + Decimal(write_12h) * Decimal(rates["cache_write_12h"]) * input_multiplier
    ) / MILLION
    output_cost = Decimal(output_tokens) * Decimal(rates["output"]) * output_multiplier / MILLION
    record["estimated_cost_usd"] = format(input_cost + output_cost, "f")
    record["cost_status"] = "ESTIMATED"
    record["long_context_pricing_applied"] = long_context
    return record


def _model_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(exclude_none=False)
    if hasattr(value, "dict"):
        return value.dict(exclude_none=False)
    return {}
