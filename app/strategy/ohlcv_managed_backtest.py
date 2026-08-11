from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.domain.trading import Bar, Fill, Side
from app.marketdata.ohlcv import OhlcvBar
from app.portfolio.ledger import PortfolioLedger
from app.strategy.backtest import BacktestConfig
from app.strategy.ohlcv_exit import (
    IntrabarExitReason,
    IntrabarPositionState,
    evaluate_long_intrabar_exit,
)
from app.strategy.position_management import (
    ExitReason,
    PositionManagementPolicy,
    PositionTrackingState,
    evaluate_position_exit,
)
from app.strategy.reentry_confirmation import (
    ReentryConfirmationPolicy,
    ReentryConfirmationState,
    arm_after_exit,
    clear_after_entry,
    evaluate_reentry_confirmation,
)
from app.strategy.regime_momentum import RegimeAwareMomentumStrategy


class OhlcvExitReason(StrEnum):
    SIGNAL_EXIT = "SIGNAL_EXIT"
    TIME_STOP = "TIME_STOP"
    CLOSE_STOP_LOSS = "CLOSE_STOP_LOSS"
    CLOSE_TAKE_PROFIT = "CLOSE_TAKE_PROFIT"
    CLOSE_TRAILING_STOP = "CLOSE_TRAILING_STOP"
    INTRABAR_HARD_STOP = "INTRABAR_HARD_STOP"
    INTRABAR_TAKE_PROFIT = "INTRABAR_TAKE_PROFIT"
    INTRABAR_TRAILING_STOP = "INTRABAR_TRAILING_STOP"


@dataclass(frozen=True)
class OhlcvClosedTrade:
    entry_time: datetime
    exit_time: datetime
    entry_execution_price: Decimal
    exit_execution_price: Decimal
    quantity: Decimal
    net_pnl: Decimal
    holding_bars: int
    exit_reason: OhlcvExitReason
    ambiguous_intrabar_exit: bool
    gap_through_stop: bool


@dataclass(frozen=True)
class OhlcvManagedBacktestResult:
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
    fees_paid: Decimal
    final_quantity: Decimal
    intrabar_exit_count: int
    ambiguous_intrabar_exit_count: int
    gap_stop_exit_count: int
    closed_trades: tuple[OhlcvClosedTrade, ...]


@dataclass(frozen=True)
class _OpenTrade:
    entry_time: datetime
    entry_execution_index: int
    entry_execution_price: Decimal
    close_tracking: PositionTrackingState
    intrabar_state: IntrabarPositionState


