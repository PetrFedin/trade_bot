from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

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


class DecisionAction(StrEnum):
    ENTER = "ENTER"
    HOLD = "HOLD"
    EXIT = "EXIT"
    STAY_FLAT = "STAY_FLAT"


@dataclass(frozen=True)
class StrategyDecisionTrace:
    execution_index: int
    decision_time: datetime
    execution_time: datetime
    symbol: str
    action: DecisionAction
    signal_eligible: bool
    signal_reasons: tuple[str, ...]
    signal_target_quantity: Decimal
    final_target_quantity: Decimal
    current_quantity: Decimal
    decision_reference_price: Decimal
    momentum_return: Decimal
    trend_strength: Decimal
    realized_volatility: Decimal
    position_profit_fraction: Decimal | None
    drawdown_from_peak_fraction: Decimal | None
    exit_reason: ExitReason | None


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
    maximum_favorable_excursion_fraction: Decimal
    maximum_adverse_excursion_fraction: Decimal
    mfe_capture_ratio: Decimal | None
    mfe_giveback_fraction: Decimal | None


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
    average_maximum_favorable_excursion_fraction: Decimal
    average_maximum_adverse_excursion_fraction: Decimal
    average_mfe_capture_ratio: Decimal | None
    positive_mfe_trades: int
    positive_mfe_closed_profitable: int
    positive_mfe_closed_losing_or_flat: int
    profit_preservation_rate: Decimal | None
    ending_equity: Decimal
    total_pnl: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    fees_paid: Decimal
    final_quantity: Decimal
    closed_trades: tuple[ClosedTrade, ...]
    decision_trace: tuple[StrategyDecisionTrace, ...]


@dataclass(frozen=True)
class _OpenTrade:
    entry_time: datetime
    entry_price: Decimal
    tracking: PositionTrackingState
    peak_market_price: Decimal
    trough_market_price: Decimal


