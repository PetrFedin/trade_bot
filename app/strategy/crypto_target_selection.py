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
    targets_usd_descending: tuple[Decimal, ...] = (
        Decimal("25"),
        Decimal("20"),
        Decimal("15"),
    )
    entry_economics_policy: CryptoEntryEconomicsPolicy | None = None

    def validate(self) -> None:
        if not self.targets_usd_descending:
            raise ValueError("crypto target selector requires targets")
        if any(target <= 0 for target in self.targets_usd_descending):
            raise ValueError("crypto target selector targets must be positive")
        if len(set(self.targets_usd_descending)) != len(self.targets_usd_descending):
            raise ValueError("crypto target selector targets must be unique")
        if self.targets_usd_descending != tuple(
            sorted(self.targets_usd_descending, reverse=True)
        ):
            raise ValueError("crypto target selector targets must be descending")
        if self.entry_economics_policy is not None:
            self.entry_economics_policy.validate()


@dataclass(frozen=True)
class CryptoTargetAttempt:
    target_net_profit_usd: Decimal
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
    strategy_promotion_allowed: bool = False
    live_activation_allowed: bool = False


def select_highest_feasible_crypto_target(
    signal: CryptoSignal,
    *,
    equity_usdt: Decimal,
    config: CryptoPerpStrategyConfig,
    policy: CryptoTargetSelectionPolicy | None = None,
) -> CryptoTargetSelection:
    """Pick the highest requested net-dollar target actually supported by the signal.

    The selector never relaxes risk sizing to force a target. It asks the existing cost-aware
    planner whether $25, then $20, then $15 is feasible under the same risk configuration.
    An optional entry-economics policy can make this a stricter research-only selection.
    """

    active_policy = CryptoTargetSelectionPolicy() if policy is None else policy
    active_policy.validate()
    config.validate()
    attempts: list[CryptoTargetAttempt] = []
    aggregate_reasons: list[str] = []

    for target in active_policy.targets_usd_descending:
        target_config = config.with_target(target)
        plan_evaluation = build_trade_plan(
            signal,
            equity_usdt=equity_usdt,
            config=target_config,
        )
        economics: CryptoEntryEconomicsDecision | None = None
        if plan_evaluation.plan is not None and active_policy.entry_economics_policy is not None:
            economics = evaluate_entry_economics(
                plan_evaluation.plan,
                active_policy.entry_economics_policy,
            )
        attempts.append(
            CryptoTargetAttempt(
                target_net_profit_usd=target,
                base_plan_eligible=plan_evaluation.eligible,
                base_plan_reasons=plan_evaluation.reasons,
                entry_economics=economics,
            )
        )
        if not plan_evaluation.eligible or plan_evaluation.plan is None:
            aggregate_reasons.extend(plan_evaluation.reasons)
            continue
        if economics is not None and not economics.eligible:
            aggregate_reasons.extend(economics.reasons)
            continue
        return CryptoTargetSelection(
            eligible=True,
            selected_target_net_profit_usd=target,
            selected_plan=plan_evaluation.plan,
            attempts=tuple(attempts),
            reasons=(),
            shadow_economics_enabled=active_policy.entry_economics_policy is not None,
        )

    reasons = tuple(dict.fromkeys(aggregate_reasons)) or ("NO_NET_TARGET_FEASIBLE",)
    return CryptoTargetSelection(
        eligible=False,
        selected_target_net_profit_usd=None,
        selected_plan=None,
        attempts=tuple(attempts),
        reasons=reasons,
        shadow_economics_enabled=active_policy.entry_economics_policy is not None,
    )