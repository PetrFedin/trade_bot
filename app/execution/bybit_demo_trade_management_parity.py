from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from app.execution.bybit_demo_excursion_tracker import BybitDemoTradeExcursionState
from app.execution.bybit_demo_protection_client import BybitDemoProtectionPosition
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide
from app.strategy.crypto_profit_runner import modeled_raw_trigger_for_net_profit
from app.strategy.crypto_trade_management import (
    CryptoExitReason,
    CryptoProtectionPolicy,
    initial_protection_state,
    update_protection_after_completed_bar,
)


class BybitDemoTradeManagementParityAction(StrEnum):
    BLOCKED = "BLOCKED"
    NO_CHANGE = "NO_CHANGE"
    RATCHET_BREAK_EVEN = "RATCHET_BREAK_EVEN"
    RATCHET_PROFIT_LOCK = "RATCHET_PROFIT_LOCK"
    MAX_HOLD_CLOSE_REQUIRED = "MAX_HOLD_CLOSE_REQUIRED"


@dataclass(frozen=True)
class BybitDemoTradeManagementParityDecision:
    action: BybitDemoTradeManagementParityAction
    reasons: tuple[str, ...]
    current_stop_loss_price: Decimal | None
    desired_stop_loss_price: Decimal | None
    desired_stop_reason: str | None
    break_even_price: Decimal | None
    maximum_favorable_r: Decimal
    maximum_adverse_r: Decimal
    completed_bar_count: int
    holding_bar_count: int
    maximum_holding_bars: int
    max_hold_close_required: bool
    exit_mode: str | None
    fixed_take_profit_preserved: bool
    runner_trailing_preserved: bool
    stop_never_widens: bool = True
    completed_bar_only: bool = True
    baseline_research_policy_only: bool = True
    tight_profit_lock_candidate_allowed: bool = False
    demo_stop_ratchet_write_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def evaluate_bybit_demo_trade_management_parity(
    excursion: BybitDemoTradeExcursionState,
    *,
    position: BybitDemoProtectionPosition,
    completed_bars_since_entry: Sequence[BybitKlineBar],
    strategy_config: CryptoPerpStrategyConfig,
    instrument: BybitInstrumentSpec,
    protection_policy: CryptoProtectionPolicy | None = None,
    completed_holding_bar_count: int | None = None,
) -> BybitDemoTradeManagementParityDecision:
    """Rebuild baseline research stop management from completed bars for one demo position.

    The evaluator is intentionally stateless. Replaying all safe completed bars after the actual
    fill makes a restart unable to erase a previously reached 0.80R/1.25R protection threshold.
    A caller may count the entry bucket for holding-time without feeding its pre-fill high/low into
    protection math by passing ``completed_holding_bar_count=len(bars)+1``. The rejected tighter
    profit-lock candidate is never selected. Exchange-native runner trailing remains intact; this
    layer compares only the independent full-position stop-loss field.
    """

    instrument.validate()
    strategy_config.validate()
    policy = CryptoProtectionPolicy() if protection_policy is None else protection_policy
    policy.validate()
    _validate_baseline_policy(policy)

    reasons = _basis_reasons(excursion, position, instrument)
    if reasons:
        return _blocked(excursion, position, policy, reasons)
    bars = tuple(completed_bars_since_entry)
    holding_bar_count = (
        len(bars) if completed_holding_bar_count is None else completed_holding_bar_count
    )
    count_reasons = _holding_count_reasons(
        protection_bar_count=len(bars),
        holding_bar_count=holding_bar_count,
    )
    if count_reasons:
        return _blocked(
            excursion,
            position,
            policy,
            count_reasons,
            bar_count=len(bars),
            holding_bar_count=holding_bar_count,
        )
    bar_reasons = _bar_reasons(bars, symbol=excursion.symbol)
    if bar_reasons:
        return _blocked(
            excursion,
            position,
            policy,
            bar_reasons,
            bar_count=len(bars),
            holding_bar_count=holding_bar_count,
        )
    if position.stop_loss_price is None:
        return _blocked(
            excursion,
            position,
            policy,
            ("EXCHANGE_STOP_LOSS_UNAVAILABLE",),
            bar_count=len(bars),
            holding_bar_count=holding_bar_count,
        )

    exit_mode = _exit_mode(position)
    if exit_mode is None:
        return _blocked(
            excursion,
            position,
            policy,
            ("EXCHANGE_PROTECTION_MODE_UNRESOLVED",),
            bar_count=len(bars),
            holding_bar_count=holding_bar_count,
        )

    hard_stop_raw = _hard_stop_price(
        side=excursion.side,
        entry_price=excursion.entry_price,
        stop_fraction=excursion.stop_fraction,
    )
    hard_stop = instrument.normalize_protective_stop_price(
        excursion.side.value,
        hard_stop_raw,
    )
    break_even_raw = modeled_raw_trigger_for_net_profit(
        side=excursion.side,
        actual_average_entry_price=excursion.entry_price,
        actual_filled_quantity=excursion.initial_quantity,
        desired_net_profit_usd=Decimal("0"),
        strategy_config=strategy_config,
    )
    break_even = instrument.normalize_protective_stop_price(
        excursion.side.value,
        break_even_raw,
    )
    risk_distance = excursion.entry_price * excursion.stop_fraction
    state = initial_protection_state(
        side=excursion.side,
        entry_price=excursion.entry_price,
        hard_stop_price=hard_stop,
    )
    for bar in bars:
        state = update_protection_after_completed_bar(
            state,
            side=excursion.side,
            entry_price=excursion.entry_price,
            risk_price_distance=risk_distance,
            break_even_price=break_even,
            completed_bar=bar,
            policy=policy,
        )

    desired_stop = instrument.normalize_protective_stop_price(
        excursion.side.value,
        state.active_stop_price,
    )
    max_hold = holding_bar_count >= policy.maximum_holding_bars
    ratchet_required = _more_protective(
        side=excursion.side,
        candidate=desired_stop,
        current=position.stop_loss_price,
    )
    if max_hold:
        action = BybitDemoTradeManagementParityAction.MAX_HOLD_CLOSE_REQUIRED
        decision_reasons = ("BASELINE_MAXIMUM_HOLDING_BARS_REACHED",)
    elif not ratchet_required:
        action = BybitDemoTradeManagementParityAction.NO_CHANGE
        decision_reasons = ()
    elif state.active_stop_reason is CryptoExitReason.BREAK_EVEN_STOP:
        action = BybitDemoTradeManagementParityAction.RATCHET_BREAK_EVEN
        decision_reasons = ("BASELINE_BREAK_EVEN_RATCHET_DUE",)
    elif state.active_stop_reason is CryptoExitReason.PROFIT_PROTECTION:
        action = BybitDemoTradeManagementParityAction.RATCHET_PROFIT_LOCK
        decision_reasons = ("BASELINE_PROFIT_LOCK_RATCHET_DUE",)
    else:
        action = BybitDemoTradeManagementParityAction.NO_CHANGE
        decision_reasons = ()

    return BybitDemoTradeManagementParityDecision(
        action=action,
        reasons=decision_reasons,
        current_stop_loss_price=position.stop_loss_price,
        desired_stop_loss_price=desired_stop,
        desired_stop_reason=state.active_stop_reason.value,
        break_even_price=break_even,
        maximum_favorable_r=state.maximum_favorable_r,
        maximum_adverse_r=state.maximum_adverse_r,
        completed_bar_count=len(bars),
        holding_bar_count=holding_bar_count,
        maximum_holding_bars=policy.maximum_holding_bars,
        max_hold_close_required=max_hold,
        exit_mode=exit_mode,
        fixed_take_profit_preserved=exit_mode == "FIXED_20_TARGET",
        runner_trailing_preserved=exit_mode == "OPEN_ENDED_RUNNER",
    )


