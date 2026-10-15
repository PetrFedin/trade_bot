from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.domain.trading import Fill, Side
from app.marketdata.ohlcv import OhlcvBar
from app.portfolio.ledger import CashAdjustmentKind, PortfolioLedger
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.ohlcv_exit import (
    IntrabarExitDecision,
    IntrabarExitReason,
    IntrabarPositionState,
    evaluate_long_intrabar_exit,
)
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.reentry_confirmation import (
    ReentryConfirmationPolicy,
    ReentryConfirmationState,
    arm_after_exit,
    clear_after_entry,
    evaluate_reentry_confirmation,
)


class PortfolioExitReason(StrEnum):
    SELECTION_EXIT = "SELECTION_EXIT"
    TIME_STOP = "TIME_STOP"
    INTRABAR_HARD_STOP = "INTRABAR_HARD_STOP"
    INTRABAR_TAKE_PROFIT = "INTRABAR_TAKE_PROFIT"
    INTRABAR_TRAILING_STOP = "INTRABAR_TRAILING_STOP"


class PortfolioEntryBlockReason(StrEnum):
    REENTRY_CONFIRMATION_PENDING = "REENTRY_CONFIRMATION_PENDING"
    GROSS_EXPOSURE_CAP = "GROSS_EXPOSURE_CAP"


@dataclass(frozen=True)
class CrossSectionalPortfolioPolicy:
    opening_cash: Decimal = Decimal("10000")
    fee_per_fill: Decimal = Decimal("0.50")
    fee_bps_per_fill: Decimal = Decimal("0")
    slippage_bps: Decimal = Decimal("5")
    maximum_gross_exposure_fraction: Decimal = Decimal("0.60")
    new_position_target_equity_fraction: Decimal = Decimal("0.30")
    allow_leverage: bool = False
    rebalance_existing_positions: bool = False

    def validate(self, *, top_k: int) -> None:
        if not self.opening_cash.is_finite() or self.opening_cash <= 0:
            raise ValueError("opening_cash must be positive and finite")
        if not self.fee_per_fill.is_finite() or self.fee_per_fill < 0:
            raise ValueError("fee_per_fill must be non-negative and finite")
        if not self.fee_bps_per_fill.is_finite() or self.fee_bps_per_fill < 0:
            raise ValueError("fee_bps_per_fill must be non-negative and finite")
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
    entry_reference_price: Decimal
    exit_reference_price: Decimal
    entry_execution_price: Decimal
    exit_execution_price: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    slippage_cost: Decimal
    gross_pnl_before_costs: Decimal
    quantity: Decimal
    net_pnl: Decimal
    holding_bars: int
    exit_reason: PortfolioExitReason
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


@dataclass(frozen=True)
class CrossSectionalPortfolioResult:
    opening_cash: Decimal
    ending_equity: Decimal
    fill_count: int
    closed_trade_count: int
    winning_trades: int
    losing_trades: int
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    total_pnl: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    max_drawdown_fraction: Decimal
    turnover_fraction: Decimal
    fees_paid: Decimal
    maximum_gross_exposure_fraction_observed: Decimal
    maximum_concurrent_positions: int
    one_bar_reentry_count: int
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
    entry_reference_price: Decimal
    entry_execution_price: Decimal
    entry_fee: Decimal
    intrabar_state: IntrabarPositionState


@dataclass(frozen=True)
class ResearchFundingPosition:
    symbol: str
    entry_time: datetime
    quantity: Decimal
    entry_execution_price: Decimal


@dataclass(frozen=True)
class ResearchDailyFundingContext:
    """End-of-day facts only; funding affects the NEXT daily entry decision.

    No mutable ledger or later-market bars are exposed to the calculator.
    This is a daily cash-feedback scenario, not venue-timestamp settlement.
    """

    day_utc: datetime
    closed_today: tuple[PortfolioTrade, ...]
    open_positions: tuple[ResearchFundingPosition, ...]


@dataclass(frozen=True)
class ResearchFundingCashflow:
    activity_id: str
    symbol: str
    settlement_time: datetime
    amount: Decimal