class OhlcvManagedHistoricalBacktester:
    """Long-only OHLCV shadow backtest with next-open decisions and intrabar exits.

    Entry and signal/close-managed exits are decided from completed prior bars and
    execute at the next bar open. A position that remains open is then exposed to the
    current bar high/low through the conservative intrabar exit engine. No high/low
    ordering is inferred, and same-bar stop/take ambiguity resolves to protection.
    """

    def __init__(
        self,
        *,
        strategy: RegimeAwareMomentumStrategy,
        position_policy: PositionManagementPolicy | None = None,
        reentry_policy: ReentryConfirmationPolicy | None = None,
        config: BacktestConfig | None = None,
    ) -> None:
        position = PositionManagementPolicy() if position_policy is None else position_policy
        position.validate()
        backtest = BacktestConfig() if config is None else config
        backtest.validate()
        if reentry_policy is not None:
            reentry_policy.validate()
        self.strategy = strategy
        self.position_policy = position
        self.reentry_policy = reentry_policy
        self.config = backtest

    def run(
        self,
        bars: Sequence[OhlcvBar],
        *,
        first_execution_index: int | None = None,
    ) -> OhlcvManagedBacktestResult:
        ordered = list(bars)
        required_history = max(
            self.config.minimum_history_bars,
            self.strategy.config.minimum_history_bars,
        )
        if len(ordered) <= required_history:
            raise ValueError("insufficient bars for OHLCV managed backtest")
        for bar in ordered:
            bar.validate()
        if len({bar.symbol for bar in ordered}) != 1:
            raise ValueError("OHLCV managed backtest requires exactly one symbol")
        timestamps = [bar.timestamp for bar in ordered]
        if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
            raise ValueError("OHLCV managed bars must be strictly increasing")

        first_execution = (
            required_history if first_execution_index is None else first_execution_index
        )
        if first_execution < required_history:
            raise ValueError("first execution cannot precede strategy history")
        if first_execution >= len(ordered):
            raise ValueError("first execution must be inside supplied OHLCV bars")

        ledger = PortfolioLedger(opening_cash=self.config.opening_cash)
        slip = self.config.slippage_bps / Decimal("10000")
        peak_equity = self.config.opening_cash
        max_drawdown = Decimal("0")
        fill_count = 0
        open_trade: _OpenTrade | None = None
        closed: list[OhlcvClosedTrade] = []
        reentry_state = ReentryConfirmationState()

        for execution_index in range(first_execution, len(ordered)):
            execution_bar = ordered[execution_index]
            history = ordered[:execution_index]
            signal = self.strategy.signal(_close_bars(history))
            desired_quantity = (
                self.strategy.target_quantity if signal.eligible else Decimal("0")
            )
            current = ledger.position(execution_bar.symbol)
            close_reason: OhlcvExitReason | None = None

            if current.quantity == 0 and self.reentry_policy is not None:
                reentry = evaluate_reentry_confirmation(
                    signal_eligible=signal.eligible,
                    state=reentry_state,
                    policy=self.reentry_policy,
                )
                reentry_state = reentry.state
                if not reentry.allow_entry:
                    desired_quantity = Decimal("0")

            if current.quantity > 0:
                if open_trade is None:
                    raise RuntimeError("OHLCV open position missing tracking state")
                close_decision = evaluate_position_exit(
                    average_cost=current.average_cost,
                    reference_price=history[-1].close,
                    state=open_trade.close_tracking,
                    current_execution_index=execution_index,
                    policy=self.position_policy,
                )
                open_trade = _OpenTrade(
                    entry_time=open_trade.entry_time,
                    entry_execution_index=open_trade.entry_execution_index,
                    entry_execution_price=open_trade.entry_execution_price,
                    close_tracking=PositionTrackingState(
                        entry_execution_index=open_trade.entry_execution_index,
                        peak_reference_price=close_decision.peak_reference_price,
                    ),
                    intrabar_state=open_trade.intrabar_state,
                )
                if close_decision.exit_now:
                    desired_quantity = Decimal("0")
                    close_reason = _map_close_exit_reason(close_decision.reason)
                elif desired_quantity == 0:
                    close_reason = OhlcvExitReason.SIGNAL_EXIT

            if current.quantity > 0 and desired_quantity == 0:
                if open_trade is None or close_reason is None:
                    raise RuntimeError("OHLCV next-open exit requires tracking and reason")
                exit_price = execution_bar.open * (Decimal("1") - slip)
                fill_count += 1
                _apply_sell(
                    ledger=ledger,
                    fill_count=fill_count,
                    execution_index=execution_index,
                    bar=execution_bar,
                    quantity=current.quantity,
                    price=exit_price,
                    fee=self.config.fee_per_fill,
                )
                closed.append(
                    _closed_trade(
                        trade=open_trade,
                        exit_time=execution_bar.timestamp,
                        exit_execution_price=exit_price,
                        quantity=current.quantity,
                        average_cost=current.average_cost,
                        exit_fee=self.config.fee_per_fill,
                        execution_index=execution_index,
                        reason=close_reason,
                        ambiguous=False,
                        gap=False,
                    )
                )
                open_trade = None
                if self.reentry_policy is not None:
                    reentry_state = arm_after_exit(policy=self.reentry_policy)
            elif current.quantity == 0 and desired_quantity > 0:
                entry_price = execution_bar.open * (Decimal("1") + slip)
                fill_count += 1
                _apply_buy(
                    ledger=ledger,
                    fill_count=fill_count,
                    execution_index=execution_index,
                    bar=execution_bar,
                    quantity=desired_quantity,
                    price=entry_price,
                    fee=self.config.fee_per_fill,
                )
                if self.reentry_policy is not None:
                    reentry_state = clear_after_entry()
                opened = ledger.position(execution_bar.symbol)
                open_trade = _OpenTrade(
                    entry_time=execution_bar.timestamp,
                    entry_execution_index=execution_index,
                    entry_execution_price=entry_price,
                    close_tracking=PositionTrackingState(
                        entry_execution_index=execution_index,
                        peak_reference_price=history[-1].close,
                    ),
                    intrabar_state=IntrabarPositionState(
                        peak_completed_price=execution_bar.open
                    ),
                )
                fill_count, open_trade = self._apply_intrabar_if_triggered(
                    ledger=ledger,
                    fill_count=fill_count,
                    execution_index=execution_index,
                    execution_bar=execution_bar,
                    open_trade=open_trade,
                    quantity=opened.quantity,
                    average_cost=opened.average_cost,
                    slip=slip,
                    closed=closed,
                )
                if open_trade is None and self.reentry_policy is not None:
                    reentry_state = arm_after_exit(policy=self.reentry_policy)
            elif current.quantity > 0 and desired_quantity == current.quantity:
                if open_trade is None:
                    raise RuntimeError("OHLCV hold requires tracking state")
                fill_count, open_trade = self._apply_intrabar_if_triggered(
                    ledger=ledger,
                    fill_count=fill_count,
                    execution_index=execution_index,
                    execution_bar=execution_bar,
                    open_trade=open_trade,
                    quantity=current.quantity,
                    average_cost=current.average_cost,
                    slip=slip,
                    closed=closed,
                )
                if open_trade is None and self.reentry_policy is not None:
                    reentry_state = arm_after_exit(policy=self.reentry_policy)
            elif current.quantity != desired_quantity:
                raise ValueError("OHLCV_MANAGED_SCALING_NOT_SUPPORTED")

            mark_position = ledger.position(execution_bar.symbol)
            mark_price = execution_bar.close
            equity = ledger.equity({execution_bar.symbol: mark_price})
            peak_equity = max(peak_equity, equity)
            max_drawdown = max(max_drawdown, peak_equity - equity)
            if mark_position.quantity > 0 and open_trade is not None:
                open_trade = _OpenTrade(
                    entry_time=open_trade.entry_time,
                    entry_execution_index=open_trade.entry_execution_index,
                    entry_execution_price=open_trade.entry_execution_price,
                    close_tracking=PositionTrackingState(
                        entry_execution_index=open_trade.entry_execution_index,
                        peak_reference_price=max(
                            open_trade.close_tracking.peak_reference_price,
                            execution_bar.close,
                        ),
                    ),
                    intrabar_state=open_trade.intrabar_state,
                )

        last = ordered[-1]
        snapshot = ledger.snapshot({last.symbol: last.close})
        wins = sum(trade.net_pnl > 0 for trade in closed)
        losses = sum(trade.net_pnl < 0 for trade in closed)
        gross_profit = sum(
            (trade.net_pnl for trade in closed if trade.net_pnl > 0), Decimal("0")
        )
        gross_loss = sum(
            (trade.net_pnl for trade in closed if trade.net_pnl < 0), Decimal("0")
        )
        profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else None
        return OhlcvManagedBacktestResult(
            fill_count=fill_count,
            closed_trade_count=len(closed),
            winning_trades=wins,
            losing_trades=losses,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            profit_factor=profit_factor,
            total_pnl=snapshot.total_pnl,
            total_return=snapshot.total_pnl / self.config.opening_cash,
            max_drawdown=max_drawdown,
            fees_paid=snapshot.fees_paid,
            final_quantity=ledger.position(last.symbol).quantity,
            intrabar_exit_count=sum(
                trade.exit_reason.value.startswith("INTRABAR_") for trade in closed
            ),
            ambiguous_intrabar_exit_count=sum(
                trade.ambiguous_intrabar_exit for trade in closed
            ),
            gap_stop_exit_count=sum(trade.gap_through_stop for trade in closed),
            closed_trades=tuple(closed),
        )

    def _apply_intrabar_if_triggered(
        self,
        *,
        ledger: PortfolioLedger,
        fill_count: int,
        execution_index: int,
        execution_bar: OhlcvBar,
        open_trade: _OpenTrade,
        quantity: Decimal,
        average_cost: Decimal,
        slip: Decimal,
        closed: list[OhlcvClosedTrade],
    ) -> tuple[int, _OpenTrade | None]:
        intrabar = evaluate_long_intrabar_exit(
            average_cost=average_cost,
            bar=execution_bar,
            state=open_trade.intrabar_state,
            policy=self.position_policy,
        )
        if not intrabar.exit_now:
            return (
                fill_count,
                _OpenTrade(
                    entry_time=open_trade.entry_time,
                    entry_execution_index=open_trade.entry_execution_index,
                    entry_execution_price=open_trade.entry_execution_price,
                    close_tracking=open_trade.close_tracking,
                    intrabar_state=intrabar.state,
                ),
            )
        if intrabar.exit_price_before_costs is None or intrabar.reason is None:
            raise RuntimeError("intrabar exit missing price or reason")
        exit_price = intrabar.exit_price_before_costs * (Decimal("1") - slip)
        fill_count += 1
        _apply_sell(
            ledger=ledger,
            fill_count=fill_count,
            execution_index=execution_index,
            bar=execution_bar,
            quantity=quantity,
            price=exit_price,
            fee=self.config.fee_per_fill,
        )
        closed.append(
            _closed_trade(
                trade=open_trade,
                exit_time=execution_bar.timestamp,
                exit_execution_price=exit_price,
                quantity=quantity,
                average_cost=average_cost,
                exit_fee=self.config.fee_per_fill,
                execution_index=execution_index,
                reason=_map_intrabar_exit_reason(intrabar.reason),
                ambiguous=intrabar.ambiguous_bar,
                gap=intrabar.gap_through_protective_stop,
            )
        )
        return fill_count, None


