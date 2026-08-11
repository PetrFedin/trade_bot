from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from app.application.paper_execution_quality import (
    PaperExecutionQualitySummary,
    SQLitePaperExecutionQualityStore,
)
from app.application.paper_trade_quality import PaperTradeQualityTracker
from app.domain.trading import Side
from app.strategy.quality_monitor import (
    StrategyQualityGateDecision,
    TradeQualityMonitorPolicy,
)


@dataclass(frozen=True)
class PaperTradingQualityReport:
    strategy_id: str
    generated_at: datetime
    trade_count: int
    win_count: int
    loss_count: int
    breakeven_count: int
    win_rate: Decimal | None
    total_net_pnl: Decimal
    gross_profit: Decimal
    gross_loss: Decimal
    profit_factor: Decimal | None
    average_return_fraction: Decimal | None
    average_mfe_fraction: Decimal | None
    average_mae_fraction: Decimal | None
    average_mfe_capture_ratio: Decimal | None
    average_mfe_giveback_fraction: Decimal | None
    positive_mfe_trade_count: int
    profit_preserved_trade_count: int
    profit_preservation_rate: Decimal | None
    exit_reason_counts: tuple[tuple[str, int], ...]
    quality_gate: StrategyQualityGateDecision
    execution_all: PaperExecutionQualitySummary | None
    execution_entries: PaperExecutionQualitySummary | None
    execution_exits: PaperExecutionQualitySummary | None

    def to_dict(self) -> dict[str, Any]:
        return _json_safe(asdict(self))


def build_paper_trading_quality_report(
    *,
    tracker: PaperTradeQualityTracker,
    policy: TradeQualityMonitorPolicy,
    generated_at: datetime,
    execution_store: SQLitePaperExecutionQualityStore | None = None,
) -> PaperTradingQualityReport:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    policy.validate()
    trades = tracker.store.closed_trades(strategy_id=tracker.strategy_id)
    pnls = tuple(trade.net_pnl for trade in trades)
    returns = tuple(trade.return_fraction for trade in trades)
    mfe = tuple(trade.maximum_favorable_excursion_fraction for trade in trades)
    mae = tuple(trade.maximum_adverse_excursion_fraction for trade in trades)
    captures = tuple(
        trade.mfe_capture_ratio
        for trade in trades
        if trade.mfe_capture_ratio is not None
    )
    givebacks = tuple(
        trade.mfe_giveback_fraction
        for trade in trades
        if trade.mfe_giveback_fraction is not None
    )
    wins = sum(pnl > 0 for pnl in pnls)
    losses = sum(pnl < 0 for pnl in pnls)
    breakeven = len(pnls) - wins - losses
    gross_profit = sum((pnl for pnl in pnls if pnl > 0), start=Decimal("0"))
    gross_loss = -sum((pnl for pnl in pnls if pnl < 0), start=Decimal("0"))
    positive_mfe_trades = tuple(
        trade for trade in trades if trade.maximum_favorable_excursion_fraction > 0
    )
    preserved = sum(trade.net_pnl > 0 for trade in positive_mfe_trades)
    reason_counts: dict[str, int] = {}
    for trade in trades:
        reason_counts[trade.exit_reason] = reason_counts.get(trade.exit_reason, 0) + 1

    return PaperTradingQualityReport(
        strategy_id=tracker.strategy_id,
        generated_at=generated_at,
        trade_count=len(trades),
        win_count=wins,
        loss_count=losses,
        breakeven_count=breakeven,
        win_rate=(
            None if not trades else Decimal(wins) / Decimal(len(trades))
        ),
        total_net_pnl=sum(pnls, start=Decimal("0")),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=(
            None if gross_loss == 0 else gross_profit / gross_loss
        ),
        average_return_fraction=_average(returns),
        average_mfe_fraction=_average(mfe),
        average_mae_fraction=_average(mae),
        average_mfe_capture_ratio=_average(captures),
        average_mfe_giveback_fraction=_average(givebacks),
        positive_mfe_trade_count=len(positive_mfe_trades),
        profit_preserved_trade_count=preserved,
        profit_preservation_rate=(
            None
            if not positive_mfe_trades
            else Decimal(preserved) / Decimal(len(positive_mfe_trades))
        ),
        exit_reason_counts=tuple(sorted(reason_counts.items())),
        quality_gate=tracker.quality_gate(policy=policy),
        execution_all=(
            None if execution_store is None else execution_store.summary()
        ),
        execution_entries=(
            None
            if execution_store is None
            else execution_store.summary(side=Side.BUY)
        ),
        execution_exits=(
            None
            if execution_store is None
            else execution_store.summary(side=Side.SELL)
        ),
    )


def _average(values: tuple[Decimal, ...]) -> Decimal | None:
    if not values:
        return None
    return sum(values, start=Decimal("0")) / Decimal(len(values))


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