class ManagedHistoricalBacktester:
    """No-lookahead evaluator with deterministic close-only position management.

    Signal and exit decisions consume bars strictly before the execution bar. Any
    resulting trade is filled on the execution bar close with the same adverse
    slippage and fee model as ``HistoricalBacktester``. Because ``Bar`` currently
    contains close only, stop/take/trailing and MFE/MAE diagnostics are close-only;
    they do not represent intrabar stop guarantees or intrabar excursions.
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
        decision_trace: list[StrategyDecisionTrace] = []

        for execution_index in range(first_execution, len(ordered)):
            history = ordered[:execution_index]
            signal = self.strategy.signal(history)
            decision_bar = history[-1]
            execution_bar = ordered[execution_index]
            symbol = decision_bar.symbol
            current = ledger.position(symbol)
            signal_target_quantity = (
                self.strategy.target_quantity if signal.eligible else Decimal("0")
            )
            desired_quantity = signal_target_quantity
            exit_reason: ExitReason | None = None
            position_profit_fraction: Decimal | None = None
            drawdown_from_peak_fraction: Decimal | None = None

            if current.quantity > 0:
                if open_trade is None:
                    raise RuntimeError("position tracking state missing for open position")
                if desired_quantity not in (Decimal("0"), current.quantity):
                    raise ValueError("MANAGED_BACKTEST_SCALING_NOT_SUPPORTED")
                decision = evaluate_position_exit(
                    average_cost=current.average_cost,
                    reference_price=decision_bar.close,
                    state=open_trade.tracking,
                    current_execution_index=execution_index,
                    policy=self.position_policy,
                )
                position_profit_fraction = decision.profit_fraction
                drawdown_from_peak_fraction = decision.drawdown_from_peak_fraction
                open_trade = _OpenTrade(
                    entry_time=open_trade.entry_time,
                    entry_price=open_trade.entry_price,
                    tracking=PositionTrackingState(
                        entry_execution_index=open_trade.tracking.entry_execution_index,
                        peak_reference_price=decision.peak_reference_price,
                    ),
                    peak_market_price=max(
                        open_trade.peak_market_price, decision_bar.close
                    ),
                    trough_market_price=min(
                        open_trade.trough_market_price, decision_bar.close
                    ),
                )
                if decision.exit_now:
                    desired_quantity = Decimal("0")
                    exit_reason = decision.reason
                elif desired_quantity == 0:
                    exit_reason = ExitReason.SIGNAL_EXIT

            action = _decision_action(
                current_quantity=current.quantity,
                desired_quantity=desired_quantity,
            )
            decision_trace.append(
                StrategyDecisionTrace(
                    execution_index=execution_index,
                    decision_time=decision_bar.timestamp,
                    execution_time=execution_bar.timestamp,
                    symbol=symbol,
                    action=action,
                    signal_eligible=signal.eligible,
                    signal_reasons=signal.reasons,
                    signal_target_quantity=signal_target_quantity,
                    final_target_quantity=desired_quantity,
                    current_quantity=current.quantity,
                    decision_reference_price=decision_bar.close,
                    momentum_return=signal.momentum_return,
                    trend_strength=signal.trend_strength,
                    realized_volatility=signal.realized_volatility,
                    position_profit_fraction=position_profit_fraction,
                    drawdown_from_peak_fraction=drawdown_from_peak_fraction,
                    exit_reason=exit_reason,
                )
            )

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
                    symbol=symbol,
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
                        peak_market_price=execution_bar.close,
                        trough_market_price=execution_bar.close,
                    )
                else:
                    if open_trade is None or exit_reason is None:
                        raise RuntimeError("exit requires tracked trade and exit reason")
                    if quantity != current.quantity:
                        raise ValueError("MANAGED_BACKTEST_PARTIAL_EXIT_NOT_SUPPORTED")
                    prior_average_cost = current.average_cost
                    peak_market_price = max(
                        open_trade.peak_market_price, execution_bar.close
                    )
                    trough_market_price = min(
                        open_trade.trough_market_price, execution_bar.close
                    )
                    ledger.apply_fill(fill)
                    net_pnl = (
                        (execution_price - prior_average_cost) * quantity
                        - self.config.fee_per_fill
                    )
                    invested = prior_average_cost * quantity
                    mfe_fraction = max(
                        Decimal("0"),
                        (peak_market_price - prior_average_cost) / prior_average_cost,
                    )
                    mae_fraction = max(
                        Decimal("0"),
                        (prior_average_cost - trough_market_price) / prior_average_cost,
                    )
                    maximum_favorable_pnl = max(
                        Decimal("0"),
                        (peak_market_price - prior_average_cost) * quantity,
                    )
                    capture_ratio = (
                        net_pnl / maximum_favorable_pnl
                        if maximum_favorable_pnl > 0
                        else None
                    )
                    giveback_fraction = (
                        (maximum_favorable_pnl - net_pnl) / maximum_favorable_pnl
                        if maximum_favorable_pnl > 0
                        else None
                    )
                    closed_trades.append(
                        ClosedTrade(
                            symbol=symbol,
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
                            maximum_favorable_excursion_fraction=mfe_fraction,
                            maximum_adverse_excursion_fraction=mae_fraction,
                            mfe_capture_ratio=capture_ratio,
                            mfe_giveback_fraction=giveback_fraction,
                        )
                    )
                    open_trade = None

            equity = ledger.equity({execution_bar.symbol: execution_bar.close})
            peak_equity = max(peak_equity, equity)
            max_drawdown = max(max_drawdown, peak_equity - equity)

        last = ordered[-1]
        snapshot = ledger.snapshot({last.symbol: last.close})
        return _result(
            fill_count=fill_count,
            closed_trades=closed_trades,
            decision_trace=decision_trace,
            ending_equity=snapshot.equity,
            total_pnl=snapshot.total_pnl,
            opening_cash=self.config.opening_cash,
            max_drawdown=max_drawdown,
            fees_paid=snapshot.fees_paid,
            final_quantity=ledger.position(last.symbol).quantity,
        )


def _decision_action(
    *, current_quantity: Decimal, desired_quantity: Decimal
) -> DecisionAction:
    if current_quantity == 0 and desired_quantity > 0:
        return DecisionAction.ENTER
    if current_quantity > 0 and desired_quantity == 0:
        return DecisionAction.EXIT
    if current_quantity > 0 and desired_quantity == current_quantity:
        return DecisionAction.HOLD
    if current_quantity == 0 and desired_quantity == 0:
        return DecisionAction.STAY_FLAT
    raise ValueError("MANAGED_BACKTEST_TARGET_TRANSITION_NOT_SUPPORTED")


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        return Decimal("0")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _result(
    *,
    fill_count: int,
    closed_trades: Sequence[ClosedTrade],
    decision_trace: Sequence[StrategyDecisionTrace],
    ending_equity: Decimal,
    total_pnl: Decimal,
    opening_cash: Decimal,
    max_drawdown: Decimal,
    fees_paid: Decimal,
    final_quantity: Decimal,
) -> ManagedBacktestResult:
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
    win_rate = Decimal(wins) / Decimal(closed_count) if closed_count else Decimal("0")
    profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else None
    capture_ratios = [
        trade.mfe_capture_ratio
        for trade in closed_trades
        if trade.mfe_capture_ratio is not None
    ]
    positive_mfe = [
        trade for trade in closed_trades if trade.maximum_favorable_excursion_fraction > 0
    ]
    positive_mfe_profitable = sum(trade.net_pnl > 0 for trade in positive_mfe)
    positive_mfe_losing_or_flat = len(positive_mfe) - positive_mfe_profitable
    preservation_rate = (
        Decimal(positive_mfe_profitable) / Decimal(len(positive_mfe))
        if positive_mfe
        else None
    )
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
        average_closed_trade_pnl=_mean([trade.net_pnl for trade in closed_trades]),
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
        positive_mfe_closed_profitable=positive_mfe_profitable,
        positive_mfe_closed_losing_or_flat=positive_mfe_losing_or_flat,
        profit_preservation_rate=preservation_rate,
        ending_equity=ending_equity,
        total_pnl=total_pnl,
        total_return=total_pnl / opening_cash,
        max_drawdown=max_drawdown,
        fees_paid=fees_paid,
        final_quantity=final_quantity,
        closed_trades=tuple(closed_trades),
        decision_trace=tuple(decision_trace),
    )
