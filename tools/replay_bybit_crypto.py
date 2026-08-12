from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.bybit_v5 import (
    BybitKlineAcquisition,
    BybitKlineBar,
    BybitKlineRequest,
    BybitPublicKlineClient,
    interval_milliseconds,
    last_completed_kline_end_ms,
)
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSide,
    CryptoSignal,
    CryptoTradePlan,
    build_trade_plan,
    rank_crypto_signals,
)
from app.strategy.crypto_trade_management import (
    CryptoBarExit,
    CryptoExitReason,
    CryptoProtectionPolicy,
    CryptoProtectionState,
    initial_protection_state,
    resolve_crypto_bar_exit,
    update_protection_after_completed_bar,
)

_BPS = Decimal("10000")
_DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "ADAUSDT",
)
_DEFAULT_TARGETS = (Decimal("15"), Decimal("20"), Decimal("25"))


@dataclass(frozen=True)
class _PendingEntry:
    plan: CryptoTradePlan
    signal: CryptoSignal


@dataclass
class _Position:
    plan: CryptoTradePlan
    side: CryptoSide
    decision_time: str
    entry_time: str
    entry_price: Decimal
    quantity: Decimal
    entry_fee: Decimal
    risk_price_distance: Decimal
    target_trigger_price: Decimal
    break_even_trigger_price: Decimal
    protection: CryptoProtectionState
    bars_held: int = 0


@dataclass(frozen=True)
class _ClosedTrade:
    symbol: str
    side: str
    decision_time: str
    entry_time: str
    exit_time: str
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    entry_notional_usdt: Decimal
    exit_notional_usdt: Decimal
    gross_pnl_usdt: Decimal
    fees_usdt: Decimal
    net_pnl_usdt: Decimal
    target_net_profit_usd: Decimal
    risk_budget_usdt: Decimal
    holding_bars: int
    exit_reason: str
    gap_through: bool
    ambiguous_intrabar_path: bool
    maximum_favorable_r_before_exit: Decimal
    maximum_adverse_r_before_exit: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "decision_time": self.decision_time,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "entry_price": float(self.entry_price),
            "exit_price": float(self.exit_price),
            "quantity": float(self.quantity),
            "entry_notional_usdt": float(self.entry_notional_usdt),
            "exit_notional_usdt": float(self.exit_notional_usdt),
            "gross_pnl_usdt": float(self.gross_pnl_usdt),
            "fees_usdt": float(self.fees_usdt),
            "net_pnl_usdt": float(self.net_pnl_usdt),
            "target_net_profit_usd": float(self.target_net_profit_usd),
            "risk_budget_usdt": float(self.risk_budget_usdt),
            "holding_bars": self.holding_bars,
            "exit_reason": self.exit_reason,
            "gap_through": self.gap_through,
            "ambiguous_intrabar_path": self.ambiguous_intrabar_path,
            "maximum_favorable_r_before_exit": float(self.maximum_favorable_r_before_exit),
            "maximum_adverse_r_before_exit": float(self.maximum_adverse_r_before_exit),
        }


def default_crypto_config() -> CryptoPerpStrategyConfig:
    """Research profile for a $1k account; not a live promotion configuration."""

    return replace(
        CryptoPerpStrategyConfig(),
        risk_fraction_per_trade=Decimal("0.01"),
        expected_move_atr_multiple=Decimal("3.0"),
    )


