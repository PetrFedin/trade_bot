from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from app.marketdata.bybit_derivatives_history import (
    BybitAccountRatioPoint,
    BybitDerivativesHistory,
    BybitHistoricalFundingPoint,
    BybitOpenInterestPoint,
)
from app.marketdata.bybit_research_universe import BybitResearchInstrument
from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_derivatives_context import build_crypto_trade_derivatives_context
from app.strategy.crypto_historical_diagnostics import (
    build_crypto_historical_trade_conditions,
)
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_profit_runner import CryptoProfitRunnerPolicy
from app.strategy.crypto_runner_admission import CryptoRunnerAdmissionPolicy
from app.strategy.crypto_strategy_evidence_matrix import (
    CryptoStrategyEvidenceRow,
    build_crypto_strategy_evidence_rows,
    build_crypto_trade_execution_economics,
)
from app.strategy.crypto_trade_management import CryptoProtectionPolicy
from tools.replay_bybit_crypto_single_symbol import (
    replay_open_ended_crypto_runner_single_symbol,
)

_FIVE_MINUTES = timedelta(minutes=5)
_FIVE_MINUTES_MS = 5 * 60 * 1000
_RUNNER_EDGE_MULTIPLE = Decimal("1.50")


@dataclass(frozen=True)
class ArchivedBybitDerivativesHistoryView:
    """Archive-native history view compatible with point-in-time strategy joins."""

    symbol: str
    start_ms: int
    end_ms: int
    interval: str
    open_interest: tuple[BybitOpenInterestPoint, ...]
    account_ratio: tuple[BybitAccountRatioPoint, ...]
    funding: tuple[BybitHistoricalFundingPoint, ...]

    def validate(self) -> None:
        if (
            not self.symbol
            or self.symbol != self.symbol.strip().upper()
            or not self.symbol.endswith("USDT")
            or not self.symbol.isalnum()
        ):
            raise ValueError("archived derivatives history symbol is invalid")
        if self.interval != "5min":
            raise ValueError("archived derivatives history requires 5min decision context")
        if self.start_ms < 0 or self.end_ms <= self.start_ms:
            raise ValueError("archived derivatives history interval is invalid")
        for points in (self.open_interest, self.account_ratio, self.funding):
            previous: int | None = None
            for point in points:
                point.validate()
                if point.symbol != self.symbol:
                    raise ValueError("archived derivatives history contains another symbol")
                if not self.start_ms <= point.timestamp_ms <= self.end_ms:
                    raise ValueError("archived derivatives point falls outside history interval")
                if previous is not None and point.timestamp_ms <= previous:
                    raise ValueError("archived derivatives points must be unique and ordered")
                previous = point.timestamp_ms