def _basis_reasons(
    excursion: BybitDemoTradeExcursionState,
    position: BybitDemoProtectionPosition,
    instrument: BybitInstrumentSpec,
) -> tuple[str, ...]:
    reasons: list[str] = []
    expected_side = "Buy" if excursion.side is CryptoSide.LONG else "Sell"
    if excursion.live_mainnet_order_routing_allowed:
        reasons.append("EXCURSION_STATE_PERMITS_LIVE_ROUTING")
    if excursion.symbol != instrument.symbol:
        reasons.append("INSTRUMENT_SYMBOL_MISMATCH")
    if position.symbol != excursion.symbol or position.side != expected_side:
        reasons.append("POSITION_IDENTITY_MISMATCH")
    if position.average_price != excursion.entry_price:
        reasons.append("ACTUAL_ENTRY_PRICE_MISMATCH")
    if position.size <= 0:
        reasons.append("POSITION_NOT_OPEN")
    elif position.size != excursion.initial_quantity:
        reasons.append("PARTIAL_OR_CHANGED_POSITION_SIZE_NOT_BASELINE_PARITY")
    if excursion.entry_price <= 0 or excursion.initial_quantity <= 0:
        reasons.append("INVALID_EXCURSION_BASIS")
    if excursion.stop_fraction <= 0:
        reasons.append("INVALID_STOP_FRACTION")
    return tuple(dict.fromkeys(reasons))