def replay_acquisition(
    acquisition: BybitKlineAcquisition,
    *,
    opening_equity_usdt: Decimal = Decimal("1000"),
    targets_usd: Sequence[Decimal] = _DEFAULT_TARGETS,
    base_config: CryptoPerpStrategyConfig | None = None,
    protection_policy: CryptoProtectionPolicy | None = None,
    interval: str = "5",
) -> dict[str, Any]:
    if opening_equity_usdt <= 0:
        raise ValueError("opening equity must be positive")
    if not targets_usd or any(target <= 0 for target in targets_usd):
        raise ValueError("replay targets must be positive")
    if len(set(targets_usd)) != len(targets_usd):
        raise ValueError("replay targets must be unique")
    config = default_crypto_config() if base_config is None else base_config
    policy = CryptoProtectionPolicy() if protection_policy is None else protection_policy
    config.validate()
    policy.validate()

    bars_by_symbol = _bars_by_symbol_and_time(acquisition.bars)
    common_times = _common_timestamps(bars_by_symbol)
    if len(common_times) < 3:
        raise ValueError("Bybit crypto replay requires synchronized timestamps")
    synchronized_bars = {
        symbol: tuple(rows[timestamp] for timestamp in common_times)
        for symbol, rows in bars_by_symbol.items()
    }
    synchronized = BybitKlineAcquisition(
        bars=tuple(
            bar
            for symbol in sorted(synchronized_bars)
            for bar in synchronized_bars[symbol]
        ),
        pages_by_symbol=dict(acquisition.pages_by_symbol),
    )

    variants: dict[str, Any] = {}
    for target in targets_usd:
        target_config = config.with_target(target)
        variants[f"TARGET_{_target_label(target)}_USD"] = _run_variant(
            synchronized_bars,
            common_times=common_times,
            opening_equity_usdt=opening_equity_usdt,
            config=target_config,
            protection_policy=policy,
            interval=interval,
        )

    return {
        "qualification": "PASS_CRYPTO_HISTORICAL_REPLAY",
        "evidence_scope": "BYBIT_PUBLIC_COMPLETED_KLINE_COUNTERFACTUAL_REPLAY",
        "source": "BYBIT_V5_PUBLIC_MAINNET_KLINE",
        "category": "linear",
        "interval": interval,
        "symbols": sorted(synchronized_bars),
        "opening_equity_usdt": float(opening_equity_usdt),
        "first_completed_bar": common_times[0].isoformat(),
        "last_completed_bar": common_times[-1].isoformat(),
        "synchronized_bar_count_per_symbol": len(common_times),
        "raw_bar_counts_by_symbol": acquisition.counts_by_symbol(),
        "pages_by_symbol": acquisition.pages_by_symbol,
        "strategy": _strategy_snapshot(config, policy),
        "targets_usd": [float(target) for target in targets_usd],
        "variants": variants,
        "frequency_is_quality_outcome": True,
        "hundreds_of_trades_per_day_targeted": False,
        "strategy_promotion_allowed": False,
        "bybit_demo_order_writes_enabled": False,
        "bybit_live_order_routing_allowed": False,
        "real_demo_fills": False,
        "funding_costs_modeled": False,
        "instrument_tick_and_qty_quantization_modeled": False,
        "liquidation_engine_modeled": False,
        "synchronized_acquisition": {
            "symbols": list(synchronized.symbols),
            "counts_by_symbol": synchronized.counts_by_symbol(),
        },
        "limitations": [
            "Historical OHLC replay cannot reconstruct true intrabar path; protective stop wins "
            "when stop and target are both touched in one bar.",
            "New break-even/profit-protection stops use only completed bars and become active on "
            "the next bar; they are never retroactively applied inside the arming bar.",
            "Configured taker fee is a conservative research assumption until the account-specific "
            "Bybit fee-rate endpoint is reconciled.",
            "Funding payments are not yet included, so longer-lived perpetual positions can differ "
            "from replay PnL.",
            "Instrument tickSize, qtyStep and minimum-notional quantization are not yet applied.",
            "Historical replay is research evidence, not demo fills and not permission for live "
            "mainnet routing.",
        ],
    }


