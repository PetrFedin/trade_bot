from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class StrategyQualityStatus(StrEnum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    HEALTHY = "HEALTHY"
    PAUSE_ENTRIES = "PAUSE_ENTRIES"


@dataclass(frozen=True)
class TradeQualityObservation:
    net_pnl: Decimal
    maximum_favorable_excursion_fraction: Decimal
    mfe_capture_ratio: Decimal | None
    exit_reason: str

    def validate(self) -> None:
        if not self.net_pnl.is_finite():
            raise ValueError("trade quality net_pnl must be finite")
        if (
            not self.maximum_favorable_excursion_fraction.is_finite()
            or self.maximum_favorable_excursion_fraction < 0
        ):
            raise ValueError("trade quality MFE must be finite and non-negative")
        if self.mfe_capture_ratio is not None and not self.mfe_capture_ratio.is_finite():
            raise ValueError("trade quality MFE capture must be finite when supplied")
        if not self.exit_reason:
            raise ValueError("trade quality exit_reason is required")


@dataclass(frozen=True)
class TradeQualityMonitorPolicy:
    window_trades: int
    minimum_observations: int
    minimum_profit_factor: Decimal
    minimum_profit_preservation_rate: Decimal
    minimum_average_mfe_capture_ratio: Decimal
    maximum_hard_stop_fraction: Decimal
    maximum_consecutive_losses: int
    allow_entries_when_insufficient_data: bool = False
    hard_stop_reasons: tuple[str, ...] = (
        "STOP_LOSS",
        "HARD_STOP",
        "INTRABAR_HARD_STOP",
    )

    def validate(self) -> None:
        if self.window_trades < 1:
            raise ValueError("window_trades must be positive")
        if self.minimum_observations < 1:
            raise ValueError("minimum_observations must be positive")
        if self.minimum_observations > self.window_trades:
            raise ValueError("minimum_observations cannot exceed window_trades")
        if (
            not self.minimum_profit_factor.is_finite()
            or self.minimum_profit_factor < 0
        ):
            raise ValueError("minimum_profit_factor must be finite and non-negative")
        if (
            not self.minimum_profit_preservation_rate.is_finite()
            or self.minimum_profit_preservation_rate < 0
            or self.minimum_profit_preservation_rate > 1
        ):
            raise ValueError(
                "minimum_profit_preservation_rate must be finite and within [0, 1]"
            )
        if not self.minimum_average_mfe_capture_ratio.is_finite():
            raise ValueError("minimum_average_mfe_capture_ratio must be finite")
        if (
            not self.maximum_hard_stop_fraction.is_finite()
            or self.maximum_hard_stop_fraction < 0
            or self.maximum_hard_stop_fraction > 1
        ):
            raise ValueError(
                "maximum_hard_stop_fraction must be finite and within [0, 1]"
            )
        if self.maximum_consecutive_losses < 1:
            raise ValueError("maximum_consecutive_losses must be positive")
        if not self.hard_stop_reasons or any(not reason for reason in self.hard_stop_reasons):
            raise ValueError("hard_stop_reasons must contain non-empty reason codes")


@dataclass(frozen=True)
class TradeQualityWindow:
    observation_count: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    gross_profit: Decimal
    gross_loss: Decimal
    total_pnl: Decimal
    win_rate: Decimal
    profit_factor: Decimal | None
    positive_mfe_trades: int
    positive_mfe_closed_profitable: int
    profit_preservation_rate: Decimal | None
    average_mfe_capture_ratio: Decimal | None
    hard_stop_fraction: Decimal
    current_consecutive_losses: int


@dataclass(frozen=True)
class StrategyQualityGateDecision:
    status: StrategyQualityStatus
    allow_new_entries: bool
    allow_exits: bool
    reasons: tuple[str, ...]
    metrics: TradeQualityWindow


def _window(
    observations: tuple[TradeQualityObservation, ...],
    policy: TradeQualityMonitorPolicy,
) -> tuple[TradeQualityObservation, ...]:
    for observation in observations:
        observation.validate()
    return observations[-policy.window_trades :]


def compute_trade_quality_window(
    observations: tuple[TradeQualityObservation, ...],
    *,
    policy: TradeQualityMonitorPolicy,
) -> TradeQualityWindow:
    policy.validate()
    values = _window(observations, policy)
    count = len(values)
    wins = sum(item.net_pnl > 0 for item in values)
    losses = sum(item.net_pnl < 0 for item in values)
    breakeven = count - wins - losses
    gross_profit = sum(
        (item.net_pnl for item in values if item.net_pnl > 0), Decimal("0")
    )
    gross_loss = sum(
        (item.net_pnl for item in values if item.net_pnl < 0), Decimal("0")
    )
    if gross_loss < 0:
        profit_factor: Decimal | None = gross_profit / abs(gross_loss)
    elif gross_profit > 0:
        profit_factor = None
    else:
        profit_factor = Decimal("0")
    win_rate = Decimal(wins) / Decimal(count) if count else Decimal("0")

    positive_mfe = [
        item for item in values if item.maximum_favorable_excursion_fraction > 0
    ]
    preserved = sum(item.net_pnl > 0 for item in positive_mfe)
    preservation_rate = (
        Decimal(preserved) / Decimal(len(positive_mfe)) if positive_mfe else None
    )
    captures = [
        item.mfe_capture_ratio
        for item in values
        if item.mfe_capture_ratio is not None
    ]
    average_capture = (
        sum((value for value in captures if value is not None), Decimal("0"))
        / Decimal(len(captures))
        if captures
        else None
    )
    hard_stops = sum(item.exit_reason in policy.hard_stop_reasons for item in values)
    hard_stop_fraction = (
        Decimal(hard_stops) / Decimal(count) if count else Decimal("0")
    )
    consecutive_losses = 0
    for item in reversed(values):
        if item.net_pnl < 0:
            consecutive_losses += 1
        else:
            break

    return TradeQualityWindow(
        observation_count=count,
        winning_trades=wins,
        losing_trades=losses,
        breakeven_trades=breakeven,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        total_pnl=gross_profit + gross_loss,
        win_rate=win_rate,
        profit_factor=profit_factor,
        positive_mfe_trades=len(positive_mfe),
        positive_mfe_closed_profitable=preserved,
        profit_preservation_rate=preservation_rate,
        average_mfe_capture_ratio=average_capture,
        hard_stop_fraction=hard_stop_fraction,
        current_consecutive_losses=consecutive_losses,
    )


def evaluate_strategy_quality_gate(
    observations: tuple[TradeQualityObservation, ...],
    *,
    policy: TradeQualityMonitorPolicy,
) -> StrategyQualityGateDecision:
    metrics = compute_trade_quality_window(observations, policy=policy)
    if metrics.observation_count < policy.minimum_observations:
        return StrategyQualityGateDecision(
            status=StrategyQualityStatus.INSUFFICIENT_DATA,
            allow_new_entries=policy.allow_entries_when_insufficient_data,
            allow_exits=True,
            reasons=("INSUFFICIENT_OBSERVATIONS",),
            metrics=metrics,
        )

    reasons: list[str] = []
    if (
        metrics.profit_factor is not None
        and metrics.profit_factor < policy.minimum_profit_factor
    ):
        reasons.append("PROFIT_FACTOR_BELOW_MINIMUM")
    if metrics.profit_preservation_rate is None:
        reasons.append("PROFIT_PRESERVATION_UNAVAILABLE")
    elif metrics.profit_preservation_rate < policy.minimum_profit_preservation_rate:
        reasons.append("PROFIT_PRESERVATION_BELOW_MINIMUM")
    if metrics.average_mfe_capture_ratio is None:
        reasons.append("MFE_CAPTURE_UNAVAILABLE")
    elif metrics.average_mfe_capture_ratio < policy.minimum_average_mfe_capture_ratio:
        reasons.append("MFE_CAPTURE_BELOW_MINIMUM")
    if metrics.hard_stop_fraction > policy.maximum_hard_stop_fraction:
        reasons.append("HARD_STOP_FRACTION_ABOVE_MAXIMUM")
    if metrics.current_consecutive_losses >= policy.maximum_consecutive_losses:
        reasons.append("CONSECUTIVE_LOSS_LIMIT_REACHED")

    paused = bool(reasons)
    return StrategyQualityGateDecision(
        status=(
            StrategyQualityStatus.PAUSE_ENTRIES
            if paused
            else StrategyQualityStatus.HEALTHY
        ),
        allow_new_entries=not paused,
        allow_exits=True,
        reasons=tuple(reasons),
        metrics=metrics,
    )
