"""Exact, no-order research analytics over qualified frozen source aggregates.

This module never contacts a broker, estimates missing fills, or turns an
engineering check into strategy promotion. PnL scenario inputs are explicit
research assumptions, not prices manufactured from signal counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any

from tools.research_capability_register import validate_register

CENT = Decimal("0.01")
PCT = Decimal("100")
BPS = Decimal("10000")
ALLOWED_STRESS_BPS = ("0", "5", "10", "25")
DENIED_AUTHORITY = (
    "external_order_routing_allowed", "demo_trading_allowed",
    "live_trading_allowed", "mainnet_entry_allowed",
    "production_release_allowed",
)
COUNT_KEYS = (
    "eligible_signals", "plan_eligible_signals", "independent_target_stop_episodes",
    "target_first", "stop_first", "neither", "portfolio_trades", "wins",
    "breakeven", "losses",
)


class ResearchAnalyticsError(ValueError):
    """Insufficient, contradictory or untrusted data for honest analysis."""


def exact_decimal(value: object, *, label: str) -> Decimal:
    if not isinstance(value, str) or len(value) > 48:
        raise ResearchAnalyticsError(f"{label}: exact numeric string required")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ResearchAnalyticsError(f"{label}: invalid decimal") from exc
    if not result.is_finite():
        raise ResearchAnalyticsError(f"{label}: non-finite number")
    return result


def _round(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_EVEN), ".2f")


def _percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        raise ResearchAnalyticsError("cannot compute percentage without denominator")
    return _round(Decimal(numerator) * PCT / Decimal(denominator))


@dataclass(frozen=True)
class CashScenarioFacts:
    """Explicit accounting aggregate in USDT; may be unverified research inputs.

    funding_debit_usdt is signed: positive means cost, negative credit.
    turnover_notional_usdt is total filled USD notional summed across fills,
    not opening exposure, leverage, equity or portfolio market value.
    """

    gross_pnl_usdt: str
    turnover_notional_usdt: str
    fees_paid_usdt: str
    slippage_paid_usdt: str
    funding_debit_usdt: str


def calculate_cash_scenarios(facts: CashScenarioFacts) -> dict[str, Any]:
    """Cost stress uses turnover * additional adverse bps / 10000.

    Fee/slippage/funding facts must come from an independently attested ledger
    before interpreting results as execution evidence. The calculation itself
    grants NO authority and cannot prove a trading edge.
    """
    if not isinstance(facts, CashScenarioFacts):
        raise ResearchAnalyticsError("typed scenario facts required")
    amounts = {
        name: exact_decimal(getattr(facts, name), label=name)
        for name in facts.__dataclass_fields__
    }
    if amounts["turnover_notional_usdt"] <= 0:
        raise ResearchAnalyticsError("positive executed turnover required")
    if amounts["fees_paid_usdt"] < 0 or amounts["slippage_paid_usdt"] < 0:
        raise ResearchAnalyticsError("fee and adverse slippage costs cannot be negative")
    # A funding credit is possible; only fee and slippage amounts are unsigned.
    gross = amounts["gross_pnl_usdt"]
    observed_net = (
        gross - amounts["fees_paid_usdt"] - amounts["slippage_paid_usdt"]
        - amounts["funding_debit_usdt"]
    )
    rows = []
    for raw_bps in ALLOWED_STRESS_BPS:
        additional_cost = amounts["turnover_notional_usdt"] * Decimal(raw_bps) / BPS
        rows.append({
            "additional_adverse_bps": raw_bps,
            "additional_cost_usdt": _round(additional_cost),
            "modeled_net_usdt": _round(observed_net - additional_cost),
        })
    return {
        "schema_version": "astra-research-cash-stress-v1",
        "method": "GROSS_MINUS_FEES_MINUS_SLIPPAGE_MINUS_SIGNED_FUNDING_MINUS_TURNOVER_STRESS",
        "base_modeled_net_usdt": _round(observed_net),
        "rows": rows,
        "observed_execution_proven": False,
        "profitability_proven": False,
        "trading_authority_allowed": False,
    }


def calculate_research_overview(
    status: dict, register: dict, *, evidence_root,
) -> dict[str, Any]:
    """Reconcile two trusted repository source contracts into one UI snapshot."""
    if not isinstance(status, dict) or status.get("schema_version") != "current-system-status-v3":
        raise ResearchAnalyticsError("unsupported authoritative status schema")
    trading = status.get("trading_authority")
    strategy = status.get("strategy")
    repository = status.get("repository")
    release = status.get("release")
    if (
        not isinstance(trading, dict)
        or not isinstance(strategy, dict)
        or not isinstance(repository, dict)
        or not isinstance(release, dict)
        or any(trading.get(key) is not False for key in DENIED_AUTHORITY)
        or strategy.get("status") != "PROFITABILITY_NOT_PROVEN"
        or strategy.get("promotion_allowed") is not False
        or release.get("status") != "SOURCE_AND_CI_QUALIFICATION_ONLY"
    ):
        raise ResearchAnalyticsError("research-only authority boundary changed")
    replay = strategy.get("latest_frozen_bybit_price_only_replay")
    if not isinstance(replay, dict):
        raise ResearchAnalyticsError("frozen replay source absent")
    counts = {}
    for key in COUNT_KEYS:
        value = replay.get(key)
        if type(value) is not int or value < 0:
            raise ResearchAnalyticsError(f"invalid historical replay count: {key}")
        counts[key] = value
    if (
        not counts["eligible_signals"]
        or not counts["independent_target_stop_episodes"]
        or not counts["portfolio_trades"]
        or counts["plan_eligible_signals"] > counts["eligible_signals"]
        or counts["target_first"] + counts["stop_first"] + counts["neither"]
        != counts["independent_target_stop_episodes"]
        or counts["wins"] + counts["breakeven"] + counts["losses"]
        != counts["portfolio_trades"]
    ):
        raise ResearchAnalyticsError("contradictory historical replay counts")
    principal = exact_decimal(replay.get("reference_equity_usdt"), label="reference equity")
    net = exact_decimal(replay.get("net_pnl_usdt"), label="net PnL")
    approx = exact_decimal(
        replay.get("net_return_fraction_approx"), label="approximate net return",
    )
    if principal <= 0:
        raise ResearchAnalyticsError("reference equity must be positive")
    actual_fraction = net / principal
    if abs(actual_fraction - approx) > Decimal("0.00005"):
        raise ResearchAnalyticsError("frozen net return and PnL disagree")

    registry_summary = validate_register(register, evidence_root)
    sha = repository.get("latest_engineering_qualified_main_sha")
    if (
        not isinstance(sha, str)
        or len(sha) != 40
        or any(letter not in "0123456789abcdef" for letter in sha)
    ):
        raise ResearchAnalyticsError("qualified main identity missing")
    rows = []
    for entry in register["capabilities"]:
        rows.append({
            "id": entry["id"],
            "priority": entry["priority"],
            "state": entry["state"],
            "depends_on": tuple(entry["depends_on"]),
            "acceptance_gate": entry["acceptance_gate"],
        })
    return {
        "schema_version": "astra-research-operations-overview-v1",
        "source_status": "REUSED_FROZEN_HISTORY_NOT_LIVE_PROVIDER_TRUTH",
        "authority": "HOLD",
        "main_qualified_sha": sha,
        "strategy_status": strategy["status"],
        "evidence_source": strategy.get("evidence_source"),
        "reference_equity_usdt": _round(principal),
        "net_pnl_usdt": _round(net),
        "net_return_pct": _round(actual_fraction * PCT),
        "net_pnl_per_trade_usdt": _round(net / counts["portfolio_trades"]),
        "win_rate_pct": _percent(counts["wins"], counts["portfolio_trades"]),
        "loss_rate_pct": _percent(counts["losses"], counts["portfolio_trades"]),
        "neutral_rate_pct": _percent(counts["breakeven"], counts["portfolio_trades"]),
        "plan_eligibility_pct": _percent(
            counts["plan_eligible_signals"], counts["eligible_signals"],
        ),
        "target_first_pct": _percent(
            counts["target_first"], counts["independent_target_stop_episodes"],
        ),
        "stop_first_pct": _percent(
            counts["stop_first"], counts["independent_target_stop_episodes"],
        ),
        "counts": counts,
        "capability_summary": registry_summary,
        "capability_rows": rows,
        # These require trade-level and actual provider evidence; never infer.
        "max_drawdown_pct": None,
        "sharpe_ratio": None,
        "actual_exchange_funding_usdt": None,
        "fill_level_cost_stress": None,
        "source_mark_price_status": "BLOCKED_SOURCE",
        "live_signal_observations": None,
        "trading_authority_allowed": False,
        "profitability_proven": False,
    }