def acquire_and_replay(
    *,
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS,
    interval: str = "5",
    lookback_days: int = 7,
    opening_equity_usdt: Decimal = Decimal("1000"),
    targets_usd: Sequence[Decimal] = _DEFAULT_TARGETS,
    now_ms: int | None = None,
    client: BybitPublicKlineClient | None = None,
) -> dict[str, Any]:
    if lookback_days < 1:
        raise ValueError("lookback_days must be positive")
    clock_ms = int(time.time() * 1000) if now_ms is None else now_ms
    end_ms = last_completed_kline_end_ms(now_ms=clock_ms, interval=interval)
    interval_ms = interval_milliseconds(interval)
    start_ms = end_ms - lookback_days * 24 * 60 * 60 * 1000 + interval_ms
    request = BybitKlineRequest(
        symbols=symbols,
        start_ms=start_ms,
        end_ms=end_ms,
        interval=interval,
        maximum_pages_per_symbol=max(4, lookback_days * 2),
    )
    market_client = BybitPublicKlineClient() if client is None else client
    acquisition = market_client.fetch(request)
    acquisition.validate(requested_symbols=symbols, minimum_bars=25)
    report = replay_acquisition(
        acquisition,
        opening_equity_usdt=opening_equity_usdt,
        targets_usd=targets_usd,
        interval=interval,
    )
    report["requested_start_ms"] = start_ms
    report["requested_end_ms"] = end_ms
    report["current_incomplete_bar_excluded"] = True
    return report


def _run_variant(
    bars_by_symbol: dict[str, tuple[BybitKlineBar, ...]],
    *,
    common_times: tuple[datetime, ...],
    opening_equity_usdt: Decimal,
    config: CryptoPerpStrategyConfig,
    protection_policy: CryptoProtectionPolicy,
    interval: str,
) -> dict[str, Any]:
    histories: dict[str, list[BybitKlineBar]] = {symbol: [] for symbol in bars_by_symbol}
    positions: dict[str, _Position] = {}
    pending_entries: list[_PendingEntry] = []
    pending_max_hold_exits: set[str] = set()
    cooldown_until_index: dict[str, int] = {}
    closed: list[_ClosedTrade] = []
    cash_equity = opening_equity_usdt
    peak_equity = opening_equity_usdt
    maximum_drawdown = Decimal("0")
    equity_curve: list[dict[str, Any]] = []
    reason_counts: Counter[str] = Counter()
    plan_block_counts: Counter[str] = Counter()
    signal_event_count = 0
    plan_event_count = 0
    maximum_concurrent = 0
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
            pending_max_hold_exits.discard(symbol)
            cooldown_until_index[symbol] = index + protection_policy.cooldown_bars_after_stop
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
            position = _open_position(
                pending,
                bar=current_bars[symbol],
                config=config,
            )
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
                    "planned_target_net_usd": float(position.plan.target_net_profit_usd),
                    "risk_budget_usdt": float(position.plan.risk_budget_usdt),
                }
            )

        for symbol, position in list(positions.items()):
            bar = current_bars[symbol]
            bar_exit = resolve_crypto_bar_exit(
                side=position.side,
                bar=bar,
                active_stop_price=position.protection.active_stop_price,
                active_stop_reason=position.protection.active_stop_reason,
                target_price=position.target_trigger_price,
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
            cooldown = (
                protection_policy.cooldown_bars_after_target
                if bar_exit.reason is CryptoExitReason.NET_TARGET
                else protection_policy.cooldown_bars_after_stop
            )
            cooldown_until_index[symbol] = index + cooldown
            decision_events.append(_exit_event(trade))

        for symbol, position in positions.items():
            position.bars_held += 1
            position.protection = update_protection_after_completed_bar(
                position.protection,
                side=position.side,
                entry_price=position.entry_price,
                risk_price_distance=position.risk_price_distance,
                break_even_price=position.break_even_trigger_price,
                completed_bar=current_bars[symbol],
                policy=protection_policy,
            )
            if position.bars_held >= protection_policy.maximum_holding_bars:
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
                        "event": "TARGET_EDGE_BLOCK",
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
                reason=CryptoExitReason.MAX_HOLD,
                config=config,
                gap_through=False,
                ambiguous=False,
            )
            cash_equity += cash_delta
            closed.append(trade)
            positions.pop(symbol)
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
    terminal = _terminal_snapshot(
        histories,
        final_equity=final_equity,
        config=config,
    )
    return {
        "target_net_profit_usd": float(config.target_net_profit_usd),
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
        "protective_stop_ambiguity_policy": "STOP_WINS_SAME_BAR_AMBIGUITY",
        "strategy_promotion_allowed": False,
        "bybit_demo_order_writes_enabled": False,
        "bybit_live_order_routing_allowed": False,
    }