def _holding_count_reasons(
    *,
    protection_bar_count: int,
    holding_bar_count: int,
) -> tuple[str, ...]:
    if holding_bar_count < 0:
        return ("HOLDING_BAR_COUNT_NEGATIVE",)
    if holding_bar_count < protection_bar_count:
        return ("HOLDING_BAR_COUNT_BELOW_PROTECTION_HISTORY",)
    if holding_bar_count > protection_bar_count + 1:
        return ("HOLDING_BAR_HISTORY_GAP",)
    return ()


def _bar_reasons(
    bars: Sequence[BybitKlineBar],
    *,
    symbol: str,
) -> tuple[str, ...]:
    previous_time = None
    for bar in bars:
        if bar.symbol != symbol:
            return ("COMPLETED_BAR_SYMBOL_MISMATCH",)
        if previous_time is not None and bar.start_time <= previous_time:
            return ("COMPLETED_BARS_NOT_STRICTLY_INCREASING",)
        previous_time = bar.start_time
    return ()


def _exit_mode(position: BybitDemoProtectionPosition) -> str | None:
    if (
        position.take_profit_price is not None
        and position.trailing_stop_distance is None
    ):
        return "FIXED_20_TARGET"
    if (
        position.take_profit_price is None
        and position.trailing_stop_distance is not None
    ):
        return "OPEN_ENDED_RUNNER"
    return None


def _hard_stop_price(
    *,
    side: CryptoSide,
    entry_price: Decimal,
    stop_fraction: Decimal,
) -> Decimal:
    move = entry_price * stop_fraction
    return entry_price - move if side is CryptoSide.LONG else entry_price + move


def _more_protective(
    *,
    side: CryptoSide,
    candidate: Decimal,
    current: Decimal,
) -> bool:
    return candidate > current if side is CryptoSide.LONG else candidate < current


def _validate_baseline_policy(policy: CryptoProtectionPolicy) -> None:
    baseline = CryptoProtectionPolicy()
    if policy != baseline:
        raise ValueError(
            "demo trade-management parity currently accepts only frozen baseline protection policy"
        )


def _blocked(
    excursion: BybitDemoTradeExcursionState,
    position: BybitDemoProtectionPosition,
    policy: CryptoProtectionPolicy,
    reasons: tuple[str, ...],
    *,
    bar_count: int = 0,
    holding_bar_count: int | None = None,
) -> BybitDemoTradeManagementParityDecision:
    resolved_holding_count = bar_count if holding_bar_count is None else holding_bar_count
    return BybitDemoTradeManagementParityDecision(
        action=BybitDemoTradeManagementParityAction.BLOCKED,
        reasons=reasons,
        current_stop_loss_price=position.stop_loss_price,
        desired_stop_loss_price=None,
        desired_stop_reason=None,
        break_even_price=None,
        maximum_favorable_r=Decimal("0"),
        maximum_adverse_r=Decimal("0"),
        completed_bar_count=bar_count,
        holding_bar_count=resolved_holding_count,
        maximum_holding_bars=policy.maximum_holding_bars,
        max_hold_close_required=False,
        exit_mode=_exit_mode(position),
        fixed_take_profit_preserved=False,
        runner_trailing_preserved=False,
    )
