from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class CryptoSessionRiskAction(StrEnum):
    ALLOW_NEW_ENTRY = "ALLOW_NEW_ENTRY"
    BLOCK_NEW_ENTRIES = "BLOCK_NEW_ENTRIES"
    FLATTEN_AND_BLOCK = "FLATTEN_AND_BLOCK"


@dataclass(frozen=True)
class CryptoSessionRiskPolicy:
    maximum_realized_loss_fraction: Decimal = Decimal("0.03")
    maximum_drawdown_fraction: Decimal = Decimal("0.05")
    maximum_execution_cost_fraction: Decimal = Decimal("0.02")
    maximum_consecutive_losses: int = 3
    minimum_equity_fraction: Decimal = Decimal("0.90")

    def validate(self) -> None:
        fractions = (
            self.maximum_realized_loss_fraction,
            self.maximum_drawdown_fraction,
            self.maximum_execution_cost_fraction,
        )
        if any(not Decimal("0") < value < Decimal("1") for value in fractions):
            raise ValueError("crypto session risk fractions must be within (0, 1)")
        if not Decimal("0") < self.minimum_equity_fraction <= Decimal("1"):
            raise ValueError("crypto minimum equity fraction must be within (0, 1]")
        if self.maximum_consecutive_losses < 1:
            raise ValueError("maximum consecutive crypto losses must be positive")


@dataclass(frozen=True)
class CryptoSessionRiskState:
    opening_equity_usdt: Decimal
    current_equity_usdt: Decimal
    peak_equity_usdt: Decimal
    realized_pnl_usdt: Decimal = Decimal("0")
    execution_cost_usdt: Decimal = Decimal("0")
    consecutive_losses: int = 0

    def validate(self) -> None:
        if self.opening_equity_usdt <= 0:
            raise ValueError("crypto session opening equity must be positive")
        if self.current_equity_usdt < 0:
            raise ValueError("crypto session current equity cannot be negative")
        if self.peak_equity_usdt <= 0:
            raise ValueError("crypto session peak equity must be positive")
        if self.peak_equity_usdt < self.current_equity_usdt:
            raise ValueError("crypto session peak equity cannot be below current equity")
        if self.execution_cost_usdt < 0:
            raise ValueError("crypto session execution costs cannot be negative")
        if self.consecutive_losses < 0:
            raise ValueError("crypto session consecutive losses cannot be negative")


@dataclass(frozen=True)
class CryptoSessionRiskDecision:
    action: CryptoSessionRiskAction
    reasons: tuple[str, ...]
    new_entries_allowed: bool
    flatten_required: bool


def evaluate_crypto_session_risk(
    state: CryptoSessionRiskState,
    policy: CryptoSessionRiskPolicy | None = None,
) -> CryptoSessionRiskDecision:
    """Fail closed when a $1k-style crypto session is degrading or overtrading."""

    state.validate()
    active_policy = CryptoSessionRiskPolicy() if policy is None else policy
    active_policy.validate()

    flatten_reasons: list[str] = []
    block_reasons: list[str] = []
    if state.current_equity_usdt <= (
        state.opening_equity_usdt * active_policy.minimum_equity_fraction
    ):
        flatten_reasons.append("SESSION_EQUITY_FLOOR_BREACHED")
    drawdown = (state.peak_equity_usdt - state.current_equity_usdt) / state.peak_equity_usdt
    if drawdown >= active_policy.maximum_drawdown_fraction:
        flatten_reasons.append("SESSION_DRAWDOWN_LIMIT_BREACHED")

    realized_loss = max(-state.realized_pnl_usdt, Decimal("0"))
    if realized_loss >= (
        state.opening_equity_usdt * active_policy.maximum_realized_loss_fraction
    ):
        block_reasons.append("SESSION_REALIZED_LOSS_LIMIT_BREACHED")
    if state.execution_cost_usdt >= (
        state.opening_equity_usdt * active_policy.maximum_execution_cost_fraction
    ):
        block_reasons.append("SESSION_EXECUTION_COST_BUDGET_EXHAUSTED")
    if state.consecutive_losses >= active_policy.maximum_consecutive_losses:
        block_reasons.append("SESSION_CONSECUTIVE_LOSS_LIMIT_REACHED")

    if flatten_reasons:
        return CryptoSessionRiskDecision(
            action=CryptoSessionRiskAction.FLATTEN_AND_BLOCK,
            reasons=tuple(flatten_reasons + block_reasons),
            new_entries_allowed=False,
            flatten_required=True,
        )
    if block_reasons:
        return CryptoSessionRiskDecision(
            action=CryptoSessionRiskAction.BLOCK_NEW_ENTRIES,
            reasons=tuple(block_reasons),
            new_entries_allowed=False,
            flatten_required=False,
        )
    return CryptoSessionRiskDecision(
        action=CryptoSessionRiskAction.ALLOW_NEW_ENTRY,
        reasons=(),
        new_entries_allowed=True,
        flatten_required=False,
    )
