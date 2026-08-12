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
from app.strategy.crypto_trade_management import (
    CryptoExitReason,
    CryptoProtectionPolicy,
    resolve_crypto_bar_exit,
    update_open_ended_runner_after_completed_bar,
)
from tools.replay_bybit_crypto import (
    _PendingEntry,
    _Position,
    _bars_by_symbol_and_time,
    _close_from_bar_exit,
    _close_position,
    _common_timestamps,
    default_crypto_config,
    _exit_event,
    _liquidation_equity,
    _metrics,
    _open_position,
    _strategy_snapshot,
    _terminal_snapshot,
)


def replay_open_ended_crypto_runner(
    acquisition: BybitKlineAcquisition,
    *,
    opening_equity_usdt: Decimal = Decimal("1000"),
    base_config: CryptoPerpStrategyConfig | None = None,
    protection_policy: CryptoProtectionPolicy | None = None,
    runner_policy: CryptoProfitRunnerPolicy | None = None,
    interval: str = "5",
) -> dict[str, Any]:
    """Replay one portfolio using >=$20 admission and no fixed take-profit ceiling.

    This is intentionally a separate shadow counterfactual from the fixed-target benchmark.
    Entry remains completed-bar signal -> next-bar open. Runner activation is observed from a
    completed bar and can only tighten the stop for the following bar, so no same-bar
    retroactive protection is credited to the strategy.
    """

    if opening_equity_usdt <= 0:
        raise ValueError("opening equity must be positive")
    runner = CryptoProfitRunnerPolicy() if runner_policy is None else runner_policy
    runner.validate()
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
            levels = build_crypto_profit_runner_levels(
                position.plan,
                actual_average_entry_price=position.entry_price,
                actual_filled_quantity=position.quantity,
                strategy_config=config,
                policy=runner,
            )
            positions[symbol] = position
            runner_levels[symbol] = levels
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
                    "runner_activation_price": float(levels.activation_price),
                    "runner_initial_protected_price": float(
                        levels.protected_price_at_activation
                    ),
                    "runner_trailing_distance": float(levels.trailing_distance),
                    "profit_cap_net_profit_usd": None,
                    "risk_budget_usdt": float(position.plan.risk_budget_usdt),
                }
            )

        for symbol, position in list(positions.items()):
            bar_exit = resolve_crypto_bar_exit(
                side=position.side,
                bar=current_bars[symbol],
                active_stop_price=position.protection.active_stop_price,
                active_stop_reason=position.protection.active_stop_reason,
                target_price=None,
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
            cooldown_until_index[symbol] = index + policy.cooldown_bars_after_stop
            decision_events.append(_exit_event(trade))

        for symbol, position in positions.items():
            position.bars_held += 1
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
    return {
        "mode": "MIN_20_NET_EDGE_OPEN_ENDED_RUNNER",
        "minimum_entry_net_profit_usd": float(runner.activation_net_profit_usd),
        "runner_activation_net_profit_usd": float(runner.activation_net_profit_usd),
        "runner_initial_protected_net_profit_usd": float(runner.protected_net_profit_usd),
        "profit_cap_net_profit_usd": None,
        "fixed_take_profit_enabled": False,
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