@dataclass(frozen=True)
class ResearchCollisionContext:
    """Exit-only research context; no ledger, future days or order authority."""

    average_cost: Decimal
    bar: OhlcvBar
    state: IntrabarPositionState
    policy: PositionManagementPolicy
    daily_decision: IntrabarExitDecision


def _validated_collision_refinement(
    context: ResearchCollisionContext, refined: IntrabarExitDecision,
) -> IntrabarExitDecision:
    original = context.daily_decision
    if not original.ambiguous_bar or not original.exit_now:
        raise ValueError("only a reached daily collision may be refined")
    if not isinstance(refined, IntrabarExitDecision) or refined.exit_now is not True:
        raise ValueError("collision refinement cannot cancel a protective exit")
    if not isinstance(refined.reason, IntrabarExitReason):
        raise ValueError("collision refinement requires a typed exit reason")
    if not isinstance(refined.ambiguous_bar, bool) or not isinstance(
        refined.gap_through_protective_stop, bool
    ):
        raise ValueError("collision uncertainty flags must be booleans")
    for field in ("hard_stop_price", "take_profit_price", "trailing_stop_price", "state"):
        if getattr(refined, field) != getattr(original, field):
            raise ValueError("collision refinement cannot change protection or prior state")
    price = refined.exit_price_before_costs
    if not isinstance(price, Decimal) or not price.is_finite():
        raise ValueError("collision refinement price must be finite Decimal")
    if not context.bar.low <= price <= context.bar.high:
        raise ValueError("collision refinement price outside source daily range")
    protective = max(original.hard_stop_price, original.trailing_stop_price or Decimal("0"))
    if refined.reason is IntrabarExitReason.TAKE_PROFIT:
        if price != original.take_profit_price or refined.gap_through_protective_stop:
            raise ValueError("target execution cannot invent favorable price improvement")
        if refined.ambiguous_bar:
            raise ValueError("unresolved collision must never be promoted to target first")
    elif refined.reason != original.reason or price > protective:
        raise ValueError("collision refinement cannot change protective reason or improve stop")
    elif refined.gap_through_protective_stop != (price < protective):
        raise ValueError("protective gap flag must agree with observed execution price")
    return refined