def _open_position(
    pending: _PendingEntry,
    *,
    bar: BybitKlineBar,
    config: CryptoPerpStrategyConfig,
) -> _Position:
    entry_price = _entry_execution_price(bar.open, pending.plan.side, config)
    quantity = pending.plan.reference_quantity
    entry_fee = entry_price * quantity * config.taker_fee_rate
    risk_price_distance = entry_price * pending.plan.stop_fraction
    if pending.plan.side is CryptoSide.LONG:
        hard_stop = entry_price - risk_price_distance
    else:
        hard_stop = entry_price + risk_price_distance
    target = _raw_trigger_for_net_pnl(
        side=pending.plan.side,
        entry_price=entry_price,
        quantity=quantity,
        entry_fee=entry_fee,
        desired_net_pnl=pending.plan.target_net_profit_usd,
        config=config,
    )
    break_even = _raw_trigger_for_net_pnl(
        side=pending.plan.side,
        entry_price=entry_price,
        quantity=quantity,
        entry_fee=entry_fee,
        desired_net_pnl=Decimal("0"),
        config=config,
    )
    return _Position(
        plan=pending.plan,
        side=pending.plan.side,
        decision_time=pending.plan.decision_time,
        entry_time=bar.start_time.isoformat(),
        entry_price=entry_price,
        quantity=quantity,
        entry_fee=entry_fee,
        risk_price_distance=risk_price_distance,
        target_trigger_price=target,
        break_even_trigger_price=break_even,
        protection=initial_protection_state(
            side=pending.plan.side,
            entry_price=entry_price,
            hard_stop_price=hard_stop,
        ),
    )


def _close_from_bar_exit(
    position: _Position,
    *,
    bar_exit: CryptoBarExit,
    exit_time: datetime,
    config: CryptoPerpStrategyConfig,
) -> tuple[_ClosedTrade, Decimal]:
    return _close_position(
        position,
        raw_exit_price=bar_exit.trigger_price,
        exit_time=exit_time,
        reason=bar_exit.reason,
        config=config,
        gap_through=bar_exit.gap_through,
        ambiguous=bar_exit.ambiguous_intrabar_path,
    )


def _close_position(
    position: _Position,
    *,
    raw_exit_price: Decimal,
    exit_time: datetime,
    reason: CryptoExitReason,
    config: CryptoPerpStrategyConfig,
    gap_through: bool,
    ambiguous: bool,
) -> tuple[_ClosedTrade, Decimal]:
    exit_price = _exit_execution_price(raw_exit_price, position.side, config)
    exit_notional = exit_price * position.quantity
    exit_fee = exit_notional * config.taker_fee_rate
    if position.side is CryptoSide.LONG:
        gross = (exit_price - position.entry_price) * position.quantity
    else:
        gross = (position.entry_price - exit_price) * position.quantity
    net = gross - position.entry_fee - exit_fee
    entry_notional = position.entry_price * position.quantity
    trade = _ClosedTrade(
        symbol=position.plan.symbol,
        side=position.side.value,
        decision_time=position.decision_time,
        entry_time=position.entry_time,
        exit_time=exit_time.isoformat(),
        entry_price=position.entry_price,
        exit_price=exit_price,
        quantity=position.quantity,
        entry_notional_usdt=entry_notional,
        exit_notional_usdt=exit_notional,
        gross_pnl_usdt=gross,
        fees_usdt=position.entry_fee + exit_fee,
        net_pnl_usdt=net,
        target_net_profit_usd=position.plan.target_net_profit_usd,
        risk_budget_usdt=position.plan.risk_budget_usdt,
        holding_bars=position.bars_held,
        exit_reason=reason.value,
        gap_through=gap_through,
        ambiguous_intrabar_path=ambiguous,
        maximum_favorable_r_before_exit=position.protection.maximum_favorable_r,
        maximum_adverse_r_before_exit=position.protection.maximum_adverse_r,
    )
    cash_delta = gross - exit_fee
    return trade, cash_delta


