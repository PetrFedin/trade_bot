from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import CryptoSide


class CryptoExitReason(StrEnum):
    HARD_STOP = "HARD_STOP"
    BREAK_EVEN_STOP = "BREAK_EVEN_STOP"
    PROFIT_PROTECTION = "PROFIT_PROTECTION"
    NET_TARGET = "NET_TARGET"
    MAX_HOLD = "MAX_HOLD"
    END_OF_REPLAY = "END_OF_REPLAY"


@dataclass(frozen=True)
class CryptoProtectionPolicy:
    break_even_activation_r: Decimal = Decimal("0.80")
    profit_lock_activation_r: Decimal = Decimal("1.25")
    profit_lock_r: Decimal = Decimal("0.35")
    maximum_holding_bars: int = 36
    cooldown_bars_after_stop: int = 3
    cooldown_bars_after_target: int = 1

    def validate(self) -> None:
        if self.break_even_activation_r <= 0:
            raise ValueError("break-even activation must be positive")
        if self.profit_lock_activation_r <= self.break_even_activation_r:
            raise ValueError("profit-lock activation must be above break-even activation")
        if not Decimal("0") < self.profit_lock_r < self.profit_lock_activation_r:
            raise ValueError("profit-lock R must be positive and below activation R")
        if self.maximum_holding_bars <= 0:
            raise ValueError("maximum holding bars must be positive")
        if self.cooldown_bars_after_stop < 0 or self.cooldown_bars_after_target < 0:
            raise ValueError("cooldown bars must be non-negative")


@dataclass(frozen=True)
class CryptoProtectionState:
    active_stop_price: Decimal
    active_stop_reason: CryptoExitReason
    favorable_extreme: Decimal
    adverse_extreme: Decimal
    maximum_favorable_r: Decimal = Decimal("0")
    maximum_adverse_r: Decimal = Decimal("0")


@dataclass(frozen=True)
class CryptoBarExit:
    trigger_price: Decimal
    reason: CryptoExitReason
    gap_through: bool
    ambiguous_intrabar_path: bool


def initial_protection_state(
    *,
    side: CryptoSide,
    entry_price: Decimal,
    hard_stop_price: Decimal,
) -> CryptoProtectionState:
    if entry_price <= 0 or hard_stop_price <= 0:
        raise ValueError("entry and hard-stop prices must be positive")
    if side is CryptoSide.LONG and hard_stop_price >= entry_price:
        raise ValueError("long hard stop must be below entry")
    if side is CryptoSide.SHORT and hard_stop_price <= entry_price:
        raise ValueError("short hard stop must be above entry")
    return CryptoProtectionState(
        active_stop_price=hard_stop_price,
        active_stop_reason=CryptoExitReason.HARD_STOP,
        favorable_extreme=entry_price,
        adverse_extreme=entry_price,
    )


def update_protection_after_completed_bar(
    state: CryptoProtectionState,
    *,
    side: CryptoSide,
    entry_price: Decimal,
    risk_price_distance: Decimal,
    break_even_price: Decimal,
    completed_bar: BybitKlineBar,
    policy: CryptoProtectionPolicy,
) -> CryptoProtectionState:
    """Tighten protection for the *next* bar using only a completed bar.

    The completed bar may arm a break-even/profit lock, but the newly armed stop is never
    retroactively applied inside that same bar. This keeps OHLC replay conservative when
    the true intrabar path is unknown.
    """

    policy.validate()
    if risk_price_distance <= 0:
        raise ValueError("risk price distance must be positive")
    if completed_bar.symbol == "":
        raise ValueError("completed bar must have a symbol")

    if side is CryptoSide.LONG:
        favorable = max(state.favorable_extreme, completed_bar.high)
        adverse = min(state.adverse_extreme, completed_bar.low)
        maximum_favorable_r = max(
            state.maximum_favorable_r,
            (favorable - entry_price) / risk_price_distance,
        )
        maximum_adverse_r = max(
            state.maximum_adverse_r,
            (entry_price - adverse) / risk_price_distance,
        )
        active_stop = state.active_stop_price
        reason = state.active_stop_reason
        if maximum_favorable_r >= policy.break_even_activation_r:
            candidate = break_even_price
            candidate_reason = CryptoExitReason.BREAK_EVEN_STOP
            if maximum_favorable_r >= policy.profit_lock_activation_r:
                candidate = max(
                    candidate,
                    entry_price + risk_price_distance * policy.profit_lock_r,
                )
                candidate_reason = CryptoExitReason.PROFIT_PROTECTION
            if candidate > active_stop:
                active_stop = candidate
                reason = candidate_reason
    else:
        favorable = min(state.favorable_extreme, completed_bar.low)
        adverse = max(state.adverse_extreme, completed_bar.high)
        maximum_favorable_r = max(
            state.maximum_favorable_r,
            (entry_price - favorable) / risk_price_distance,
        )
        maximum_adverse_r = max(
            state.maximum_adverse_r,
            (adverse - entry_price) / risk_price_distance,
        )
        active_stop = state.active_stop_price
        reason = state.active_stop_reason
        if maximum_favorable_r >= policy.break_even_activation_r:
            candidate = break_even_price
            candidate_reason = CryptoExitReason.BREAK_EVEN_STOP
            if maximum_favorable_r >= policy.profit_lock_activation_r:
                candidate = min(
                    candidate,
                    entry_price - risk_price_distance * policy.profit_lock_r,
                )
                candidate_reason = CryptoExitReason.PROFIT_PROTECTION
            if candidate < active_stop:
                active_stop = candidate
                reason = candidate_reason

    return CryptoProtectionState(
        active_stop_price=active_stop,
        active_stop_reason=reason,
        favorable_extreme=favorable,
        adverse_extreme=adverse,
        maximum_favorable_r=maximum_favorable_r,
        maximum_adverse_r=maximum_adverse_r,
    )


def resolve_crypto_bar_exit(
    *,
    side: CryptoSide,
    bar: BybitKlineBar,
    active_stop_price: Decimal,
    active_stop_reason: CryptoExitReason,
    target_price: Decimal,
) -> CryptoBarExit | None:
    if active_stop_price <= 0 or target_price <= 0:
        raise ValueError("crypto exit prices must be positive")
    if active_stop_reason not in {
        CryptoExitReason.HARD_STOP,
        CryptoExitReason.BREAK_EVEN_STOP,
        CryptoExitReason.PROFIT_PROTECTION,
    }:
        raise ValueError("active stop reason must be protective")

    if side is CryptoSide.LONG:
        if bar.open <= active_stop_price:
            return CryptoBarExit(bar.open, active_stop_reason, True, False)
        if bar.open >= target_price:
            return CryptoBarExit(bar.open, CryptoExitReason.NET_TARGET, True, False)
        stop_hit = bar.low <= active_stop_price
        target_hit = bar.high >= target_price
    else:
        if bar.open >= active_stop_price:
            return CryptoBarExit(bar.open, active_stop_reason, True, False)
        if bar.open <= target_price:
            return CryptoBarExit(bar.open, CryptoExitReason.NET_TARGET, True, False)
        stop_hit = bar.high >= active_stop_price
        target_hit = bar.low <= target_price

    if stop_hit and target_hit:
        return CryptoBarExit(
            active_stop_price,
            active_stop_reason,
            False,
            True,
        )
    if stop_hit:
        return CryptoBarExit(active_stop_price, active_stop_reason, False, False)
    if target_hit:
        return CryptoBarExit(target_price, CryptoExitReason.NET_TARGET, False, False)
    return None