class CrossSectionalPortfolioBacktester:
    """Synchronized long-only portfolio shadow backtest.

    Selection is computed strictly from completed prior bars. Deselect/time exits are
    processed at the next open before entries. New positions use a fixed fraction of
    prior-close equity and are admission-blocked when projected gross exposure would
    exceed the configured cap. Existing positions are not mechanically rebalanced.
    Intrabar protection then applies to every surviving/new position using OHLCV.
    """

    def __init__(
        self,
        *,
        selector: CrossSectionalSelector,
        portfolio_policy: CrossSectionalPortfolioPolicy | None = None,
        position_policy: PositionManagementPolicy | None = None,
        reentry_policy: ReentryConfirmationPolicy | None = None,
    ) -> None:
        portfolio = (
            CrossSectionalPortfolioPolicy()
            if portfolio_policy is None
            else portfolio_policy
        )
        portfolio.validate(top_k=selector.top_k)
        position = PositionManagementPolicy() if position_policy is None else position_policy
        position.validate()
        if reentry_policy is not None:
            reentry_policy.validate()
        self.selector = selector
        self.portfolio_policy = portfolio
        self.position_policy = position
        self.reentry_policy = reentry_policy

    def run(
        self,
        bars: Iterable[OhlcvBar],
        *,
        first_execution_index: int | None = None,
        research_collision_resolver: (
            Callable[[ResearchCollisionContext], IntrabarExitDecision] | None
        ) = None,
        research_daily_funding: (
            Callable[[ResearchDailyFundingContext], tuple[ResearchFundingCashflow, ...]] | None
        ) = None,
    ) -> CrossSectionalPortfolioResult:
        if research_daily_funding is not None and not callable(research_daily_funding):
            raise ValueError("research daily funding must be callable")
        if research_collision_resolver is not None and not callable(research_collision_resolver):
            raise ValueError("research collision resolver must be callable")
        by_symbol, timeline = _synchronized_universe(bars)
        if research_daily_funding is not None:
            for i, timestamp in enumerate(timeline):
                if (
                    timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0)
                    or (timestamp.hour, timestamp.minute, timestamp.second,
                        timestamp.microsecond) != (0, 0, 0, 0)
                    or (i and timestamp - timeline[i - 1] != timedelta(days=1))
                ):
                    raise ValueError("daily funding requires contiguous UTC daily bars")
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
        last_exit_index: dict[str, int] = {}
        closed_trades: list[PortfolioTrade] = []
        trace: list[PortfolioDecisionTrace] = []
        selection_counts: Counter[str] = Counter()
        entry_block_counts: Counter[str] = Counter()
        realized_by_symbol: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        intrabar_exit_counts: Counter[str] = Counter()
        traded_notional = Decimal("0")
        fill_count = 0
        peak_equity = self.portfolio_policy.opening_cash
        max_drawdown = Decimal("0")
        max_gross_fraction = Decimal("0")
        max_positions = 0
        one_bar_reentries = 0
        funding_activity_ids: set[str] = set()

        for execution_index in range(first_execution, len(timeline)):
            execution_time = timeline[execution_index]
            closed_before_today = len(closed_trades)
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
            selection_counts.update(selected)
            current_bars = {
                symbol: by_symbol[symbol][execution_index] for symbol in symbols
            }
            previous_closes = {
                symbol: by_symbol[symbol][execution_index - 1].close for symbol in symbols
            }
            equity_at_prior_close = ledger.equity(previous_closes)
            if equity_at_prior_close <= 0:
                raise ValueError("portfolio equity must remain positive")

            open_exit_symbols: list[str] = []
            intrabar_exit_symbols: list[str] = []
            entered_symbols: list[str] = []
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
                if symbol not in selected_set:
                    reason = PortfolioExitReason.SELECTION_EXIT
                elif holding_bars >= self.position_policy.maximum_holding_bars:
                    reason = PortfolioExitReason.TIME_STOP
                if reason is None:
                    continue
                exit_price = current_bars[symbol].open * (Decimal("1") - slip)
                exit_fee = _fill_fee(
                    notional=position.quantity * exit_price,
                    policy=self.portfolio_policy,
                )
                fill_count += 1
                _sell(
                    ledger=ledger,
                    fill_count=fill_count,
                    execution_index=execution_index,
                    bar=current_bars[symbol],
                    quantity=position.quantity,
                    price=exit_price,
                    fee=exit_fee,
                )
                traded_notional += position.quantity * exit_price
                trade = _closed_trade(
                    symbol=symbol,
                    state=state,
                    exit_time=execution_time,
                    exit_reference_price=current_bars[symbol].open,
                    exit_price=exit_price,
                    quantity=position.quantity,
                    average_cost=position.average_cost,
                    exit_fee=exit_fee,
                    execution_index=execution_index,
                    reason=reason,
                )
                closed_trades.append(trade)
                realized_by_symbol[symbol] += trade.net_pnl
                open_states.pop(symbol)
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
                target_notional = (
                    equity_at_prior_close
                    * self.portfolio_policy.new_position_target_equity_fraction
                )
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
                entry_fee = _fill_fee(
                    notional=quantity * entry_price,
                    policy=self.portfolio_policy,
                )
                required_cash = target_notional + entry_fee
                if ledger.cash < required_cash:
                    raise ValueError("portfolio entry requires cash beyond available balance")
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
                    fee=entry_fee,
                )
                traded_notional += quantity * entry_price
                entered_symbols.append(symbol)
                open_states[symbol] = _OpenPositionState(
                    entry_time=execution_time,
                    entry_execution_index=execution_index,
                    entry_reference_price=current_bars[symbol].open,
                    entry_execution_price=entry_price,
                    entry_fee=entry_fee,
                    intrabar_state=IntrabarPositionState(
                        peak_completed_price=current_bars[symbol].open
                    ),
                )
                if self.reentry_policy is not None:
                    reentry_states[symbol] = clear_after_entry()

            for symbol in tuple(sorted(open_states)):
                position = ledger.position(symbol)
                if position.quantity <= 0:
                    raise RuntimeError("portfolio tracking survived closed ledger position")
                state = open_states[symbol]
                intrabar = evaluate_long_intrabar_exit(
                    average_cost=position.average_cost,
                    bar=current_bars[symbol],
                    state=state.intrabar_state,
                    policy=self.position_policy,
                )
                if intrabar.ambiguous_bar and research_collision_resolver is not None:
                    context = ResearchCollisionContext(
                        average_cost=position.average_cost,
                        bar=current_bars[symbol],
                        state=state.intrabar_state,
                        policy=self.position_policy,
                        daily_decision=intrabar,
                    )
                    intrabar = _validated_collision_refinement(
                        context, research_collision_resolver(context),
                    )
                if not intrabar.exit_now:
                    open_states[symbol] = _OpenPositionState(
                        entry_time=state.entry_time,
                        entry_execution_index=state.entry_execution_index,
                        entry_reference_price=state.entry_reference_price,
                        entry_execution_price=state.entry_execution_price,
                        entry_fee=state.entry_fee,
                        intrabar_state=intrabar.state,
                    )
                    continue
                if intrabar.exit_price_before_costs is None or intrabar.reason is None:
                    raise RuntimeError("portfolio intrabar exit missing reason or price")
                exit_price = intrabar.exit_price_before_costs * (Decimal("1") - slip)
                exit_fee = _fill_fee(
                    notional=position.quantity * exit_price,
                    policy=self.portfolio_policy,
                )
                fill_count += 1
                _sell(
                    ledger=ledger,
                    fill_count=fill_count,
                    execution_index=execution_index,
                    bar=current_bars[symbol],
                    quantity=position.quantity,
                    price=exit_price,
                    fee=exit_fee,
                )
                traded_notional += position.quantity * exit_price
                reason = _map_intrabar_reason(intrabar.reason)
                trade = _closed_trade(
                    symbol=symbol,
                    state=state,
                    exit_time=execution_time,
                    exit_reference_price=intrabar.exit_price_before_costs,
                    exit_price=exit_price,
                    quantity=position.quantity,
                    average_cost=position.average_cost,
                    exit_fee=exit_fee,
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
                last_exit_index[symbol] = execution_index
                exited_today.add(symbol)
                if self.reentry_policy is not None:
                    reentry_states[symbol] = arm_after_exit(policy=self.reentry_policy)

            if research_daily_funding is not None:
                funding_context = ResearchDailyFundingContext(
                    day_utc=execution_time,
                    closed_today=tuple(closed_trades[closed_before_today:]),
                    open_positions=tuple(
                        ResearchFundingPosition(
                            symbol, state.entry_time, ledger.position(symbol).quantity,
                            state.entry_execution_price,
                        )
                        for symbol, state in sorted(open_states.items())
                    ),
                )
                flows = research_daily_funding(funding_context)
                if not isinstance(flows, tuple):
                    raise ValueError("research funding output must be an immutable tuple")
                exposed = {t.symbol for t in funding_context.closed_today} | {
                    p.symbol for p in funding_context.open_positions
                }
                pending_ids: set[str] = set()
                for flow in flows:
                    if not isinstance(flow, ResearchFundingCashflow):
                        raise ValueError("research funding cashflow must be typed")
                    if (
                        not isinstance(flow.settlement_time, datetime)
                        or flow.settlement_time.tzinfo is None
                        or flow.settlement_time.utcoffset() != timedelta(0)
                        or not execution_time <= flow.settlement_time
                        < execution_time + timedelta(days=1)
                        or flow.symbol not in exposed
                    ):
                        raise ValueError("funding flow is outside actual symbol/day exposure")
                    expected_id = (
                        f"research-funding:{flow.symbol}:{flow.settlement_time.isoformat()}"
                    )
                    if flow.activity_id != expected_id:
                        raise ValueError("funding activity identity is not canonical")
                    if flow.activity_id in funding_activity_ids | pending_ids:
                        raise ValueError("duplicate funding activity")
                    if (
                        not isinstance(flow.amount, Decimal) or not flow.amount.is_finite()
                        or flow.amount == 0
                    ):
                        raise ValueError("funding amount must be nonzero finite Decimal")
                    pending_ids.add(flow.activity_id)
                for flow in sorted(flows, key=lambda x: (x.settlement_time, x.symbol)):
                    # Funding credits/debits are NOT external contributions, nor
                    # execution fees. This preserves fees_paid and cost basis.
                    ledger.apply_cash_adjustment(
                        activity_id=flow.activity_id, amount=flow.amount,
                        kind=CashAdjustmentKind.INCOME,
                    )
                funding_activity_ids.update(pending_ids)

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
                )
            )

        last_prices = {
            symbol: by_symbol[symbol][-1].close for symbol in symbols
        }
        snapshot = ledger.snapshot(last_prices)
        wins = sum(trade.net_pnl > 0 for trade in closed_trades)
        losses = sum(trade.net_pnl < 0 for trade in closed_trades)
        gross_profit = sum(
            (trade.net_pnl for trade in closed_trades if trade.net_pnl > 0),
            Decimal("0"),
        )
        gross_loss = sum(
            (trade.net_pnl for trade in closed_trades if trade.net_pnl < 0),
            Decimal("0"),
        )
        profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else None
        return CrossSectionalPortfolioResult(
            opening_cash=self.portfolio_policy.opening_cash,
            ending_equity=snapshot.equity,
            fill_count=fill_count,
            closed_trade_count=len(closed_trades),
            winning_trades=wins,
            losing_trades=losses,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            profit_factor=profit_factor,
            total_pnl=snapshot.total_pnl,
            total_return=snapshot.total_pnl / self.portfolio_policy.opening_cash,
            max_drawdown=max_drawdown,
            max_drawdown_fraction=max_drawdown / self.portfolio_policy.opening_cash,
            turnover_fraction=traded_notional / self.portfolio_policy.opening_cash,
            fees_paid=snapshot.fees_paid,
            maximum_gross_exposure_fraction_observed=max_gross_fraction,
            maximum_concurrent_positions=max_positions,
            one_bar_reentry_count=one_bar_reentries,
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
        IntrabarExitReason.TAKE_PROFIT: PortfolioExitReason.INTRABAR_TAKE_PROFIT,
        IntrabarExitReason.TRAILING_STOP: PortfolioExitReason.INTRABAR_TRAILING_STOP,
    }[reason]