def _raw_trigger_for_net_pnl(
    *,
    side: CryptoSide,
    entry_price: Decimal,
    quantity: Decimal,
    entry_fee: Decimal,
    desired_net_pnl: Decimal,
    config: CryptoPerpStrategyConfig,
) -> Decimal:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    fee = config.taker_fee_rate
    slippage = config.slippage_bps_per_fill / _BPS
    if side is CryptoSide.LONG:
        exit_execution = (
            desired_net_pnl + quantity * entry_price + entry_fee
        ) / (quantity * (Decimal("1") - fee))
        return exit_execution / (Decimal("1") - slippage)
    exit_execution = (
        quantity * entry_price - entry_fee - desired_net_pnl
    ) / (quantity * (Decimal("1") + fee))
    if exit_execution <= 0:
        raise ValueError("short target would require a non-positive exit price")
    return exit_execution / (Decimal("1") + slippage)


def _entry_execution_price(
    raw_price: Decimal,
    side: CryptoSide,
    config: CryptoPerpStrategyConfig,
) -> Decimal:
    slippage = config.slippage_bps_per_fill / _BPS
    if side is CryptoSide.LONG:
        return raw_price * (Decimal("1") + slippage)
    return raw_price * (Decimal("1") - slippage)


def _exit_execution_price(
    raw_price: Decimal,
    side: CryptoSide,
    config: CryptoPerpStrategyConfig,
) -> Decimal:
    slippage = config.slippage_bps_per_fill / _BPS
    if side is CryptoSide.LONG:
        return raw_price * (Decimal("1") - slippage)
    return raw_price * (Decimal("1") + slippage)


def _liquidation_equity(
    cash_equity: Decimal,
    positions: dict[str, _Position],
    current_bars: dict[str, BybitKlineBar],
    config: CryptoPerpStrategyConfig,
) -> Decimal:
    equity = cash_equity
    for symbol, position in positions.items():
        hypothetical_exit = _exit_execution_price(
            current_bars[symbol].close,
            position.side,
            config,
        )
        exit_fee = hypothetical_exit * position.quantity * config.taker_fee_rate
        if position.side is CryptoSide.LONG:
            gross = (hypothetical_exit - position.entry_price) * position.quantity
        else:
            gross = (position.entry_price - hypothetical_exit) * position.quantity
        equity += gross - exit_fee
    return equity


def _terminal_snapshot(
    histories: dict[str, list[BybitKlineBar]],
    *,
    final_equity: Decimal,
    config: CryptoPerpStrategyConfig,
) -> dict[str, Any]:
    rankings = rank_crypto_signals(histories, config)
    candidates = []
    for rank, evaluation in enumerate(rankings, start=1):
        payload: dict[str, Any] = {
            "rank": rank,
            "symbol": evaluation.symbol,
            "signal_eligible": evaluation.signal is not None,
            "signal_reasons": list(evaluation.reasons),
        }
        if evaluation.signal is not None:
            signal = evaluation.signal
            plan = build_trade_plan(signal, equity_usdt=final_equity, config=config)
            payload.update(
                side=signal.side.value,
                reference_price=float(signal.reference_price),
                quality_score=float(signal.quality_score),
                momentum=float(signal.momentum),
                atr_fraction=float(signal.atr_fraction),
                average_turnover_usdt=float(signal.average_turnover_usdt),
                target_edge_eligible=plan.eligible,
                target_edge_reasons=list(plan.reasons),
            )
            if plan.plan is not None:
                payload.update(
                    planned_notional_usdt=float(plan.plan.notional_usdt),
                    expected_net_edge_usd=float(plan.plan.expected_net_edge_usd),
                    risk_budget_usdt=float(plan.plan.risk_budget_usdt),
                )
        candidates.append(payload)
    latest_time = max(history[-1].start_time for history in histories.values())
    return {
        "decision_time": latest_time.isoformat(),
        "execution_pending": True,
        "execution_rule": "NEXT_5M_BAR_OPEN_AFTER_COMPLETED_SIGNAL",
        "completed_bar_selection_is_not_an_order": True,
        "candidates": candidates,
    }


