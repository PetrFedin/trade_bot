from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.strategy.crypto_entry_economics import (
    CryptoEntryEconomicsDecision,
    CryptoEntryEconomicsPolicy,
    evaluate_entry_economics,
)
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSignal,
    CryptoTradePlan,
    build_trade_plan,
)


@dataclass(frozen=True)
class CryptoTargetSelectionPolicy:
    """Cost-aware entry objective and realized-profit tolerance for crypto trades.

    A new position must still support at least ``minimum_entry_net_profit_usd`` after modeled
    fees and slippage. The normal fixed-exit objective is centered on $20, but realized PnL is
    allowed to land within a small tolerance band because exchange ticks, fees and slippage make
    an exact dollar result artificial.

    ``fallback_protected_net_profit_usd`` is never an entry target. It is only a defensive
    protection objective after a profitable position has already advanced. It is not guaranteed
    realized PnL because gaps, latency and adverse slippage can cross any stop.
    """

    minimum_entry_net_profit_usd: Decimal = Decimal("20")
    normal_exit_target_net_profit_usd: Decimal = Decimal("20")
    normal_exit_tolerance_usd: Decimal = Decimal("2")
    fallback_protected_net_profit_usd: Decimal = Decimal("15")
    open_ended_profit_runner: bool = True
    entry_economics_policy: CryptoEntryEconomicsPolicy | None = None

    @property
    def normal_exit_band_low_usd(self) -> Decimal:
        return self.normal_exit_target_net_profit_usd - self.normal_exit_tolerance_usd

    @property
    def normal_exit_band_high_usd(self) -> Decimal:
        return self.normal_exit_target_net_profit_usd + self.normal_exit_tolerance_usd

    def validate(self) -> None:
        if self.minimum_entry_net_profit_usd <= 0:
            raise ValueError("crypto minimum entry net profit must be positive")
        if self.normal_exit_target_net_profit_usd < self.minimum_entry_net_profit_usd:
            raise ValueError("normal crypto exit target cannot be below entry threshold")
        if not (
            Decimal("0")
            < self.normal_exit_tolerance_usd
            < self.normal_exit_target_net_profit_usd
        ):
            raise ValueError("normal crypto exit tolerance must be positive and below target")
        if self.fallback_protected_net_profit_usd <= 0:
            raise ValueError("crypto protected fallback profit must be positive")
        if self.fallback_protected_net_profit_usd >= self.normal_exit_band_low_usd:
            raise ValueError("crypto protected fallback must stay below normal exit band")
        if not self.open_ended_profit_runner:
            raise ValueError("crypto primary policy requires an open-ended profit runner")
        if self.entry_economics_policy is not None:
            self.entry_economics_policy.validate()


@dataclass(frozen=True)
class CryptoTargetAttempt:
    minimum_net_profit_usd: Decimal
    base_plan_eligible: bool
    base_plan_reasons: tuple[str, ...]
    entry_economics: CryptoEntryEconomicsDecision | None


@dataclass(frozen=True)
class CryptoTargetSelection:
    eligible: bool
    selected_target_net_profit_usd: Decimal | None
    selected_plan: CryptoTradePlan | None
    attempts: tuple[CryptoTargetAttempt, ...]
    reasons: tuple[str, ...]
    shadow_economics_enabled: bool
    open_ended_profit_runner: bool
    profit_cap_net_profit_usd: Decimal | None
    fallback_protected_net_profit_usd: Decimal
    normal_exit_band_low_usd: Decimal
    normal_exit_band_high_usd: Decimal
    strategy_promotion_allowed: bool = False
    live_activation_allowed: bool = False


def select_highest_feasible_crypto_target(
    signal: CryptoSignal,
    *,
    equity_usdt: Decimal,
    config: CryptoPerpStrategyConfig,
    policy: CryptoTargetSelectionPolicy | None = None,
) -> CryptoTargetSelection:
    """Require >=$20 modeled net edge and expose an $18-$22 realized tolerance band.

    The legacy name is retained for call-site compatibility. The selector performs one admission
    test at the minimum acceptable $20 modeled net edge. A successful admission may later use a
    fixed $20-centered exit or an excess-edge-gated open-ended runner. A $15 objective is never
    used to justify a new position.
    """

    active_policy = CryptoTargetSelectionPolicy() if policy is None else policy
    active_policy.validate()
    config.validate()

    minimum_config = config.with_target(active_policy.minimum_entry_net_profit_usd)
    plan_evaluation = build_trade_plan(
        signal,
        equity_usdt=equity_usdt,
        config=minimum_config,
    )
    economics: CryptoEntryEconomicsDecision | None = None
    if plan_evaluation.plan is not None and active_policy.entry_economics_policy is not None:
        economics = evaluate_entry_economics(
            plan_evaluation.plan,
            active_policy.entry_economics_policy,
        )

    attempt = CryptoTargetAttempt(
        minimum_net_profit_usd=active_policy.minimum_entry_net_profit_usd,
        base_plan_eligible=plan_evaluation.eligible,
        base_plan_reasons=plan_evaluation.reasons,
        entry_economics=economics,
    )

    reasons: list[str] = []
    if not plan_evaluation.eligible or plan_evaluation.plan is None:
        reasons.extend(plan_evaluation.reasons)
    if economics is not None and not economics.eligible:
        reasons.extend(economics.reasons)

    common = {
        "shadow_economics_enabled": active_policy.entry_economics_policy is not None,
        "open_ended_profit_runner": active_policy.open_ended_profit_runner,
        "profit_cap_net_profit_usd": None,
        "fallback_protected_net_profit_usd": active_policy.fallback_protected_net_profit_usd,
        "normal_exit_band_low_usd": active_policy.normal_exit_band_low_usd,
        "normal_exit_band_high_usd": active_policy.normal_exit_band_high_usd,
    }

    if reasons:
        return CryptoTargetSelection(
            eligible=False,
            selected_target_net_profit_usd=None,
            selected_plan=None,
            attempts=(attempt,),
            reasons=tuple(dict.fromkeys(reasons)) or ("MINIMUM_20_USD_NET_EDGE_UNAVAILABLE",),
            **common,
        )

    return CryptoTargetSelection(
        eligible=True,
        selected_target_net_profit_usd=active_policy.normal_exit_target_net_profit_usd,
        selected_plan=plan_evaluation.plan,
        attempts=(attempt,),
        reasons=(),
        **common,
    )
