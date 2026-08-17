from __future__ import annotations

from collections import Counter
from dataclasses import replace
from decimal import Decimal
from typing import Any

from app.marketdata.bybit_v5 import BybitKlineAcquisition
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    build_trade_plan,
    rank_crypto_signals,
)
from app.strategy.crypto_profit_runner import (
    CryptoProfitRunnerLevels,
    CryptoProfitRunnerPolicy,
    build_crypto_profit_runner_levels,
)
from app.strategy.crypto_runner_admission import (
    CryptoRunnerAdmissionPolicy,
    evaluate_crypto_runner_admission,
)
from app.strategy.crypto_trade_management import (
    CryptoExitReason,
    CryptoProtectionPolicy,
    resolve_crypto_bar_exit,
    update_open_ended_runner_after_completed_bar,
    update_protection_after_completed_bar,
)
from tools import replay_bybit_crypto as replay_core

_PendingEntry = replay_core._PendingEntry
_Position = replay_core._Position
_bars_by_symbol_and_time = replay_core._bars_by_symbol_and_time
_close_from_bar_exit = replay_core._close_from_bar_exit
_close_position = replay_core._close_position
_common_timestamps = replay_core._common_timestamps
_exit_event = replay_core._exit_event
_liquidation_equity = replay_core._liquidation_equity
_metrics = replay_core._metrics
_open_position = replay_core._open_position
_strategy_snapshot = replay_core._strategy_snapshot
_terminal_snapshot = replay_core._terminal_snapshot
default_crypto_config = replay_core.default_crypto_config


