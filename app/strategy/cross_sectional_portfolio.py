from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.domain.trading import Fill, Side
from app.marketdata.ohlcv import OhlcvBar
from app.portfolio.ledger import PortfolioLedger
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.ohlcv_exit import (
    IntrabarExitReason,
    IntrabarPositionState,
    evaluate_long_intrabar_exit,
)
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.position_sizing import RiskAwareSizingPolicy, size_position_from_risk
from app.strategy.reentry_confirmation import (
    ReentryConfirmationPolicy,
    ReentryConfirmationState,
    arm_after_exit,
    clear_after_entry,
    evaluate_reentry_confirmation,
)
from app.strategy.selection_exit_confirmation import (
    SelectionExitConfirmationPolicy,
    SelectionExitConfirmationState,
    evaluate_selection_exit_confirmation,
)


class PortfolioExitReason(StrEnum):
    SELECTION_EXIT = "SELECTION_EXIT"
    TIME_STOP = "TIME_STOP"
    INTRABAR_HARD_STOP = "INTRABAR_HARD_STOP"
    INTRABAR_BREAK_EVEN_STOP = "INTRABAR_BREAK_EVEN_STOP"
    INTRABAR_PROFIT_PROTECTION = "INTRABAR_PROFIT_PROTECTION"
    INTRABAR_TAKE_PROFIT = "INTRABAR_TAKE_PROFIT"
    INTRABAR_TRAILING_STOP = "INTRABAR_TRAILING_STOP"


class PortfolioEntryBlockReason(StrEnum):
    REENTRY_CONFIRMATION_PENDING = "REENTRY_CONFIRMATION_PENDING"
    GROSS_EXPOSURE_CAP = "GROSS_EXPOSURE_CAP"