def _close_bars(bars: Sequence[OhlcvBar]) -> list[Bar]:
    return [Bar(bar.symbol, bar.timestamp, bar.close) for bar in bars]


def _map_close_exit_reason(reason: ExitReason | None) -> OhlcvExitReason:
    mapping = {
        ExitReason.STOP_LOSS: OhlcvExitReason.CLOSE_STOP_LOSS,
        ExitReason.TAKE_PROFIT: OhlcvExitReason.CLOSE_TAKE_PROFIT,
        ExitReason.TRAILING_STOP: OhlcvExitReason.CLOSE_TRAILING_STOP,
        ExitReason.TIME_STOP: OhlcvExitReason.TIME_STOP,
        ExitReason.SIGNAL_EXIT: OhlcvExitReason.SIGNAL_EXIT,
    }
    if reason is None:
        raise ValueError("close exit reason is required")
    return mapping[reason]


def _map_intrabar_exit_reason(reason: IntrabarExitReason) -> OhlcvExitReason:
    return {
        IntrabarExitReason.HARD_STOP: OhlcvExitReason.INTRABAR_HARD_STOP,
        IntrabarExitReason.TAKE_PROFIT: OhlcvExitReason.INTRABAR_TAKE_PROFIT,
        IntrabarExitReason.TRAILING_STOP: OhlcvExitReason.INTRABAR_TRAILING_STOP,
    }[reason]