def replay_open_ended_crypto_runner(
    acquisition: BybitKlineAcquisition,
    *,
    opening_equity_usdt: Decimal = Decimal("1000"),
    base_config: CryptoPerpStrategyConfig | None = None,
    protection_policy: CryptoProtectionPolicy | None = None,
    runner_policy: CryptoProfitRunnerPolicy | None = None,
    runner_admission_policy: CryptoRunnerAdmissionPolicy | None = None,
    interval: str = "5",
) -> dict[str, Any]:
    """Replay a >=$20 portfolio with optional excess-edge-gated unlimited runners.

    With ``runner_admission_policy=None`` every accepted position uses the original unlimited
    runner shadow. When an admission policy is supplied, positions whose expected net edge is
    not sufficiently above the $20 activation keep the fixed $20 target; only high-excess-edge
    positions give up that fixed target for the unlimited runner.

    Entry remains completed-bar signal -> next-bar open. Runner activation is observed from a
    completed bar and can only tighten the stop for the following bar, so no same-bar
    retroactive protection is credited to the strategy.
    """

    if opening_equity_usdt <= 0:
        raise ValueError("opening equity must be positive")
    runner = CryptoProfitRunnerPolicy() if runner_policy is None else runner_policy
    runner.validate()
    if runner_admission_policy is not None:
        runner_admission_policy.validate()
    base = default_crypto_config() if base_config is None else base_config
    config = replace(base, target_net_profit_usd=runner.activation_net_profit_usd)
    policy = CryptoProtectionPolicy() if protection_policy is None else protection_policy
    config.validate()
    policy.validate()

    bars_by_symbol_and_time = _bars_by_symbol_and_time(acquisition.bars)
    common_times = _common_timestamps(bars_by_symbol_and_time)
    if len(common_times) < 3:
        raise ValueError("Bybit crypto runner replay requires synchronized timestamps")
    bars_by_symbol = {
        symbol: tuple(rows[timestamp] for timestamp in common_times)
        for symbol, rows in bars_by_symbol_and_time.items()
    }

    histories = {symbol: [] for symbol in bars_by_symbol}
    positions: dict[str, _Position] = {}
    runner_levels: dict[str, CryptoProfitRunnerLevels] = {}
    pending_entries: list[_PendingEntry] = []
    pending_max_hold_exits: set[str] = set()
    cooldown_until_index: dict[str, int] = {}
    closed = []
    cash_equity = opening_equity_usdt
    peak_equity = opening_equity_usdt
    maximum_drawdown = Decimal("0")
    equity_curve: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    plan_block_counts: Counter[str] = Counter()
    signal_event_count = 0
    plan_event_count = 0
    maximum_concurrent = 0
    runner_activation_event_count = 0
    runner_selected_trade_count = 0
    fixed_target_selected_trade_count = 0
    runner_armed: set[str] = set()
    decision_events: list[dict[str, Any]] = []

    for index, timestamp in enumerate(common_times):
        current_bars = {symbol: bars[index] for symbol, bars in bars_by_symbol.items()}

        for symbol in tuple(pending_max_hold_exits):
            position = positions.get(symbol)
            if position is None:
                pending_max_hold_exits.discard(symbol)
                continue
            trade, cash_delta = _close_position(
                position,
                raw_exit_price=current_bars[symbol].open,
                exit_time=timestamp,
                reason=CryptoExitReason.MAX_HOLD,
                config=config,
                gap_through=False,
                ambiguous=False,
            )
            cash_equity += cash_delta
            closed.append(trade)
            positions.pop(symbol)
            runner_levels.pop(symbol, None)
            runner_armed.discard(symbol)
            pending_max_hold_exits.discard(symbol)
            cooldown_until_index[symbol] = index + policy.cooldown_bars_after_stop
            decision_events.append(_exit_event(trade))

        entries_to_execute = pending_entries
        pending_entries = []
        for pending in entries_to_execute:
            if pending.plan.symbol in positions:
                continue
            if len(positions) >= config.maximum_concurrent_positions:
                break
            symbol = pending.plan.symbol
            if index < cooldown_until_index.get(symbol, -1):
                continue
            position = _open_position(pending, bar=current_bars[symbol], config=config)
            admission = None
            runner_selected = True
            if runner_admission_policy is not None:
                admission = evaluate_crypto_runner_admission(
                    position.plan,
                    runner_policy=runner,
                    admission_policy=runner_admission_policy,
                )
                runner_selected = admission.eligible

            levels: CryptoProfitRunnerLevels | None = None
            if runner_selected:
                levels = build_crypto_profit_runner_levels(
                    position.plan,
                    actual_average_entry_price=position.entry_price,
                    actual_filled_quantity=position.quantity,
                    strategy_config=config,
                    policy=runner,
                )
                runner_levels[symbol] = levels
                runner_selected_trade_count += 1
            else:
                fixed_target_selected_trade_count += 1

            positions[symbol] = position
            cash_equity -= position.entry_fee
            decision_events.append(
                {
                    "event": "ENTRY",
                    "symbol": symbol,
                    "side": position.side.value,
                    "decision_time": position.decision_time,
                    "execution_time": position.entry_time,
                    "entry_price": float(position.entry_price),
                    "quantity": float(position.quantity),
                    "minimum_entry_net_edge_usd": float(runner.activation_net_profit_usd),
                    "expected_net_edge_usd": float(position.plan.expected_net_edge_usd),
                    "exit_mode": "OPEN_ENDED_RUNNER" if runner_selected else "FIXED_20_TARGET",
                    "runner_activation_price": (
                        None if levels is None else float(levels.activation_price)
                    ),
                    "runner_initial_protected_price": (
                        None
                        if levels is None
                        else float(levels.protected_price_at_activation)
                    ),
                    "runner_trailing_distance": (
                        None if levels is None else float(levels.trailing_distance)
                    ),
                    "runner_required_expected_net_edge_usd": (
                        None
                        if admission is None
                        else float(admission.required_expected_net_edge_usd)
                    ),
                    "runner_admission_reasons": (
                        [] if admission is None else list(admission.reasons)
                    ),
                    "fixed_target_price": (
                        None
                        if runner_selected
                        else float(position.target_trigger_price)
                    ),
                    "profit_cap_net_profit_usd": None if runner_selected else 20.0,
                    "risk_budget_usdt": float(position.plan.risk_budget_usdt),
                }
            )

        for symbol, position in list(positions.items()):
            runner_selected = symbol in runner_levels
            bar_exit = resolve_crypto_bar_exit(
                side=position.side,
                bar=current_bars[symbol],
                active_stop_price=position.protection.active_stop_price,
                active_stop_reason=position.protection.active_stop_reason,
                target_price=None if runner_selected else position.target_trigger_price,
            )
            if bar_exit is None:
                continue
            trade, cash_delta = _close_from_bar_exit(
                position,
                bar_exit=bar_exit,
                exit_time=timestamp,
                config=config,
            )
            cash_equity += cash_delta
            closed.append(trade)
            positions.pop(symbol)
            runner_levels.pop(symbol, None)
            runner_armed.discard(symbol)
            cooldown_until_index[symbol] = index + _cooldown_bars_for_reason(
                bar_exit.reason,
                policy,
            )
            decision_events.append(_exit_event(trade))

        for symbol, position in positions.items():
            position.bars_held += 1
            if symbol in runner_levels:
                levels = runner_levels[symbol]
                previous_favorable = position.protection.favorable_extreme
                position.protection = update_open_ended_runner_after_completed_bar(
                    position.protection,
                    side=position.side,
                    entry_price=position.entry_price,
                    risk_price_distance=position.risk_price_distance,
                    break_even_price=position.break_even_trigger_price,
                    runner_activation_price=levels.activation_price,
                    runner_protected_price_at_activation=levels.protected_price_at_activation,
                    runner_trailing_distance=levels.trailing_distance,
                    completed_bar=current_bars[symbol],
                    policy=policy,
                )
                if symbol not in runner_armed and _runner_reached_activation(
                    position,
                    levels,
                    previous_favorable=previous_favorable,
                ):
                    runner_armed.add(symbol)
                    runner_activation_event_count += 1
                    decision_events.append(
                        {
                            "event": "RUNNER_ACTIVATED",
                            "symbol": symbol,
                            "side": position.side.value,
                            "decision_time": timestamp.isoformat(),
                            "active_stop_for_next_bar": float(
                                position.protection.active_stop_price
                            ),
                            "profit_cap_net_profit_usd": None,
                        }
                    )
            else:
                position.protection = update_protection_after_completed_bar(
                    position.protection,
                    side=position.side,
                    entry_price=position.entry_price,
                    risk_price_distance=position.risk_price_distance,
                    break_even_price=position.break_even_trigger_price,
                    completed_bar=current_bars[symbol],
                    policy=policy,
                )
            if position.bars_held >= policy.maximum_holding_bars:
                pending_max_hold_exits.add(symbol)

        for symbol, bar in current_bars.items():
            histories[symbol].append(bar)

        equity = _liquidation_equity(cash_equity, positions, current_bars, config)
        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            drawdown = (peak_equity - equity) / peak_equity
            maximum_drawdown = max(maximum_drawdown, drawdown)
        equity_curve.append({"time": timestamp.isoformat(), "equity": float(equity)})

        if index >= len(common_times) - 1:
            continue
        rankings = rank_crypto_signals(histories, config)
        available_slots = config.maximum_concurrent_positions - len(positions)
        already_pending = {pending.plan.symbol for pending in pending_entries}
        for evaluation in rankings:
            if evaluation.signal is None:
                reason_counts.update(evaluation.reasons)
                continue
            signal_event_count += 1
            symbol = evaluation.signal.symbol
            if symbol in positions or symbol in already_pending:
                continue
            if index < cooldown_until_index.get(symbol, -1):
                plan_block_counts["COOLDOWN_ACTIVE"] += 1
                continue
            plan_evaluation = build_trade_plan(
                evaluation.signal,
                equity_usdt=equity,
                config=config,
            )
            if not plan_evaluation.eligible or plan_evaluation.plan is None:
                plan_block_counts.update(plan_evaluation.reasons)
                decision_events.append(
                    {
                        "event": "MINIMUM_20_NET_EDGE_BLOCK",
                        "symbol": symbol,
                        "side": evaluation.signal.side.value,
                        "decision_time": timestamp.isoformat(),
                        "reasons": list(plan_evaluation.reasons),
                        "quality_score": float(evaluation.signal.quality_score),
                        "atr_fraction": float(evaluation.signal.atr_fraction),
                    }
                )
                continue
            if available_slots <= 0:
                plan_block_counts["CONCURRENCY_LIMIT"] += 1
                continue
            plan_event_count += 1
            pending_entries.append(
                _PendingEntry(plan=plan_evaluation.plan, signal=evaluation.signal)
            )
            already_pending.add(symbol)
            available_slots -= 1

        maximum_concurrent = max(maximum_concurrent, len(positions) + len(pending_entries))

    if common_times:
        final_time = common_times[-1]
        final_bars = {symbol: bars[-1] for symbol, bars in bars_by_symbol.items()}
        for symbol, position in list(positions.items()):
            trade, cash_delta = _close_position(
                position,
                raw_exit_price=final_bars[symbol].close,
                exit_time=final_time,
                reason=CryptoExitReason.END_OF_REPLAY,
                config=config,
                gap_through=False,
                ambiguous=False,
            )
            cash_equity += cash_delta
            closed.append(trade)
            positions.pop(symbol)
            runner_levels.pop(symbol, None)
            runner_armed.discard(symbol)
            decision_events.append(_exit_event(trade))

    final_equity = cash_equity
    metrics = _metrics(
        closed,
        opening_equity=opening_equity_usdt,
        final_equity=final_equity,
        maximum_drawdown=maximum_drawdown,
        first_time=common_times[0],
        last_time=common_times[-1],
        interval=interval,
        maximum_concurrent=maximum_concurrent,
    )
    terminal = _terminal_snapshot(histories, final_equity=final_equity, config=config)
    conditional = runner_admission_policy is not None
    return {
        "mode": (
            "MIN_20_NET_EDGE_CONDITIONAL_OPEN_ENDED_RUNNER"
            if conditional
            else "MIN_20_NET_EDGE_OPEN_ENDED_RUNNER"
        ),
        "minimum_entry_net_profit_usd": float(runner.activation_net_profit_usd),
        "runner_activation_net_profit_usd": float(runner.activation_net_profit_usd),
        "runner_initial_protected_net_profit_usd": float(runner.protected_net_profit_usd),
        "runner_minimum_expected_edge_multiple": (
            None
            if runner_admission_policy is None
            else float(runner_admission_policy.minimum_expected_edge_multiple)
        ),
        "runner_selected_trade_count": runner_selected_trade_count,
        "fixed_target_selected_trade_count": fixed_target_selected_trade_count,
        "profit_cap_net_profit_usd": None if not conditional else "CONDITIONAL_BY_TRADE",
        "fixed_take_profit_enabled": conditional,
        "runner_activation_event_count": runner_activation_event_count,
        "metrics": metrics,
        "closed_trades": [trade.as_dict() for trade in closed],
        "signal_filter_reason_counts": dict(reason_counts),
        "trade_plan_block_reason_counts": dict(plan_block_counts),
        "eligible_signal_event_count": signal_event_count,
        "accepted_trade_plan_event_count": plan_event_count,
        "terminal_completed_bar_signal": terminal,
        "decision_events": decision_events,
        "equity_curve": equity_curve,
        "no_lookahead_contract": "completed bar decision -> next bar open execution",
        "runner_protection_contract": (
            "completed favorable bar arms/tightens runner only for the next bar"
        ),
        "conditional_runner_contract": (
            "fixed $20 target unless pre-entry expected net edge clears the excess-edge runner gate"
            if conditional
            else "all accepted positions use the open-ended runner shadow"
        ),
        "protected_15_is_guaranteed_realized_pnl": False,
        "strategy_promotion_allowed": False,
        "bybit_demo_order_writes_enabled": False,
        "bybit_live_order_routing_allowed": False,
        "strategy": _strategy_snapshot(config, policy),
    }


def _runner_reached_activation(
    position: _Position,
    levels: CryptoProfitRunnerLevels,
    *,
    previous_favorable: Decimal,
) -> bool:
    if position.side.value == "LONG":
        return (
            previous_favorable < levels.activation_price
            <= position.protection.favorable_extreme
        )
    return (
        previous_favorable > levels.activation_price
        >= position.protection.favorable_extreme
    )


def _cooldown_bars_for_reason(
    reason: CryptoExitReason,
    policy: CryptoProtectionPolicy,
) -> int:
    if reason is CryptoExitReason.NET_TARGET:
        return policy.cooldown_bars_after_target
    return policy.cooldown_bars_after_stop