def _metrics(
    trades: Sequence[_ClosedTrade],
    *,
    opening_equity: Decimal,
    final_equity: Decimal,
    maximum_drawdown: Decimal,
    first_time: datetime,
    last_time: datetime,
    interval: str,
    maximum_concurrent: int,
) -> dict[str, Any]:
    net_values = [trade.net_pnl_usdt for trade in trades]
    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value < 0]
    gross_profit = sum(wins, start=Decimal("0"))
    gross_loss = -sum(losses, start=Decimal("0"))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    duration_seconds = max((last_time - first_time).total_seconds(), 1.0)
    observed_days = Decimal(str(duration_seconds)) / Decimal("86400")
    observed_days = max(observed_days, Decimal(interval) / Decimal("1440"))
    turnover = sum(
        trade.entry_notional_usdt + trade.exit_notional_usdt for trade in trades
    )
    fee_total = sum((trade.fees_usdt for trade in trades), start=Decimal("0"))
    target_hits = sum(trade.exit_reason == CryptoExitReason.NET_TARGET.value for trade in trades)
    protected = sum(
        trade.exit_reason
        in {
            CryptoExitReason.BREAK_EVEN_STOP.value,
            CryptoExitReason.PROFIT_PROTECTION.value,
        }
        for trade in trades
    )
    hard_stops = sum(trade.exit_reason == CryptoExitReason.HARD_STOP.value for trade in trades)
    max_holds = sum(trade.exit_reason == CryptoExitReason.MAX_HOLD.value for trade in trades)
    target_or_better = sum(
        trade.net_pnl_usdt >= trade.target_net_profit_usd for trade in trades
    )
    risk_breaches = sum(
        trade.net_pnl_usdt < -trade.risk_budget_usdt * Decimal("1.05") for trade in trades
    )
    average = statistics.mean(net_values) if net_values else Decimal("0")
    median = statistics.median(net_values) if net_values else Decimal("0")
    average_holding = (
        statistics.mean([trade.holding_bars for trade in trades]) if trades else 0.0
    )
    return {
        "opening_equity_usdt": float(opening_equity),
        "final_equity_usdt": float(final_equity),
        "total_net_pnl_usdt": float(final_equity - opening_equity),
        "total_return_pct": float((final_equity / opening_equity - Decimal("1")) * 100),
        "closed_trade_count": len(trades),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": len(wins) / len(trades) if trades else 0.0,
        "profit_factor": float(profit_factor) if profit_factor is not None else None,
        "average_net_pnl_usdt": float(average),
        "median_net_pnl_usdt": float(median),
        "maximum_drawdown_pct": float(maximum_drawdown * 100),
        "trades_per_observed_day": float(Decimal(len(trades)) / observed_days),
        "turnover_usdt": float(turnover),
        "turnover_to_opening_equity": float(turnover / opening_equity),
        "fees_usdt": float(fee_total),
        "target_exit_count": target_hits,
        "realized_target_or_better_count": target_or_better,
        "protected_stop_count": protected,
        "hard_stop_count": hard_stops,
        "max_hold_exit_count": max_holds,
        "risk_budget_breach_count": risk_breaches,
        "gap_exit_count": sum(trade.gap_through for trade in trades),
        "ambiguous_intrabar_exit_count": sum(
            trade.ambiguous_intrabar_path for trade in trades
        ),
        "average_holding_bars": float(average_holding),
        "maximum_concurrent_positions": maximum_concurrent,
    }


