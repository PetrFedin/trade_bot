from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.strategy.crypto_perp import CryptoPerpStrategyConfig

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS = Decimal("10000")


@dataclass(frozen=True)
class CryptoTradePlanFeasibilityPoint:
    equity_usdt: Decimal
    minimum_required_atr_fraction: Decimal | None
    strategy_valid_atr_available: bool
    atr_gate_multiple_vs_strategy_minimum: Decimal | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "equity_usdt": float(self.equity_usdt),
            "minimum_required_atr_fraction": (
                None
                if self.minimum_required_atr_fraction is None
                else float(self.minimum_required_atr_fraction)
            ),
            "strategy_valid_atr_available": self.strategy_valid_atr_available,
            "atr_gate_multiple_vs_strategy_minimum": (
                None
                if self.atr_gate_multiple_vs_strategy_minimum is None
                else float(self.atr_gate_multiple_vs_strategy_minimum)
            ),
        }


def minimum_atr_fraction_for_trade_plan(
    *,
    equity_usdt: Decimal,
    config: CryptoPerpStrategyConfig,
) -> Decimal | None:
    """Return the exact ATR fraction required by the frozen trade-plan algebra.

    This is a diagnostic inversion of ``build_trade_plan``. It does not inspect historical
    outcomes and does not change the production eligibility rule.
    """

    config.validate()
    if not equity_usdt.is_finite() or equity_usdt <= 0:
        raise ValueError("trade-plan feasibility equity must be positive and finite")

    cost = _round_trip_cost_fraction(config)
    risk_fraction = config.risk_fraction_per_trade
    stop_multiple = config.hard_stop_atr_multiple
    move_multiple = config.expected_move_atr_multiple
    notional_cap = config.maximum_notional_to_equity
    target = config.target_net_profit_usd

    cap_boundary = (risk_fraction / notional_cap - cost) / stop_multiple
    cap_edge_threshold = (
        target / (notional_cap * equity_usdt) + cost
    ) / move_multiple
    candidates: list[Decimal] = []
    if cap_boundary >= _ZERO and cap_edge_threshold <= cap_boundary:
        candidates.append(max(_ZERO, cap_edge_threshold))

    required_edge_to_risk = target / (risk_fraction * equity_usdt)
    denominator = move_multiple - required_edge_to_risk * stop_multiple
    if denominator > _ZERO:
        risk_sized_threshold = (
            cost * (_ONE + required_edge_to_risk) / denominator
        )
        candidates.append(max(_ZERO, cap_boundary, risk_sized_threshold))

    if not candidates:
        return None
    return min(candidates)


def minimum_equity_for_any_strategy_valid_trade_plan(
    config: CryptoPerpStrategyConfig,
) -> Decimal:
    """Return minimum equity at which max allowed ATR can still satisfy the fixed target."""

    config.validate()
    cost = _round_trip_cost_fraction(config)
    atr = config.maximum_atr_fraction
    stop_fraction = atr * config.hard_stop_atr_multiple
    notional_per_equity = min(
        config.risk_fraction_per_trade / (stop_fraction + cost),
        config.maximum_notional_to_equity,
    )
    expected_edge_per_equity = notional_per_equity * (
        atr * config.expected_move_atr_multiple - cost
    )
    if expected_edge_per_equity <= _ZERO:
        raise ValueError("trade-plan feasibility has no positive edge at maximum ATR")
    return config.target_net_profit_usd / expected_edge_per_equity


def diagnose_crypto_trade_plan_feasibility(
    equities_usdt: tuple[Decimal, ...],
    *,
    config: CryptoPerpStrategyConfig | None = None,
) -> dict[str, Any]:
    active = CryptoPerpStrategyConfig() if config is None else config
    active.validate()
    if not equities_usdt:
        raise ValueError("trade-plan feasibility requires at least one equity point")
    if len(set(equities_usdt)) != len(equities_usdt):
        raise ValueError("trade-plan feasibility equity points must be unique")

    points: list[CryptoTradePlanFeasibilityPoint] = []
    for equity in equities_usdt:
        threshold = minimum_atr_fraction_for_trade_plan(
            equity_usdt=equity,
            config=active,
        )
        strategy_threshold = (
            None
            if threshold is None
            else max(active.minimum_atr_fraction, threshold)
        )
        valid = (
            strategy_threshold is not None
            and strategy_threshold <= active.maximum_atr_fraction
        )
        points.append(
            CryptoTradePlanFeasibilityPoint(
                equity_usdt=equity,
                minimum_required_atr_fraction=strategy_threshold,
                strategy_valid_atr_available=valid,
                atr_gate_multiple_vs_strategy_minimum=(
                    None
                    if strategy_threshold is None
                    else strategy_threshold / active.minimum_atr_fraction
                ),
            )
        )

    minimum_equity = minimum_equity_for_any_strategy_valid_trade_plan(active)
    return {
        "diagnostic": "CRYPTO_FIXED_TARGET_TRADE_PLAN_FEASIBILITY_V1",
        "strategy": {
            "target_net_profit_usd": float(active.target_net_profit_usd),
            "risk_fraction_per_trade": float(active.risk_fraction_per_trade),
            "maximum_notional_to_equity": float(active.maximum_notional_to_equity),
            "hard_stop_atr_multiple": float(active.hard_stop_atr_multiple),
            "expected_move_atr_multiple": float(active.expected_move_atr_multiple),
            "minimum_atr_fraction": float(active.minimum_atr_fraction),
            "maximum_atr_fraction": float(active.maximum_atr_fraction),
            "round_trip_cost_fraction": float(_round_trip_cost_fraction(active)),
        },
        "minimum_equity_usdt_for_any_strategy_valid_trade_plan": float(minimum_equity),
        "points": [point.to_payload() for point in points],
        "diagnostic_contract": (
            "exact inversion of the existing fixed-target trade-plan feasibility algebra; "
            "no historical outcome is used to derive the threshold"
        ),
        "fixed_target_feedback_risk_observed": True,
        "parameter_retuning_performed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "trade_actionable": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def _round_trip_cost_fraction(config: CryptoPerpStrategyConfig) -> Decimal:
    per_fill = config.taker_fee_rate + config.slippage_bps_per_fill / _BPS
    return per_fill * Decimal("2")


__all__ = [
    "CryptoTradePlanFeasibilityPoint",
    "diagnose_crypto_trade_plan_feasibility",
    "minimum_atr_fraction_for_trade_plan",
    "minimum_equity_for_any_strategy_valid_trade_plan",
]