def _closed_trade(
    *,
    symbol: str,
    state: _OpenPositionState,
    exit_time: datetime,
    exit_reference_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
    average_cost: Decimal,
    exit_fee: Decimal,
    execution_index: int,
    reason: PortfolioExitReason,
    ambiguous: bool = False,
    gap: bool = False,
) -> PortfolioTrade:
    gross_pnl_before_costs = (
        exit_reference_price - state.entry_reference_price
    ) * quantity
    slippage_cost = (
        (state.entry_execution_price - state.entry_reference_price) * quantity
        + (exit_reference_price - exit_price) * quantity
    )
    return PortfolioTrade(
        symbol=symbol,
        entry_time=state.entry_time,
        exit_time=exit_time,
        entry_reference_price=state.entry_reference_price,
        exit_reference_price=exit_reference_price,
        entry_execution_price=state.entry_execution_price,
        exit_execution_price=exit_price,
        entry_fee=state.entry_fee,
        exit_fee=exit_fee,
        slippage_cost=slippage_cost,
        gross_pnl_before_costs=gross_pnl_before_costs,
        quantity=quantity,
        net_pnl=(
            gross_pnl_before_costs
            - slippage_cost
            - state.entry_fee
            - exit_fee
        ),
        holding_bars=execution_index - state.entry_execution_index,
        exit_reason=reason,
        ambiguous_intrabar_exit=ambiguous,
        gap_through_stop=gap,
    )


def _fill_fee(
    *,
    notional: Decimal,
    policy: CrossSectionalPortfolioPolicy,
) -> Decimal:
    if notional < 0 or not notional.is_finite():
        raise ValueError("fill notional must be non-negative and finite")
    return policy.fee_per_fill + (
        notional * policy.fee_bps_per_fill / Decimal("10000")
    )


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
