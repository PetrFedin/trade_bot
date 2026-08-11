from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain.trading import Bar, Fill, Side
from app.portfolio.ledger import PortfolioLedger
from app.strategy.backtest import BacktestConfig
from app.strategy.position_management import (
    ExitReason,
    PositionManagementPolicy,
    PositionTrackingState,
    evaluate_position_exit,
)
from app.strategy.regime_momentum import RegimeAwareMomentumStrategy


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    entry_time: datetime
    exit_time: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    net_pnl: Decimal
    return_fraction: Decimal
    holding_bars: int
    exit_reason: ExitReason


@dataclass(frozen=True)
class ManagedBacktestResult:
    fill_count: int
    closed_trade_count: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    average_closed_trade_pnl: Decimal
    ending_equity: Decimal
    total_pnl: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    fees_paid: Decimal
    final_quantity: Decimal
    closed_trades: tuple[ClosedTrade, ...]


@dataclass(frozen=True)
class _OpenTrade:
    entry_time: datetime
    entry_price: Decimal
    tracking: PositionTrackingState


class ManagedHistoricalBacktester:
    """No-lookahead evaluator with deterministic close-only position management.

    Signal and exit decisions consume bars strictly before the execution bar. Any
    resulting trade is filled on the execution bar close with the same adverse
    slippage and fee model as ``HistoricalBacktester``. Because ``Bar`` currently
    contains close only, stop/take/trailing decisions are close-to-next-close rules,
    not intrabar stop guarantees.
    """

    def __init__(
        self,
        *,
        strategy: RegimeAwareMomentumStrategy,
        position_policy: PositionManagementPolicy | None = None,
        config: BacktestConfig | None = None,
    ) -> None:
        backtest_config = BacktestConfig() if config is None else config
        backtest_config.validate()
        policy = PositionManagementPolicy() if position_policy is None else position_policy
        policy.validate()
        self.strategy = strategy
        self.position_policy = policy
        self.config = backtest_config

    def run(
        self,
        bars: Sequence[Bar],
        *,
        first_execution_index: int | None = None,
    ) -> ManagedBacktestResult:
        ordered = list(bars)
        required_history = max(
            self.config.minimum_history_bars,
            self.strategy.config.minimum_history_bars,
        )
        if len(ordered) <= required_history:
            raise ValueError("insufficient bars for managed no-lookahead backtest")
        for bar in ordered:
            bar.validate()
        if len({bar.symbol for bar in ordered}) != 1:
            raise ValueError("managed backtest requires exactly one symbol")
        timestamps = [bar.timestamp for bar in ordered]
        if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
            raise ValueError("managed backtest bars must be strictly increasing")

        first_execution = (
            required_history if first_execution_index is None else first_execution_index
        )
        if first_execution < required_history:
            raise ValueError("first execution cannot precede strategy history")
        if first_execution >= len(ordered):
            raise ValueError("first execution must be inside the supplied bar series")

        ledger = PortfolioLedger(opening_cash=self.config.opening_cash)
        peak_equity = self.config.opening_cash
        max_drawdown = Decimal("0")
        slip = self.config.slippage_bps / Decimal("10000")
        fill_count = 0
        open_trade: _OpenTrade | None = None
        closed_trades: list[ClosedTrade] = []

        for execution_index in range(first_execution, len(ordered)):
            history = ordered[:execution_index]
            target = self.strategy.target(history)
            execution_bar = ordered[execution_index]
            current = ledger.position(target.symbol)
            desired_quantity = target.quantity
            exit_reason: ExitReason | None = None

            if current.quantity > 0:
                if open_trade is None:
                    raise RuntimeError("position tracking state missing for open position")
                if desired_quantity not in (Decimal("0"), current.quantity):
                    raise ValueError("MANAGED_BACKTEST_SCALING_NOT_SUPPORTED")
                decision = evaluate_position_exit(
                    average_cost=current.average_cost,
                    reference_price=history[-1].close,
                    state=open_trade.tracking,
                    current_execution_index=execution_index,
                    policy=self.position_policy,
                )
                open_trade = _OpenTrade(
                    entry_time=open_trade.entry_time,
                    entry_price=open_trade.entry_price,
                    tracking=PositionTrackingState(
                        entry_execution_index=open_trade.tracking.entry_execution_index,
                        peak_reference_price=decision.peak_reference_price,
                    ),
                )
                if decision.exit_now:
                    desired_quantity = Decimal("0")
                    exit_reason = decision.reason
                elif desired_quantity == 0:
                    exit_reason = ExitReason.SIGNAL_EXIT

            delta = desired_quantity - current.quantity
            if delta != 0:
                side = Side.BUY if delta > 0 else Side.SELL
                execution_price = execution_bar.close * (
                    Decimal("1") + slip if side is Side.BUY else Decimal("1") - slip
                )
                if execution_price <= 0:
                    raise ValueError("slippage produced invalid execution price")
                quantity = abs(delta)
                fill_count += 1
                fill = Fill(
                    fill_id=f"managed-bt-{fill_count}-{execution_bar.timestamp.isoformat()}",
                    order_intent_id=f"managed-bt-intent-{execution_index - 1}",
                    symbol=target.symbol,
                    side=side,
                    quantity=quantity,
                    price=execution_price,
                    occurred_at=execution_bar.timestamp,
                    fee=self.config.fee_per_fill,
                )

                if side is Side.BUY:
                    if current.quantity != 0 or open_trade is not None:
                        raise ValueError("MANAGED_BACKTEST_SCALE_IN_NOT_SUPPORTED")
                    ledger.apply_fill(fill)
                    open_trade = _OpenTrade(
                        entry_time=execution_bar.timestamp,
                        entry_price=execution_price,
                        tracking=PositionTrackingState(
                            entry_execution_index=execution_index,
                            peak_reference_price=execution_bar.close,
                        ),
                    )
                else:
                    if open_trade is None or exit_reason is None:
                        raise RuntimeError("exit requires tracked trade and exit reason")
                    if quantity != current.quantity:
                        raise ValueError("MANAGED_BACKTEST_PARTIAL_EXIT_NOT_SUPPORTED")
                    prior_average_cost = current.average_cost
                    ledger.apply_fill(fill)
                    net_pnl = (
                        (execution_price - prior_average_cost) * quantity
                        - self.config.fee_per_fill
                    )
                    invested = prior_average_cost * quantity
                    closed_trades.append(
                        ClosedTrade(
                            symbol=target.symbol,
                            entry_time=open_trade.entry_time,
                            exit_time=execution_bar.timestamp,
                            entry_price=open_trade.entry_price,
                            exit_price=execution_price,
                            quantity=quantity,
                            net_pnl=net_pnl,
                            return_fraction=net_pnl / invested,
                            holding_bars=(
                                execution_index
                                - open_trade.tracking.entry_execution_index
                            ),
                            exit_reason=exit_reason,
                        )
                    )
                    open_trade = None

            equity = ledger.equity({execution_bar.symbol: execution_bar.close})
            peak_equity = max(peak_equity, equity)
            max_drawdown = max(max_drawdown, peak_equity - equity)

        last = ordered[-1]
        snapshot = ledger.snapshot({last.symbol: last.close})
        wins = sum(trade.net_pnl > 0 for trade in closed_trades)
        losses = sum(trade.net_pnl < 0 for trade in closed_trades)
        breakeven = len(closed_trades) - wins - losses
        gross_profit = sum(
            (trade.net_pnl for trade in closed_trades if trade.net_pnl > 0),
            Decimal("0"),
        )
        gross_loss = sum(
            (trade.net_pnl for trade in closed_trades if trade.net_pnl < 0),
            Decimal("0"),
        )
        closed_count = len(closed_trades)
        win_rate = (
            Decimal(wins) / Decimal(closed_count) if closed_count else Decimal("0")
        )
        average_pnl = (
            sum((trade.net_pnl for trade in closed_trades), Decimal("0"))
            / Decimal(closed_count)
            if closed_count
            else Decimal("0")
        )
        profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else None
        return ManagedBacktestResult(
            fill_count=fill_count,
            closed_trade_count=closed_count,
            winning_trades=wins,
            losing_trades=losses,
            breakeven_trades=breakeven,
            win_rate=win_rate,
            gross_profit=gross_profit,
            gross_loss=gross_loss,
            profit_factor=profit_factor,
            average_closed_trade_pnl=average_pnl,
            ending_equity=snapshot.equity,
            total_pnl=snapshot.total_pnl,
            total_return=snapshot.total_pnl / self.config.opening_cash,
            max_drawdown=max_drawdown,
            fees_paid=snapshot.fees_paid,
            final_quantity=ledger.position(last.symbol).quantity,
            closed_trades=tuple(closed_trades),
        )