@dataclass(frozen=True)
class CrossSectionalPortfolioPolicy:
    opening_cash: Decimal = Decimal("10000")
    fee_per_fill: Decimal = Decimal("0.50")
    slippage_bps: Decimal = Decimal("5")
    maximum_gross_exposure_fraction: Decimal = Decimal("0.60")
    new_position_target_equity_fraction: Decimal = Decimal("0.29")
    allow_leverage: bool = False
    rebalance_existing_positions: bool = False

    def validate(self, *, top_k: int) -> None:
        if not self.opening_cash.is_finite() or self.opening_cash <= 0:
            raise ValueError("opening_cash must be positive and finite")
        if not self.fee_per_fill.is_finite() or self.fee_per_fill < 0:
            raise ValueError("fee_per_fill must be non-negative and finite")
        if not self.slippage_bps.is_finite() or self.slippage_bps < 0:
            raise ValueError("slippage_bps must be non-negative and finite")
        for name, value in (
            ("maximum_gross_exposure_fraction", self.maximum_gross_exposure_fraction),
            (
                "new_position_target_equity_fraction",
                self.new_position_target_equity_fraction,
            ),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if not self.allow_leverage and self.maximum_gross_exposure_fraction > 1:
            raise ValueError("gross exposure cannot exceed 1 without leverage")
        if self.rebalance_existing_positions:
            raise ValueError("existing-position rebalancing is not qualified")
        if (
            self.new_position_target_equity_fraction * Decimal(top_k)
            > self.maximum_gross_exposure_fraction
        ):
            raise ValueError("top_k target allocation exceeds gross exposure cap")


@dataclass(frozen=True)
class PortfolioTrade:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_execution_price: Decimal
    exit_execution_price: Decimal
    quantity: Decimal
    net_pnl: Decimal
    holding_bars: int
    exit_reason: PortfolioExitReason
    maximum_favorable_excursion_fraction: Decimal = Decimal("0")
    maximum_adverse_excursion_fraction: Decimal = Decimal("0")
    mfe_capture_ratio: Decimal | None = None
    mfe_giveback_fraction: Decimal | None = None
    ambiguous_intrabar_exit: bool = False
    gap_through_stop: bool = False


@dataclass(frozen=True)
class PortfolioDecisionTrace:
    execution_index: int
    decision_time: datetime
    execution_time: datetime
    selected_symbols: tuple[str, ...]
    entered_symbols: tuple[str, ...]
    open_exit_symbols: tuple[str, ...]
    intrabar_exit_symbols: tuple[str, ...]
    blocked_entries: tuple[tuple[str, PortfolioEntryBlockReason], ...]
    equity_at_prior_close: Decimal
    closing_equity: Decimal
    closing_gross_exposure_fraction: Decimal
    concurrent_positions: int
    pending_selection_exit_symbols: tuple[str, ...] = ()


@dataclass(frozen=True)
class CrossSectionalPortfolioResult:
    fill_count: int
    closed_trade_count: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    average_maximum_favorable_excursion_fraction: Decimal
    average_maximum_adverse_excursion_fraction: Decimal
    average_mfe_capture_ratio: Decimal | None
    positive_mfe_trades: int
    positive_mfe_closed_profitable: int
    positive_mfe_closed_losing_or_flat: int
    profit_preservation_rate: Decimal | None
    total_pnl: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    max_drawdown_fraction: Decimal
    turnover_fraction: Decimal
    fees_paid: Decimal
    maximum_gross_exposure_fraction_observed: Decimal
    maximum_concurrent_positions: int
    one_bar_reentry_count: int
    selection_exit_confirmation_pending_count: int
    selection_counts: dict[str, int]
    realized_pnl_by_symbol: dict[str, Decimal]
    intrabar_exit_counts: dict[str, int]
    entry_block_counts: dict[str, int]
    final_quantities: dict[str, Decimal]
    closed_trades: tuple[PortfolioTrade, ...]
    decision_trace: tuple[PortfolioDecisionTrace, ...]


@dataclass(frozen=True)
class _OpenPositionState:
    entry_time: datetime
    entry_execution_index: int
    entry_execution_price: Decimal
    intrabar_state: IntrabarPositionState


class CrossSectionalPortfolioBacktester:
    """Synchronized long-only portfolio shadow backtest.

    Selection is computed strictly from completed prior bars. Deselect/time exits are
    processed at the next open before entries. New positions use either the declared
    equity fraction or an explicit stop-risk/inverse-volatility sizing policy and are
    admission-blocked when projected gross exposure would exceed the hard cap.
    Existing positions are not mechanically rebalanced. Intrabar protection applies
    to every surviving/new position using conservative OHLCV path assumptions.
    """

    def __init__(
        self,
        *,
        selector: CrossSectionalSelector,
        portfolio_policy: CrossSectionalPortfolioPolicy | None = None,
        position_policy: PositionManagementPolicy | None = None,
        reentry_policy: ReentryConfirmationPolicy | None = None,
        sizing_policy: RiskAwareSizingPolicy | None = None,
        selection_exit_policy: SelectionExitConfirmationPolicy | None = None,
    ) -> None:
        portfolio = (
            CrossSectionalPortfolioPolicy()
            if portfolio_policy is None
            else portfolio_policy
        )
        portfolio.validate(top_k=selector.top_k)
        position = (
            PositionManagementPolicy()
            if position_policy is None
            else position_policy
        )
        position.validate()
        if reentry_policy is not None:
            reentry_policy.validate()
        if sizing_policy is not None:
            sizing_policy.validate()
        if selection_exit_policy is not None:
            selection_exit_policy.validate()
        self.selector = selector
        self.portfolio_policy = portfolio
        self.position_policy = position
        self.reentry_policy = reentry_policy
        self.sizing_policy = sizing_policy
        self.selection_exit_policy = selection_exit_policy

    def run(
        self,
        bars: Iterable[OhlcvBar],
        *,
        first_execution_index: int | None = None,
    ) -> CrossSectionalPortfolioResult:
        by_symbol, timeline = _synchronized_universe(bars)
        symbols = tuple(sorted(by_symbol))
        if len(symbols) < 2:
            raise ValueError("portfolio backtest requires at least two symbols")
        required_history = self.selector.signal_config.minimum_history_bars
        first_execution = (
            required_history if first_execution_index is None else first_execution_index
        )
        if first_execution < required_history:
            raise ValueError("first execution cannot precede selector history")
        if first_execution >= len(timeline):
            raise ValueError("first execution must be inside synchronized timeline")

        ledger = PortfolioLedger(opening_cash=self.portfolio_policy.opening_cash)
        slip = self.portfolio_policy.slippage_bps / Decimal("10000")
        open_states: dict[str, _OpenPositionState] = {}
        reentry_states = {symbol: ReentryConfirmationState() for symbol in symbols}
        selection_exit_states = {
            symbol: SelectionExitConfirmationState() for symbol in symbols
        }
        last_exit_index: dict[str, int] = {}
        closed_trades: list[PortfolioTrade] = []
        trace: list[PortfolioDecisionTrace] = []
        selection_counts: Counter[str] = Counter()
        entry_block_counts: Counter[str] = Counter()
        realized_by_symbol: defaultdict[str, Decimal] = defaultdict(
            lambda: Decimal("0")
        )
        intrabar_exit_counts: Counter[str] = Counter()
        traded_notional = Decimal("0")
        fill_count = 0
        peak_equity = self.portfolio_policy.opening_cash
        max_drawdown = Decimal("0")
        max_gross_fraction = Decimal("0")
        max_positions = 0
        one_bar_reentries = 0
        selection_exit_confirmation_pending_count = 0

        for execution_index in range(first_execution, len(timeline)):
            execution_time = timeline[execution_index]
            decision_time = timeline[execution_index - 1]
            history = [
                bar
                for symbol in symbols
                for bar in by_symbol[symbol][:execution_index]
            ]
            selection = self.selector.select(history)
            if selection.decision_time != decision_time:
                raise RuntimeError("selector decision timestamp drifted")
            selected = tuple(selection.selected_symbols)
            selected_set = set(selected)
            candidates = {
                candidate.symbol: candidate for candidate in selection.candidates
            }
            selection_counts.update(selected)
            current_bars = {
                symbol: by_symbol[symbol][execution_index] for symbol in symbols
            }
            previous_closes = {
                symbol: by_symbol[symbol][execution_index - 1].close
                for symbol in symbols
            }
            equity_at_prior_close = ledger.equity(previous_closes)
            if equity_at_prior_close <= 0:
                raise ValueError("portfolio equity must remain positive")

            open_exit_symbols: list[str] = []
            intrabar_exit_symbols: list[str] = []
            entered_symbols: list[str] = []
            pending_selection_exit_symbols: list[str] = []
            blocked_entries: list[tuple[str, PortfolioEntryBlockReason]] = []
            exited_today: set[str] = set()

            for symbol in symbols:
                position = ledger.position(symbol)
                if position.quantity <= 0:
                    continue
                state = open_states.get(symbol)
                if state is None:
                    raise RuntimeError("open portfolio position missing tracking state")
                holding_bars = execution_index - state.entry_execution_index
                reason: PortfolioExitReason | None = None
                if self.selection_exit_policy is None:
                    if symbol not in selected_set:
                        reason = PortfolioExitReason.SELECTION_EXIT
                    elif holding_bars >= self.position_policy.maximum_holding_bars:
                        reason = PortfolioExitReason.TIME_STOP
                else:
                    if holding_bars >= self.position_policy.maximum_holding_bars:
                        reason = PortfolioExitReason.TIME_STOP
                    else:
                        selection_exit = evaluate_selection_exit_confirmation(
                            selected=symbol in selected_set,
                            profitable_at_decision=(
                                previous_closes[symbol] > position.average_cost
                            ),
                            state=selection_exit_states[symbol],
                            policy=self.selection_exit_policy,
                        )
                        selection_exit_states[symbol] = selection_exit.state
                        if selection_exit.allow_selection_exit:
                            reason = PortfolioExitReason.SELECTION_EXIT
                        elif symbol not in selected_set:
                            pending_selection_exit_symbols.append(symbol)
                            selection_exit_confirmation_pending_count += 1
                if reason is None:
                    continue
                exit_price = current_bars[symbol].open * (Decimal("1") - slip)
                fill_count += 1
                _sell(
                    ledger=ledger,
                    fill_count=fill_count,
                    execution_index=execution_index,
                    bar=current_bars[symbol],
                    quantity=position.quantity,
                    price=exit_price,
                    fee=self.portfolio_policy.fee_per_fill,
                )
                traded_notional += position.quantity * exit_price
                trade = _closed_trade(
                    symbol=symbol,
                    state=state,
                    exit_time=execution_time,
                    exit_price=exit_price,
                    quantity=position.quantity,
                    average_cost=position.average_cost,
                    exit_fee=self.portfolio_policy.fee_per_fill,
                    execution_index=execution_index,
                    reason=reason,
                )
                closed_trades.append(trade)
                realized_by_symbol[symbol] += trade.net_pnl
                open_states.pop(symbol)
                selection_exit_states[symbol] = SelectionExitConfirmationState()
                last_exit_index[symbol] = execution_index
                exited_today.add(symbol)
                open_exit_symbols.append(symbol)
                if self.reentry_policy is not None:
                    reentry_states[symbol] = arm_after_exit(policy=self.reentry_policy)

            for symbol in symbols:
                if symbol in exited_today or ledger.position(symbol).quantity > 0:
                    continue
                if self.reentry_policy is None:
                    continue
                decision = evaluate_reentry_confirmation(
                    signal_eligible=symbol in selected_set,
                    state=reentry_states[symbol],
                    policy=self.reentry_policy,
                )
                reentry_states[symbol] = decision.state
                if symbol in selected_set and not decision.allow_entry:
                    blocked_entries.append(
                        (symbol, PortfolioEntryBlockReason.REENTRY_CONFIRMATION_PENDING)
                    )
                    entry_block_counts[
                        PortfolioEntryBlockReason.REENTRY_CONFIRMATION_PENDING.value
                    ] += 1

            for symbol in selected:
                if symbol in exited_today or ledger.position(symbol).quantity > 0:
                    continue
                if self.reentry_policy is not None:
                    state = reentry_states[symbol]
                    if state.blocked_after_exit:
                        continue

                if self.sizing_policy is None:
                    target_notional = (
                        equity_at_prior_close
                        * self.portfolio_policy.new_position_target_equity_fraction
                    )
                else:
                    candidate = candidates[symbol]
                    sizing = size_position_from_risk(
                        equity=equity_at_prior_close,
                        realized_volatility=candidate.realized_volatility,
                        stop_loss_fraction=self.position_policy.stop_loss_fraction,
                        policy=self.sizing_policy,
                    )
                    target_notional = sizing.target_notional

                open_prices = {
                    item: current_bars[item].open for item in symbols
                }
                current_gross = ledger.gross_notional(open_prices)
                projected_gross = current_gross + target_notional
                cap = (
                    equity_at_prior_close
                    * self.portfolio_policy.maximum_gross_exposure_fraction
                )
                if projected_gross > cap:
                    blocked_entries.append(
                        (symbol, PortfolioEntryBlockReason.GROSS_EXPOSURE_CAP)
                    )
                    entry_block_counts[
                        PortfolioEntryBlockReason.GROSS_EXPOSURE_CAP.value
                    ] += 1
                    continue
                entry_price = current_bars[symbol].open * (Decimal("1") + slip)
                quantity = target_notional / entry_price
                required_cash = target_notional + self.portfolio_policy.fee_per_fill
                if ledger.cash < required_cash:
                    raise ValueError(
                        "portfolio entry requires cash beyond available balance"
                    )
                if last_exit_index.get(symbol) == execution_index - 1:
                    one_bar_reentries += 1
                fill_count += 1
                _buy(
                    ledger=ledger,
                    fill_count=fill_count,
                    execution_index=execution_index,
                    bar=current_bars[symbol],
                    quantity=quantity,
                    price=entry_price,
                    fee=self.portfolio_policy.fee_per_fill,
                )
                traded_notional += quantity * entry_price
                entered_symbols.append(symbol)
                selection_exit_states[symbol] = SelectionExitConfirmationState()
                open_states[symbol] = _OpenPositionState(
                    entry_time=execution_time,
                    entry_execution_index=execution_index,
                    entry_execution_price=entry_price,
                    intrabar_state=IntrabarPositionState(
                        peak_completed_price=current_bars[symbol].open,
                        trough_completed_price=current_bars[symbol].open,
                    ),
                )
                if self.reentry_policy is not None:
                    reentry_states[symbol] = clear_after_entry()

            for symbol in tuple(sorted(open_states)):
                position = ledger.position(symbol)
                if position.quantity <= 0:
                    raise RuntimeError(
                        "portfolio tracking survived closed ledger position"
                    )
                state = open_states[symbol]
                intrabar = evaluate_long_intrabar_exit(
                    average_cost=position.average_cost,
                    bar=current_bars[symbol],
                    state=state.intrabar_state,
                    policy=self.position_policy,
                )
                if not intrabar.exit_now:
                    open_states[symbol] = _OpenPositionState(
                        entry_time=state.entry_time,
                        entry_execution_index=state.entry_execution_index,
                        entry_execution_price=state.entry_execution_price,
                        intrabar_state=intrabar.state,
                    )
                    continue
                if (
                    intrabar.exit_price_before_costs is None
                    or intrabar.reason is None
                ):
                    raise RuntimeError(
                        "portfolio intrabar exit missing reason or price"
                    )
                exit_price = intrabar.exit_price_before_costs * (Decimal("1") - slip)
                fill_count += 1
                _sell(
                    ledger=ledger,
                    fill_count=fill_count,
                    execution_index=execution_index,
                    bar=current_bars[symbol],
                    quantity=position.quantity,
                    price=exit_price,
                    fee=self.portfolio_policy.fee_per_fill,
                )
                traded_notional += position.quantity * exit_price
                reason = _map_intrabar_reason(intrabar.reason)
                trade = _closed_trade(
                    symbol=symbol,
                    state=state,
                    exit_time=execution_time,
                    exit_price=exit_price,
                    quantity=position.quantity,
                    average_cost=position.average_cost,
                    exit_fee=self.portfolio_policy.fee_per_fill,
                    execution_index=execution_index,
                    reason=reason,
                    ambiguous=intrabar.ambiguous_bar,
                    gap=intrabar.gap_through_protective_stop,
                )
                closed_trades.append(trade)
                realized_by_symbol[symbol] += trade.net_pnl
                intrabar_exit_counts[reason.value] += 1
                intrabar_exit_symbols.append(symbol)
                open_states.pop(symbol)
                selection_exit_states[symbol] = SelectionExitConfirmationState()
                last_exit_index[symbol] = execution_index
                exited_today.add(symbol)
                if self.reentry_policy is not None:
                    reentry_states[symbol] = arm_after_exit(policy=self.reentry_policy)

            closing_prices = {
                symbol: current_bars[symbol].close for symbol in symbols
            }
            snapshot = ledger.snapshot(closing_prices)
            if snapshot.equity <= 0:
                raise ValueError("portfolio closing equity must remain positive")
            gross_fraction = snapshot.gross_notional / snapshot.equity
            max_gross_fraction = max(max_gross_fraction, gross_fraction)
            concurrent = sum(
                ledger.position(symbol).quantity > 0 for symbol in symbols
            )
            max_positions = max(max_positions, concurrent)
            peak_equity = max(peak_equity, snapshot.equity)
            max_drawdown = max(max_drawdown, peak_equity - snapshot.equity)
            trace.append(
                PortfolioDecisionTrace(
                    execution_index=execution_index,
                    decision_time=decision_time,
                    execution_time=execution_time,
                    selected_symbols=selected,
                    entered_symbols=tuple(entered_symbols),
                    open_exit_symbols=tuple(open_exit_symbols),
                    intrabar_exit_symbols=tuple(intrabar_exit_symbols),
                    blocked_entries=tuple(blocked_entries),
                    equity_at_prior_close=equity_at_prior_close,
                    closing_equity=snapshot.equity,
                    closing_gross_exposure_fraction=gross_fraction,
                    concurrent_positions=concurrent,
                    pending_selection_exit_symbols=tuple(
                        pending_selection_exit_symbols
                    ),
                )
            )

        last_prices = {symbol: by_symbol[symbol][-1].close for symbol in symbols}
        snapshot = ledger.snapshot(last_prices)
        wins = sum(trade.net_pnl > 0 for trade in closed_trades)
        losses = sum(trade.net_pnl < 0 for trade in closed_trades)
        breakeven = len(closed_trades) - wins - losses
        closed_count = len(closed_trades)
        win_rate = (
            Decimal(wins) / Decimal(closed_count)
            if closed_count
            else Decimal("0")
        )
        gross_profit = sum(
            (trade.net_pnl for trade in closed_trades if trade.net_pnl > 0),
            Decimal("0"),
        )
        gross_loss = sum(
            (trade.net_pnl for trade in closed_trades if trade.net_pnl < 0),
            Decimal("0"),
        )
        profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else None
        capture_ratios = [
            trade.mfe_capture_ratio
            for trade in closed_trades
            if trade.mfe_capture_ratio is not None
        ]
        positive_mfe = [
            trade
            for trade in closed_trades
            if trade.maximum_favorable_excursion_fraction > 0
        ]
        preserved_mfe = sum(trade.net_pnl > 0 for trade in positive_mfe)
        preservation_rate = (
            Decimal(preserved_mfe) / Decimal(len(positive_mfe))
            if positive_mfe
            else None
        )
        return CrossSectionalPortfolioResult(
            fill_count=fill_count,
            closed_trade_count=closed_count,
            winning_trades=wins,
            losing_trades=losses,
            breakeven_trades=breakeven,
            win_rate=win_rate,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            profit_factor=profit_factor,
            average_maximum_favorable_excursion_fraction=_mean(
                [trade.maximum_favorable_excursion_fraction for trade in closed_trades]
            ),
            average_maximum_adverse_excursion_fraction=_mean(
                [trade.maximum_adverse_excursion_fraction for trade in closed_trades]
            ),
            average_mfe_capture_ratio=(
                _mean([value for value in capture_ratios if value is not None])
                if capture_ratios
                else None
            ),
            positive_mfe_trades=len(positive_mfe),
            positive_mfe_closed_profitable=preserved_mfe,
            positive_mfe_closed_losing_or_flat=len(positive_mfe) - preserved_mfe,
            profit_preservation_rate=preservation_rate,
            total_pnl=snapshot.total_pnl,
            total_return=snapshot.total_pnl / self.portfolio_policy.opening_cash,
            max_drawdown=max_drawdown,
            max_drawdown_fraction=max_drawdown / self.portfolio_policy.opening_cash,
            turnover_fraction=traded_notional / self.portfolio_policy.opening_cash,
            fees_paid=snapshot.fees_paid,
            maximum_gross_exposure_fraction_observed=max_gross_fraction,
            maximum_concurrent_positions=max_positions,
            one_bar_reentry_count=one_bar_reentries,
            selection_exit_confirmation_pending_count=(
                selection_exit_confirmation_pending_count
            ),
            selection_counts=dict(sorted(selection_counts.items())),
            realized_pnl_by_symbol=dict(sorted(realized_by_symbol.items())),
            intrabar_exit_counts=dict(sorted(intrabar_exit_counts.items())),
            entry_block_counts=dict(sorted(entry_block_counts.items())),
            final_quantities={
                symbol: ledger.position(symbol).quantity for symbol in symbols
            },
            closed_trades=tuple(closed_trades),
            decision_trace=tuple(trace),
        )


def _synchronized_universe(
    bars: Iterable[OhlcvBar],
) -> tuple[dict[str, list[OhlcvBar]], tuple[datetime, ...]]:
    grouped: defaultdict[str, list[OhlcvBar]] = defaultdict(list)
    for bar in bars:
        bar.validate()
        grouped[bar.symbol].append(bar)
    if not grouped:
        raise ValueError("portfolio universe is empty")
    timestamp_sets: list[set[datetime]] = []
    for symbol, values in grouped.items():
        ordered = sorted(values, key=lambda bar: bar.timestamp)
        if len({bar.timestamp for bar in ordered}) != len(ordered):
            raise ValueError(f"duplicate portfolio timestamp:{symbol}")
        grouped[symbol] = ordered
        timestamp_sets.append({bar.timestamp for bar in ordered})
    first = timestamp_sets[0]
    if any(timestamps != first for timestamps in timestamp_sets[1:]):
        raise ValueError("portfolio universe must have synchronized timestamps")
    timeline = tuple(sorted(first))
    if len(timeline) < 2:
        raise ValueError("portfolio universe requires at least two timestamps")
    return dict(grouped), timeline


def _map_intrabar_reason(reason: IntrabarExitReason) -> PortfolioExitReason:
    return {
        IntrabarExitReason.HARD_STOP: PortfolioExitReason.INTRABAR_HARD_STOP,
        IntrabarExitReason.BREAK_EVEN_STOP: PortfolioExitReason.INTRABAR_BREAK_EVEN_STOP,
        IntrabarExitReason.PROFIT_PROTECTION: (
            PortfolioExitReason.INTRABAR_PROFIT_PROTECTION
        ),
        IntrabarExitReason.TAKE_PROFIT: PortfolioExitReason.INTRABAR_TAKE_PROFIT,
        IntrabarExitReason.TRAILING_STOP: PortfolioExitReason.INTRABAR_TRAILING_STOP,
    }[reason]


def _closed_trade(
    *,
    symbol: str,
    state: _OpenPositionState,
    exit_time: datetime,
    exit_price: Decimal,
    quantity: Decimal,
    average_cost: Decimal,
    exit_fee: Decimal,
    execution_index: int,
    reason: PortfolioExitReason,
    ambiguous: bool = False,
    gap: bool = False,
) -> PortfolioTrade:
    net_pnl = (exit_price - average_cost) * quantity - exit_fee
    prior_trough = (
        state.entry_execution_price
        if state.intrabar_state.trough_completed_price is None
        else state.intrabar_state.trough_completed_price
    )
    peak = max(state.intrabar_state.peak_completed_price, exit_price)
    trough = min(prior_trough, exit_price)
    mfe_fraction = max(Decimal("0"), (peak - average_cost) / average_cost)
    mae_fraction = max(Decimal("0"), (average_cost - trough) / average_cost)
    maximum_favorable_pnl = max(
        Decimal("0"),
        (peak - average_cost) * quantity,
    )
    capture_ratio = (
        net_pnl / maximum_favorable_pnl if maximum_favorable_pnl > 0 else None
    )
    giveback_fraction = (
        (maximum_favorable_pnl - net_pnl) / maximum_favorable_pnl
        if maximum_favorable_pnl > 0
        else None
    )
    return PortfolioTrade(
        symbol=symbol,
        entry_time=state.entry_time,
        exit_time=exit_time,
        entry_execution_price=state.entry_execution_price,
        exit_execution_price=exit_price,
        quantity=quantity,
        net_pnl=net_pnl,
        holding_bars=execution_index - state.entry_execution_index,
        exit_reason=reason,
        maximum_favorable_excursion_fraction=mfe_fraction,
        maximum_adverse_excursion_fraction=mae_fraction,
        mfe_capture_ratio=capture_ratio,
        mfe_giveback_fraction=giveback_fraction,
        ambiguous_intrabar_exit=ambiguous,
        gap_through_stop=gap,
    )


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _buy(
    *,
    ledger: PortfolioLedger,
    fill_count: int,
    execution_index: int,
    bar: OhlcvBar,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
) -> None:
    ledger.apply_fill(
        Fill(
            fill_id=f"portfolio-{fill_count}-{bar.symbol}-{bar.timestamp.isoformat()}",
            order_intent_id=f"portfolio-{execution_index}-{bar.symbol}-buy",
            symbol=bar.symbol,
            side=Side.BUY,
            quantity=quantity,
            price=price,
            occurred_at=bar.timestamp,
            fee=fee,
        )
    )


def _sell(
    *,
    ledger: PortfolioLedger,
    fill_count: int,
    execution_index: int,
    bar: OhlcvBar,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
) -> None:
    ledger.apply_fill(
        Fill(
            fill_id=f"portfolio-{fill_count}-{bar.symbol}-{bar.timestamp.isoformat()}",
            order_intent_id=f"portfolio-{execution_index}-{bar.symbol}-sell",
            symbol=bar.symbol,
            side=Side.SELL,
            quantity=quantity,
            price=price,
            occurred_at=bar.timestamp,
            fee=fee,
        )
    )