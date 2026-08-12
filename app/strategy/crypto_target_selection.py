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
    """Entry objective for the crypto strategy.

    New positions must support at least ``minimum_entry_net_profit_usd`` after modeled
    execution costs. There is intentionally no upper profit cap: once a position has earned
    enough to arm the runner, exit management is expected to trail the move rather than place
    a fixed take-profit ceiling.

    ``fallback_protected_net_profit_usd`` is not an entry target and is not a guaranteed
    realized PnL. It is the minimum normal-fill protection objective after the runner has
    already reached its activation level; gaps and slippage can still realize less.
    """

    minimum_entry_net_profit_usd: Decimal = Decimal("20")
    fallback_protected_net_profit_usd: Decimal = Decimal("15")
    open_ended_profit_runner: bool = True
    entry_economics_policy: CryptoEntryEconomicsPolicy | None = None

    def validate(self) -> None:
        if self.minimum_entry_net_profit_usd <= 0:
            raise ValueError("crypto minimum entry net profit must be positive")
        if self.fallback_protected_net_profit_usd <= 0:
            raise ValueError("crypto protected fallback profit must be positive")
        if self.fallback_protected_net_profit_usd >= self.minimum_entry_net_profit_usd:
            raise ValueError("crypto protected fallback must be below the minimum entry edge")
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
    strategy_promotion_allowed: bool = False
    live_activation_allowed: bool = False


def select_highest_feasible_crypto_target(
    signal: CryptoSignal,
    *,
    equity_usdt: Decimal,
    config: CryptoPerpStrategyConfig,
    policy: CryptoTargetSelectionPolicy | None = None,
) -> CryptoTargetSelection:
    """Require >=$20 modeled net edge, then leave upside uncapped.

    The legacy name is retained for call-site compatibility, but the policy no longer chooses
    among fixed $15/$20/$25 take-profit ceilings. It performs one admission test at the minimum
    acceptable net edge. If that threshold is feasible, the trade is admitted with an
    open-ended runner objective. If it is not feasible, the correct action is no trade.

    A $15 objective is never used to justify a new entry.
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

    if reasons:
        return CryptoTargetSelection(
            eligible=False,
            selected_target_net_profit_usd=None,
            selected_plan=None,
            attempts=(attempt,),
            reasons=tuple(dict.fromkeys(reasons)) or ("MINIMUM_20_USD_NET_EDGE_UNAVAILABLE",),
            shadow_economics_enabled=active_policy.entry_economics_policy is not None,
            open_ended_profit_runner=active_policy.open_ended_profit_runner,
            profit_cap_net_profit_usd=None,
            fallback_protected_net_profit_usd=active_policy.fallback_protected_net_profit_usd,
        )

    return CryptoTargetSelection(
        eligible=True,
        selected_target_net_profit_usd=active_policy.minimum_entry_net_profit_usd,
        selected_plan=plan_evaluation.plan,
        attempts=(attempt,),
        reasons=(),
        shadow_economics_enabled=active_policy.entry_economics_policy is not None,
        open_ended_profit_runner=active_policy.open_ended_profit_runner,
        profit_cap_net_profit_usd=None,
        fallback_protected_net_profit_usd=active_policy.fallback_protected_net_profit_usd,
    )
