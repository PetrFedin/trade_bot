from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.domain.trading import Bar, Fill, Side
from app.portfolio.ledger import PortfolioLedger
from app.strategy.momentum import LongOnlyMomentumStrategy


@dataclass(frozen=True)
class BacktestConfig:
    opening_cash: Decimal = Decimal("10000")
    fee_per_fill: Decimal = Decimal("0")
    slippage_bps: Decimal = Decimal("0")
    minimum_history_bars: int = 3

    def validate(self) -> None:
        if not self.opening_cash.is_finite() or self.opening_cash <= 0:
            raise ValueError("opening_cash must be positive and finite")
        if not self.fee_per_fill.is_finite() or self.fee_per_fill < 0:
            raise ValueError("fee_per_fill must be finite and non-negative")
        if not self.slippage_bps.is_finite() or self.slippage_bps < 0:
            raise ValueError("slippage_bps must be finite and non-negative")
        if self.minimum_history_bars < 3:
            raise ValueError("minimum_history_bars must be at least three")


@dataclass(frozen=True)
class BacktestResult:
    trades: int
    ending_equity: Decimal
    total_pnl: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    fees_paid: Decimal
    final_quantity: Decimal


class HistoricalBacktester:
    """Deterministic close-to-next-close evaluator with no same-bar execution.

    A target is computed only from bars available through time t. Any resulting trade
    executes at bar t+1 with configured adverse slippage, preventing same-bar lookahead.
    """

    def __init__(
        self,
        *,
        strategy: LongOnlyMomentumStrategy,
        config: BacktestConfig | None = None,
    ) -> None:
        config = BacktestConfig() if config is None else config
        config.validate()
        self.strategy = strategy
        self.config = config

    def run(self, bars: Sequence[Bar]) -> BacktestResult:
        if len(bars) <= self.config.minimum_history_bars:
            raise ValueError("insufficient bars for no-lookahead backtest")
        ordered = list(bars)
        for bar in ordered:
            bar.validate()
        if len({bar.symbol for bar in ordered}) != 1:
            raise ValueError("backtest requires exactly one symbol")
        timestamps = [bar.timestamp for bar in ordered]
        if timestamps != sorted(timestamps) or len(set(timestamps)) != len(timestamps):
            raise ValueError("backtest bars must be strictly increasing")

        ledger = PortfolioLedger(opening_cash=self.config.opening_cash)
        peak_equity = self.config.opening_cash
        max_drawdown = Decimal("0")
        trades = 0
        slip = self.config.slippage_bps / Decimal("10000")

        for signal_index in range(self.config.minimum_history_bars - 1, len(ordered) - 1):
            history = ordered[: signal_index + 1]
            target = self.strategy.target(history)
            execution_bar = ordered[signal_index + 1]
            current = ledger.position(target.symbol)
            delta = target.quantity - current.quantity
            if delta != 0:
                side = Side.BUY if delta > 0 else Side.SELL
                execution_price = execution_bar.close * (
                    Decimal("1") + slip if side is Side.BUY else Decimal("1") - slip
                )
                if execution_price <= 0:
                    raise ValueError("slippage produced invalid execution price")
                ledger.apply_fill(
                    Fill(
                        fill_id=f"bt-{trades + 1}-{execution_bar.timestamp.isoformat()}",
                        order_intent_id=f"bt-intent-{signal_index}",
                        symbol=target.symbol,
                        side=side,
                        quantity=abs(delta),
                        price=execution_price,
                        occurred_at=execution_bar.timestamp,
                        fee=self.config.fee_per_fill,
                    )
                )
                trades += 1
            equity = ledger.equity({execution_bar.symbol: execution_bar.close})
            peak_equity = max(peak_equity, equity)
            drawdown = peak_equity - equity
            max_drawdown = max(max_drawdown, drawdown)

        last = ordered[-1]
        snapshot = ledger.snapshot({last.symbol: last.close})
        return BacktestResult(
            trades=trades,
            ending_equity=snapshot.equity,
            total_pnl=snapshot.total_pnl,
            total_return=snapshot.total_pnl / self.config.opening_cash,
            max_drawdown=max_drawdown,
            fees_paid=snapshot.fees_paid,
            final_quantity=ledger.position(last.symbol).quantity,
        )