def build_source_common_period_symbol_evidence_rows(
    instrument: BybitResearchInstrument,
    *,
    bars: Sequence[BybitKlineBar],
    open_interest: Sequence[BybitOpenInterestPoint],
    account_ratio: Sequence[BybitAccountRatioPoint],
    funding: Sequence[BybitHistoricalFundingPoint],
    common_start_at: datetime,
    end_exclusive_at: datetime,
    opening_equity_usdt: Decimal = Decimal("1000"),
    strategy_config: CryptoPerpStrategyConfig | None = None,
) -> tuple[tuple[CryptoStrategyEvidenceRow, ...], dict[str, Any]]:
    """Build fixed-strategy rows over the longest period shared by all required sources."""

    start = _utc(common_start_at)
    end = _utc(end_exclusive_at)
    if end <= start:
        raise ValueError("source-common evidence interval is invalid")
    if not opening_equity_usdt.is_finite() or opening_equity_usdt <= 0:
        raise ValueError("source-common evidence opening equity must be positive and finite")
    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    if config != CryptoPerpStrategyConfig():
        raise ValueError("source-common evidence requires the qualified fixed strategy config")

    normalized_bars = tuple(bars)
    _validate_price_grid(
        instrument.symbol,
        normalized_bars,
        start_at=start,
        end_exclusive_at=end,
    )
    acquisition = BybitKlineAcquisition(
        bars=normalized_bars,
        pages_by_symbol={instrument.symbol: 1},
    )
    acquisition.validate(requested_symbols=(instrument.symbol,), minimum_bars=60)
    replay = replay_open_ended_crypto_runner_single_symbol(
        acquisition,
        opening_equity_usdt=opening_equity_usdt,
        base_config=config,
        protection_policy=CryptoProtectionPolicy(),
        runner_policy=CryptoProfitRunnerPolicy(),
        runner_admission_policy=CryptoRunnerAdmissionPolicy(
            minimum_expected_edge_multiple=_RUNNER_EDGE_MULTIPLE
        ),
        interval="5",
    )
    _validate_replay_boundary(replay)

    archive_history = ArchivedBybitDerivativesHistoryView(
        symbol=instrument.symbol,
        start_ms=int(start.timestamp() * 1000),
        end_ms=int(end.timestamp() * 1000) - 1,
        interval="5min",
        open_interest=tuple(open_interest),
        account_ratio=tuple(account_ratio),
        funding=tuple(funding),
    )
    archive_history.validate()
    compatibility_history = cast(BybitDerivativesHistory, archive_history)
    conditions = build_crypto_historical_trade_conditions(
        acquisition,
        replay,
        strategy_config=config,
    )
    derivatives = build_crypto_trade_derivatives_context(
        replay,
        {instrument.symbol: compatibility_history},
    )
    economics = build_crypto_trade_execution_economics(
        replay,
        strategy_config=config,
    )
    rows = build_crypto_strategy_evidence_rows(
        conditions,
        derivatives,
        economics,
        strategy_config=config,
    )
    if len(rows) != len(conditions) or len(rows) != len(derivatives):
        raise ValueError("source-common evidence row joins are incomplete")
    complete_context_count = sum(
        1 for item in derivatives if item.decision_context_complete
    )
    summary = {
        "symbol": instrument.symbol,
        "common_start_at": start.isoformat(),
        "end_exclusive_at": end.isoformat(),
        "bar_count": len(normalized_bars),
        "open_interest_point_count": len(open_interest),
        "account_ratio_point_count": len(account_ratio),
        "funding_event_count": len(funding),
        "closed_trade_count": len(rows),
        "complete_decision_context_count": complete_context_count,
        "incomplete_decision_context_count": len(rows) - complete_context_count,
        "scope": "MAX_SOURCE_AVAILABLE_COMMON_PERIOD",
        "price_grid_complete": True,
        "strategy_parameters_changed": False,
        "parameter_retuning_performed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }
    return rows, summary


def _validate_price_grid(
    symbol: str,
    bars: Sequence[BybitKlineBar],
    *,
    start_at: datetime,
    end_exclusive_at: datetime,
) -> None:
    first_expected = _ceil_five_minutes(start_at)
    if first_expected >= end_exclusive_at:
        raise ValueError("source-common price grid is empty")
    expected_count = int((end_exclusive_at - first_expected) / _FIVE_MINUTES)
    if len(bars) != expected_count:
        raise ValueError(
            "source-common price grid count mismatch:"
            f"actual={len(bars)}:expected={expected_count}"
        )
    for index, bar in enumerate(bars):
        bar.validate()
        if bar.symbol != symbol:
            raise ValueError("source-common price grid contains another symbol")
        expected = first_expected + index * _FIVE_MINUTES
        if bar.start_time.astimezone(UTC) != expected:
            raise ValueError(
                "source-common price grid timestamp mismatch:"
                f"actual={bar.start_time.isoformat()}:expected={expected.isoformat()}"
            )


def _validate_replay_boundary(replay: Mapping[str, Any]) -> None:
    for field in (
        "strategy_promotion_allowed",
        "bybit_demo_order_writes_enabled",
        "bybit_live_order_routing_allowed",
    ):
        if replay.get(field) is not False:
            raise ValueError(f"source-common replay requires explicit {field}=false")


def _ceil_five_minutes(value: datetime) -> datetime:
    utc = _utc(value)
    epoch_ms = int(utc.timestamp() * 1000)
    ceiled = ((epoch_ms + _FIVE_MINUTES_MS - 1) // _FIVE_MINUTES_MS) * _FIVE_MINUTES_MS
    return datetime.fromtimestamp(ceiled / 1000, tz=UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("source-common evidence timestamp must be timezone-aware")
    return value.astimezone(UTC)
