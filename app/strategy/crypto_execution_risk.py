from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from app.strategy.crypto_entry_economics import revalidate_entry_at_actual_taker_fee
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan

_BPS = Decimal("10000")


@dataclass(frozen=True)
class CryptoExecutionRiskPolicy:
    """Fail-closed quantity resize at the first executable next-bar price."""

    maximum_risk_budget_multiple: Decimal = Decimal("1.00")

    def validate(self) -> None:
        if (
            not self.maximum_risk_budget_multiple.is_finite()
            or self.maximum_risk_budget_multiple <= 0
            or self.maximum_risk_budget_multiple > 1
        ):
            raise ValueError("execution risk budget multiple must be finite and within (0, 1]")


@dataclass(frozen=True)
class CryptoExecutionRiskDecision:
    eligible: bool
    reasons: tuple[str, ...]
    adjusted_plan: CryptoTradePlan | None
    actual_entry_price: Decimal
    original_quantity: Decimal
    adjusted_quantity: Decimal
    resized: bool
    modeled_stop_loss_after_cost_usdt: Decimal
    modeled_expected_net_edge_usd: Decimal
    shadow_only: bool = True
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False


def resize_trade_plan_at_next_open(
    plan: CryptoTradePlan,
    *,
    raw_next_open_price: Decimal,
    strategy_config: CryptoPerpStrategyConfig,
    policy: CryptoExecutionRiskPolicy | None = None,
) -> CryptoExecutionRiskDecision:
    """Downsize a pending plan using only the next bar open, then recheck target economics.

    The function never increases quantity. It uses the same fee/slippage risk model as the
    pre-entry planner, but with the first actually executable next-bar price. A gap can therefore
    reduce size or cancel the trade if the minimum net-profit edge no longer survives resizing.
    """

    active = CryptoExecutionRiskPolicy() if policy is None else policy
    active.validate()
    strategy_config.validate()
    if not raw_next_open_price.is_finite() or raw_next_open_price <= 0:
        raise ValueError("next-open execution price must be positive and finite")
    if plan.reference_quantity <= 0 or plan.risk_budget_usdt <= 0:
        raise ValueError("execution risk plan quantity and risk budget must be positive")

    entry_price = _entry_execution_price(
        raw_next_open_price,
        side=plan.side,
        config=strategy_config,
    )
    per_fill_cost_fraction = (
        strategy_config.taker_fee_rate
        + strategy_config.slippage_bps_per_fill / _BPS
    )
    stop_loss_after_cost_fraction = (
        plan.stop_fraction + per_fill_cost_fraction * Decimal("2")
    )
    if stop_loss_after_cost_fraction <= 0:
        raise ValueError("execution risk stop-loss fraction must be positive")

    original_notional = entry_price * plan.reference_quantity
    risk_limit = plan.risk_budget_usdt * active.maximum_risk_budget_multiple
    maximum_risk_sized_notional = risk_limit / stop_loss_after_cost_fraction
    adjusted_notional = min(original_notional, maximum_risk_sized_notional)
    adjusted_quantity = adjusted_notional / entry_price
    resized = adjusted_quantity < plan.reference_quantity

    economics = revalidate_entry_at_actual_taker_fee(
        plan,
        execution_notional_usdt=adjusted_notional,
        actual_taker_fee_rate=strategy_config.taker_fee_rate,
        strategy_config=strategy_config,
    )
    reasons = tuple(
        _execution_reason(reason) for reason in economics.reasons
    )
    if economics.modeled_stop_loss_after_cost_usdt > risk_limit:
        reasons = (*reasons, "NEXT_OPEN_RISK_BUDGET_EXCEEDED")
    reasons = tuple(dict.fromkeys(reasons))
    if reasons:
        return CryptoExecutionRiskDecision(
            eligible=False,
            reasons=reasons,
            adjusted_plan=None,
            actual_entry_price=entry_price,
            original_quantity=plan.reference_quantity,
            adjusted_quantity=adjusted_quantity,
            resized=resized,
            modeled_stop_loss_after_cost_usdt=(
                economics.modeled_stop_loss_after_cost_usdt
            ),
            modeled_expected_net_edge_usd=economics.modeled_expected_net_edge_usd,
        )

    adjusted_plan = replace(
        plan,
        notional_usdt=adjusted_notional,
        reference_quantity=adjusted_quantity,
        estimated_round_trip_cost_usdt=economics.modeled_round_trip_cost_usdt,
        estimated_stop_loss_after_cost_usdt=(
            economics.modeled_stop_loss_after_cost_usdt
        ),
        required_move_fraction=economics.required_move_fraction,
        expected_net_edge_usd=economics.modeled_expected_net_edge_usd,
    )
    return CryptoExecutionRiskDecision(
        eligible=True,
        reasons=(),
        adjusted_plan=adjusted_plan,
        actual_entry_price=entry_price,
        original_quantity=plan.reference_quantity,
        adjusted_quantity=adjusted_quantity,
        resized=resized,
        modeled_stop_loss_after_cost_usdt=(
            economics.modeled_stop_loss_after_cost_usdt
        ),
        modeled_expected_net_edge_usd=economics.modeled_expected_net_edge_usd,
    )


def _entry_execution_price(
    raw_price: Decimal,
    *,
    side: CryptoSide,
    config: CryptoPerpStrategyConfig,
) -> Decimal:
    slippage = config.slippage_bps_per_fill / _BPS
    if side is CryptoSide.LONG:
        return raw_price * (Decimal("1") + slippage)
    return raw_price * (Decimal("1") - slippage)


def _execution_reason(reason: str) -> str:
    mapping = {
        "ACCOUNT_FEE_TARGET_NET_EDGE_UNAVAILABLE": "NEXT_OPEN_TARGET_NET_EDGE_UNAVAILABLE",
        "ACCOUNT_FEE_EXPECTED_NET_PROFIT_BELOW_TARGET": (
            "NEXT_OPEN_EXPECTED_NET_PROFIT_BELOW_TARGET"
        ),
        "ACCOUNT_FEE_RISK_BUDGET_EXCEEDED": "NEXT_OPEN_RISK_BUDGET_EXCEEDED",
    }
    return mapping.get(reason, f"NEXT_OPEN_{reason}")