def _bars_by_symbol_and_time(
    bars: Sequence[BybitKlineBar],
) -> dict[str, dict[datetime, BybitKlineBar]]:
    result: dict[str, dict[datetime, BybitKlineBar]] = {}
    for bar in bars:
        result.setdefault(bar.symbol, {})[bar.start_time] = bar
    return result


def _common_timestamps(
    bars_by_symbol: dict[str, dict[datetime, BybitKlineBar]],
) -> tuple[datetime, ...]:
    if len(bars_by_symbol) < 2:
        raise ValueError("crypto replay requires at least two symbols")
    timestamp_sets = [set(rows) for rows in bars_by_symbol.values()]
    common = set.intersection(*timestamp_sets)
    return tuple(sorted(common))


def _strategy_snapshot(
    config: CryptoPerpStrategyConfig,
    policy: CryptoProtectionPolicy,
) -> dict[str, Any]:
    return {
        "fast_ema_bars": config.fast_ema_bars,
        "slow_ema_bars": config.slow_ema_bars,
        "momentum_bars": config.momentum_bars,
        "breakout_bars": config.breakout_bars,
        "atr_bars": config.atr_bars,
        "minimum_average_turnover_usdt": float(config.minimum_average_turnover_usdt),
        "minimum_atr_fraction": float(config.minimum_atr_fraction),
        "maximum_atr_fraction": float(config.maximum_atr_fraction),
        "minimum_abs_momentum": float(config.minimum_abs_momentum),
        "minimum_quality_score": float(config.minimum_quality_score),
        "risk_fraction_per_trade": float(config.risk_fraction_per_trade),
        "maximum_notional_to_equity": float(config.maximum_notional_to_equity),
        "hard_stop_atr_multiple": float(config.hard_stop_atr_multiple),
        "expected_move_atr_multiple": float(config.expected_move_atr_multiple),
        "taker_fee_rate_assumption": float(config.taker_fee_rate),
        "slippage_bps_per_fill": float(config.slippage_bps_per_fill),
        "maximum_concurrent_positions": config.maximum_concurrent_positions,
        "break_even_activation_r": float(policy.break_even_activation_r),
        "profit_lock_activation_r": float(policy.profit_lock_activation_r),
        "profit_lock_r": float(policy.profit_lock_r),
        "maximum_holding_bars": policy.maximum_holding_bars,
    }


def _target_label(target: Decimal) -> str:
    return format(target.normalize(), "f").replace(".", "_")


def _exit_event(trade: _ClosedTrade) -> dict[str, Any]:
    return {
        "event": "EXIT",
        "symbol": trade.symbol,
        "side": trade.side,
        "execution_time": trade.exit_time,
        "exit_reason": trade.exit_reason,
        "net_pnl_usdt": float(trade.net_pnl_usdt),
        "gap_through": trade.gap_through,
        "ambiguous_intrabar_path": trade.ambiguous_intrabar_path,
    }


def write_trade_csvs(report: dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for variant, payload in report["variants"].items():
        trades = payload["closed_trades"]
        path = output_dir / f"{variant.lower()}-trades.csv"
        if not trades:
            path.write_text("", encoding="utf-8")
            continue
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(trades[0]))
            writer.writeheader()
            writer.writerows(trades)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay completed Bybit crypto signals")
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--interval", default="5")
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument("--targets", default="15,20,25")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trades-dir", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    symbols = tuple(symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip())
    targets = tuple(Decimal(value.strip()) for value in args.targets.split(",") if value.strip())
    report = acquire_and_replay(
        symbols=symbols,
        interval=args.interval,
        lookback_days=args.lookback_days,
        opening_equity_usdt=Decimal(args.opening_equity),
        targets_usd=targets,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.trades_dir is not None:
        write_trade_csvs(report, args.trades_dir)
    print(json.dumps({"qualification": report["qualification"], "output": str(args.output)}))


if __name__ == "__main__":
    main()