def _closed_trade(
    *,
    trade: _OpenTrade,
    exit_time: datetime,
    exit_execution_price: Decimal,
    quantity: Decimal,
    average_cost: Decimal,
    exit_fee: Decimal,
    execution_index: int,
    reason: OhlcvExitReason,
    ambiguous: bool,
    gap: bool,
) -> OhlcvClosedTrade:
    return OhlcvClosedTrade(
        entry_time=trade.entry_time,
        exit_time=exit_time,
        entry_execution_price=trade.entry_execution_price,
        exit_execution_price=exit_execution_price,
        quantity=quantity,
        net_pnl=(exit_execution_price - average_cost) * quantity - exit_fee,
        holding_bars=execution_index - trade.entry_execution_index,
        exit_reason=reason,
        ambiguous_intrabar_exit=ambiguous,
        gap_through_stop=gap,
    )


def _apply_buy(
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
            fill_id=f"ohlcv-bt-{fill_count}-{bar.timestamp.isoformat()}",
            order_intent_id=f"ohlcv-bt-intent-{execution_index}-buy",
            symbol=bar.symbol,
            side=Side.BUY,
            quantity=quantity,
            price=price,
            occurred_at=bar.timestamp,
            fee=fee,
        )
    )


def _apply_sell(
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
            fill_id=f"ohlcv-bt-{fill_count}-{bar.timestamp.isoformat()}",
            order_intent_id=f"ohlcv-bt-intent-{execution_index}-sell",
            symbol=bar.symbol,
            side=Side.SELL,
            quantity=quantity,
            price=price,
            occurred_at=bar.timestamp,
            fee=fee,
        )
    )
