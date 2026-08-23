from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.marketdata.bybit_derivatives_history import (
    BybitAccountRatioPoint,
    BybitHistoricalFundingPoint,
    BybitOpenInterestPoint,
)
from app.marketdata.bybit_research_universe import BybitResearchInstrument

OPEN_INTEREST = "OPEN_INTEREST"
ACCOUNT_RATIO = "ACCOUNT_RATIO"
FUNDING = "FUNDING"
DERIVATIVES_SOURCES = (OPEN_INTEREST, ACCOUNT_RATIO, FUNDING)
_ACCOUNT_RATIO_SOURCE_FLOOR = datetime(2020, 7, 20, tzinfo=UTC)
_FIVE_MINUTES = timedelta(minutes=5)
_FIVE_MINUTES_MS = 5 * 60 * 1000
_ZERO = Decimal("0")


@dataclass(frozen=True, order=True)
class BybitFullPeriodDerivativesWorkItem:
    archive_date: date
    source: str
    symbol: str

    def validate(self) -> None:
        _validate_source(self.source)
        _validate_symbol(self.symbol)


@dataclass(frozen=True)
class BybitDerivativesSourceDayAudit:
    source: str
    symbol: str
    archive_date: date
    query_start_at: str
    query_end_at: str
    expected_point_count: int | None
    actual_point_count: int
    missing_point_count: int
    extra_point_count: int
    first_missing_at: str | None
    first_extra_at: str | None
    query_window_complete: bool
    exact_grid_required: bool

    def validate(self) -> None:
        _validate_source(self.source)
        _validate_symbol(self.symbol)
        start = _parse_time(self.query_start_at)
        end = _parse_time(self.query_end_at)
        if end <= start:
            raise ValueError("derivatives source-day audit interval is invalid")
        if start.date() != self.archive_date:
            raise ValueError("derivatives source-day audit start date is inconsistent")
        if self.actual_point_count < 0:
            raise ValueError("derivatives source-day actual point count cannot be negative")
        if self.missing_point_count < 0 or self.extra_point_count < 0:
            raise ValueError("derivatives source-day gap counts cannot be negative")
        if self.exact_grid_required:
            if self.expected_point_count is None or self.expected_point_count < 0:
                raise ValueError("fixed-grid derivatives audit requires expected point count")
        elif self.expected_point_count is not None:
            raise ValueError("event-series derivatives audit cannot assert fixed point count")
        if self.missing_point_count == 0 and self.first_missing_at is not None:
            raise ValueError("derivatives source-day missing timestamp is inconsistent")
        if self.missing_point_count > 0 and self.first_missing_at is None:
            raise ValueError("derivatives source-day missing timestamp is required")
        if self.extra_point_count == 0 and self.first_extra_at is not None:
            raise ValueError("derivatives source-day extra timestamp is inconsistent")
        if self.extra_point_count > 0 and self.first_extra_at is None:
            raise ValueError("derivatives source-day extra timestamp is required")
        if self.first_missing_at is not None:
            _parse_time(self.first_missing_at)
        if self.first_extra_at is not None:
            _parse_time(self.first_extra_at)

    @property
    def complete(self) -> bool:
        return (
            self.query_window_complete
            and self.missing_point_count == 0
            and self.extra_point_count == 0
        )

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "source": self.source,
            "symbol": self.symbol,
            "archive_date": self.archive_date.isoformat(),
            "query_start_at": self.query_start_at,
            "query_end_at": self.query_end_at,
            "expected_point_count": self.expected_point_count,
            "actual_point_count": self.actual_point_count,
            "missing_point_count": self.missing_point_count,
            "extra_point_count": self.extra_point_count,
            "first_missing_at": self.first_missing_at,
            "first_extra_at": self.first_extra_at,
            "query_window_complete": self.query_window_complete,
            "exact_grid_required": self.exact_grid_required,
            "complete": self.complete,
        }


@dataclass(frozen=True)
class BybitDerivativesSourceCoverage:
    source: str
    symbol: str
    instrument_launch_at: str
    source_start_at: str
    last_archive_date: date
    expected_dates: tuple[date, ...]
    completed_dates: tuple[date, ...]
    blocked_dates: tuple[date, ...]
    pending_dates: tuple[date, ...]
    lifetime_truncated_by_source_floor: bool

    def validate(self) -> None:
        _validate_source(self.source)
        _validate_symbol(self.symbol)
        launch = _parse_time(self.instrument_launch_at)
        source_start = _parse_time(self.source_start_at)
        if source_start < launch:
            raise ValueError("derivatives source coverage cannot start before listing")
        expected = _validate_date_partition(
            self.expected_dates,
            self.completed_dates,
            self.blocked_dates,
            self.pending_dates,
            label="derivatives source coverage",
        )
        if not expected:
            raise ValueError("derivatives source coverage requires expected dates")
        if expected[0] != source_start.date():
            raise ValueError("derivatives source coverage starts on wrong date")
        if expected[-1] != self.last_archive_date:
            raise ValueError("derivatives source coverage ends on wrong date")
        if self.lifetime_truncated_by_source_floor != (source_start > launch):
            raise ValueError("derivatives source-floor truncation flag is inconsistent")
        if self.source != ACCOUNT_RATIO and self.lifetime_truncated_by_source_floor:
            raise ValueError("only account-ratio has a documented post-listing source floor")

    @property
    def expected_day_count(self) -> int:
        return len(self.expected_dates)

    @property
    def completed_day_count(self) -> int:
        return len(self.completed_dates)

    @property
    def coverage_fraction(self) -> Decimal:
        if not self.expected_dates:
            return _ZERO
        return Decimal(len(self.completed_dates)) / Decimal(len(self.expected_dates))

    @property
    def source_available_period_complete(self) -> bool:
        return len(self.completed_dates) == len(self.expected_dates)

    @property
    def instrument_lifetime_complete(self) -> bool:
        return (
            self.source_available_period_complete
            and not self.lifetime_truncated_by_source_floor
        )

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "source": self.source,
            "symbol": self.symbol,
            "instrument_launch_at": self.instrument_launch_at,
            "source_start_at": self.source_start_at,
            "last_archive_date": self.last_archive_date.isoformat(),
            "expected_day_count": self.expected_day_count,
            "completed_day_count": self.completed_day_count,
            "blocked_day_count": len(self.blocked_dates),
            "pending_day_count": len(self.pending_dates),
            "coverage_fraction": str(self.coverage_fraction),
            "source_available_period_complete": self.source_available_period_complete,
            "lifetime_truncated_by_source_floor": self.lifetime_truncated_by_source_floor,
            "instrument_lifetime_complete": self.instrument_lifetime_complete,
            "first_pending_date": (
                None if not self.pending_dates else self.pending_dates[0].isoformat()
            ),
            "first_blocked_date": (
                None if not self.blocked_dates else self.blocked_dates[0].isoformat()
            ),
        }


@dataclass(frozen=True)
class BybitFullPeriodDerivativesSymbolCoverage:
    symbol: str
    instrument_launch_at: str
    last_archive_date: date
    sources: tuple[BybitDerivativesSourceCoverage, ...]

    def validate(self) -> None:
        _validate_symbol(self.symbol)
        launch = _parse_time(self.instrument_launch_at)
        if self.last_archive_date < launch.date():
            raise ValueError("derivatives symbol coverage ends before listing")
        if tuple(item.source for item in self.sources) != DERIVATIVES_SOURCES:
            raise ValueError("derivatives symbol coverage sources are incomplete or unordered")
        for item in self.sources:
            item.validate()
            if item.symbol != self.symbol:
                raise ValueError("derivatives symbol coverage contains another symbol")
            if item.instrument_launch_at != self.instrument_launch_at:
                raise ValueError("derivatives symbol coverage launch time mismatch")
            if item.last_archive_date != self.last_archive_date:
                raise ValueError("derivatives symbol coverage cutoff mismatch")

    @property
    def source_available_common_start_at(self) -> str:
        starts = (_parse_time(item.source_start_at) for item in self.sources)
        return max(starts).isoformat()

    @property
    def source_available_period_complete(self) -> bool:
        return all(item.source_available_period_complete for item in self.sources)

    @property
    def instrument_lifetime_derivatives_complete(self) -> bool:
        return all(item.instrument_lifetime_complete for item in self.sources)

    @property
    def full_period_evidence_matrix_allowed(self) -> bool:
        return self.instrument_lifetime_derivatives_complete

    @property
    def source_available_common_period_matrix_allowed(self) -> bool:
        return self.source_available_period_complete

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "symbol": self.symbol,
            "instrument_launch_at": self.instrument_launch_at,
            "last_archive_date": self.last_archive_date.isoformat(),
            "source_available_common_start_at": self.source_available_common_start_at,
            "source_available_period_complete": self.source_available_period_complete,
            "instrument_lifetime_derivatives_complete": (
                self.instrument_lifetime_derivatives_complete
            ),
            "full_period_evidence_matrix_allowed": self.full_period_evidence_matrix_allowed,
            "source_available_common_period_matrix_allowed": (
                self.source_available_common_period_matrix_allowed
            ),
            "sources": [item.to_payload() for item in self.sources],
        }


@dataclass(frozen=True)
class BybitFullPeriodDerivativesCoveragePlan:
    observed_at: str
    last_archive_date: date
    symbols: tuple[str, ...]
    coverage: tuple[BybitFullPeriodDerivativesSymbolCoverage, ...]
    parameter_retuning_performed: bool = False
    strategy_promotion_allowed: bool = False
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False
    bybit_live_order_routing_allowed: bool = False

    def validate(self) -> None:
        _parse_time(self.observed_at)
        if tuple(sorted(set(self.symbols))) != self.symbols or not self.symbols:
            raise ValueError("derivatives coverage plan symbols must be sorted and unique")
        if tuple(item.symbol for item in self.coverage) != self.symbols:
            raise ValueError("derivatives coverage plan does not match symbols")
        for item in self.coverage:
            item.validate()
            if item.last_archive_date != self.last_archive_date:
                raise ValueError("derivatives coverage plan has inconsistent cutoff")
        if (
            self.parameter_retuning_performed
            or self.strategy_promotion_allowed
            or self.demo_activation_allowed
            or self.live_activation_allowed
            or self.bybit_live_order_routing_allowed
        ):
            raise ValueError("derivatives coverage plan cannot activate trading")

    @property
    def source_available_period_complete(self) -> bool:
        return bool(self.coverage) and all(
            item.source_available_period_complete for item in self.coverage
        )

    @property
    def instrument_lifetime_derivatives_complete(self) -> bool:
        return bool(self.coverage) and all(
            item.instrument_lifetime_derivatives_complete for item in self.coverage
        )

    @property
    def full_period_evidence_matrix_allowed(self) -> bool:
        return self.instrument_lifetime_derivatives_complete

    def next_work_items(self, *, limit: int) -> tuple[BybitFullPeriodDerivativesWorkItem, ...]:
        self.validate()
        if isinstance(limit, bool) or not 1 <= limit <= 5000:
            raise ValueError("derivatives coverage work limit must be within [1, 5000]")
        work = sorted(
            BybitFullPeriodDerivativesWorkItem(
                archive_date=value,
                source=source.source,
                symbol=symbol.symbol,
            )
            for symbol in self.coverage
            for source in symbol.sources
            for value in source.pending_dates
        )
        result = tuple(work[:limit])
        for item in result:
            item.validate()
        return result

    @property
    def plan_id(self) -> str:
        canonical = json.dumps(
            self.to_payload(include_plan_id=False),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def to_payload(self, *, include_plan_id: bool = True) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "schema": "BYBIT_FULL_PERIOD_DERIVATIVES_COVERAGE_PLAN_V114",
            "observed_at": self.observed_at,
            "last_archive_date": self.last_archive_date.isoformat(),
            "symbols": list(self.symbols),
            "coverage": [item.to_payload() for item in self.coverage],
            "source_available_period_complete": self.source_available_period_complete,
            "instrument_lifetime_derivatives_complete": (
                self.instrument_lifetime_derivatives_complete
            ),
            "full_period_evidence_matrix_allowed": self.full_period_evidence_matrix_allowed,
            "account_ratio_documented_source_floor_at": (
                _ACCOUNT_RATIO_SOURCE_FLOOR.isoformat()
            ),
            "parameter_retuning_performed": self.parameter_retuning_performed,
            "strategy_promotion_allowed": self.strategy_promotion_allowed,
            "demo_activation_allowed": self.demo_activation_allowed,
            "live_activation_allowed": self.live_activation_allowed,
            "bybit_live_order_routing_allowed": self.bybit_live_order_routing_allowed,
            "causal_claim_allowed": False,
            "predictive_guarantee_allowed": False,
        }
        if include_plan_id:
            payload["plan_id"] = self.plan_id
        return payload


def build_bybit_full_period_derivatives_plan(
    instruments: Sequence[BybitResearchInstrument],
    *,
    symbols: Sequence[str],
    observed_at: datetime,
    completed_by_source_symbol: Mapping[
        str, Mapping[str, Sequence[date]]
    ] | None = None,
    unavailable_retry_after_by_source_symbol: Mapping[
        str, Mapping[str, Mapping[date, datetime]]
    ] | None = None,
) -> BybitFullPeriodDerivativesCoveragePlan:
    cutoff = _utc(observed_at)
    last_archive_date = cutoff.date() - timedelta(days=1)
    normalized_symbols = tuple(sorted(symbols))
    if not normalized_symbols or tuple(sorted(set(symbols))) != normalized_symbols:
        raise ValueError("derivatives coverage symbols must be sorted and unique")
    instrument_map = {item.symbol: item for item in instruments}
    if any(symbol not in instrument_map for symbol in normalized_symbols):
        raise ValueError("derivatives coverage symbol is missing instrument metadata")
    completed_source = {} if completed_by_source_symbol is None else completed_by_source_symbol
    unavailable_source = (
        {}
        if unavailable_retry_after_by_source_symbol is None
        else unavailable_retry_after_by_source_symbol
    )
    result: list[BybitFullPeriodDerivativesSymbolCoverage] = []
    for symbol in normalized_symbols:
        instrument = instrument_map[symbol]
        launch = datetime.fromtimestamp(instrument.launch_time_ms / 1000, tz=UTC)
        if launch.date() > last_archive_date:
            raise ValueError("derivatives coverage symbol has no completed history days")
        source_rows: list[BybitDerivativesSourceCoverage] = []
        for source in DERIVATIVES_SOURCES:
            source_start = derivatives_source_start_at(instrument, source=source)
            expected = _date_range(source_start.date(), last_archive_date)
            expected_set = set(expected)
            completed = tuple(
                sorted(set(completed_source.get(source, {}).get(symbol, ())))
            )
            if not set(completed) <= expected_set:
                raise ValueError("derivatives completed dates exceed source interval")
            unavailable = unavailable_source.get(source, {}).get(symbol, {})
            if not set(unavailable) <= expected_set:
                raise ValueError("derivatives unavailable dates exceed source interval")
            if any(
                retry.tzinfo is None or retry.utcoffset() is None
                for retry in unavailable.values()
            ):
                raise ValueError("derivatives retry timestamps must be timezone-aware")
            completed_set = set(completed)
            blocked = tuple(
                sorted(
                    value
                    for value, retry in unavailable.items()
                    if value not in completed_set and _utc(retry) > cutoff
                )
            )
            blocked_set = set(blocked)
            pending = tuple(
                value
                for value in expected
                if value not in completed_set and value not in blocked_set
            )
            row = BybitDerivativesSourceCoverage(
                source=source,
                symbol=symbol,
                instrument_launch_at=launch.isoformat(),
                source_start_at=source_start.isoformat(),
                last_archive_date=last_archive_date,
                expected_dates=expected,
                completed_dates=completed,
                blocked_dates=blocked,
                pending_dates=pending,
                lifetime_truncated_by_source_floor=source_start > launch,
            )
            row.validate()
            source_rows.append(row)
        symbol_row = BybitFullPeriodDerivativesSymbolCoverage(
            symbol=symbol,
            instrument_launch_at=launch.isoformat(),
            last_archive_date=last_archive_date,
            sources=tuple(source_rows),
        )
        symbol_row.validate()
        result.append(symbol_row)
    plan = BybitFullPeriodDerivativesCoveragePlan(
        observed_at=cutoff.isoformat(),
        last_archive_date=last_archive_date,
        symbols=normalized_symbols,
        coverage=tuple(result),
    )
    plan.validate()
    return plan


def derivatives_source_start_at(
    instrument: BybitResearchInstrument,
    *,
    source: str,
) -> datetime:
    _validate_source(source)
    launch = datetime.fromtimestamp(instrument.launch_time_ms / 1000, tz=UTC)
    if source == ACCOUNT_RATIO:
        return max(launch, _ACCOUNT_RATIO_SOURCE_FLOOR)
    return launch


def audit_bybit_derivatives_source_day(
    instrument: BybitResearchInstrument,
    *,
    source: str,
    archive_date: date,
    points: Sequence[
        BybitOpenInterestPoint | BybitAccountRatioPoint | BybitHistoricalFundingPoint
    ],
    query_window_complete: bool = True,
) -> BybitDerivativesSourceDayAudit:
    _validate_source(source)
    source_start = derivatives_source_start_at(instrument, source=source)
    day_start = datetime.combine(archive_date, datetime.min.time(), tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    query_start = max(day_start, source_start)
    if query_start >= day_end:
        raise ValueError("derivatives source-day precedes source availability")
    actual_times: list[datetime] = []
    seen: set[int] = set()
    for point in points:
        point.validate()
        if point.symbol != instrument.symbol:
            raise ValueError("derivatives source-day contains another symbol")
        if source == OPEN_INTEREST and not isinstance(point, BybitOpenInterestPoint):
            raise ValueError("open-interest audit received wrong point type")
        if source == ACCOUNT_RATIO and not isinstance(point, BybitAccountRatioPoint):
            raise ValueError("account-ratio audit received wrong point type")
        if source == FUNDING and not isinstance(point, BybitHistoricalFundingPoint):
            raise ValueError("funding audit received wrong point type")
        if point.timestamp_ms in seen:
            raise ValueError("derivatives source-day contains duplicate timestamps")
        seen.add(point.timestamp_ms)
        moment = datetime.fromtimestamp(point.timestamp_ms / 1000, tz=UTC)
        actual_times.append(moment)
    if actual_times != sorted(actual_times):
        raise ValueError("derivatives source-day points must be chronological")

    exact_grid = source in (OPEN_INTEREST, ACCOUNT_RATIO)
    expected_times: tuple[datetime, ...] = ()
    if exact_grid:
        first = _ceil_five_minutes(query_start)
        expected_times = _time_range(first, day_end)
        expected_set = set(expected_times)
        actual_set = set(actual_times)
        missing = sorted(expected_set - actual_set)
        extra = sorted(actual_set - expected_set)
        expected_count: int | None = len(expected_times)
    else:
        missing = []
        extra = sorted(
            value for value in actual_times if value < query_start or value >= day_end
        )
        expected_count = None

    audit = BybitDerivativesSourceDayAudit(
        source=source,
        symbol=instrument.symbol,
        archive_date=archive_date,
        query_start_at=query_start.isoformat(),
        query_end_at=day_end.isoformat(),
        expected_point_count=expected_count,
        actual_point_count=len(actual_times),
        missing_point_count=len(missing),
        extra_point_count=len(extra),
        first_missing_at=None if not missing else missing[0].isoformat(),
        first_extra_at=None if not extra else extra[0].isoformat(),
        query_window_complete=query_window_complete,
        exact_grid_required=exact_grid,
    )
    audit.validate()
    return audit


def _validate_date_partition(
    expected_dates: tuple[date, ...],
    completed_dates: tuple[date, ...],
    blocked_dates: tuple[date, ...],
    pending_dates: tuple[date, ...],
    *,
    label: str,
) -> tuple[date, ...]:
    for name, values in (
        ("expected", expected_dates),
        ("completed", completed_dates),
        ("blocked", blocked_dates),
        ("pending", pending_dates),
    ):
        if tuple(sorted(set(values))) != values:
            raise ValueError(f"{label} {name} dates must be unique and chronological")
    expected = set(expected_dates)
    completed = set(completed_dates)
    blocked = set(blocked_dates)
    pending = set(pending_dates)
    if not completed <= expected or not blocked <= expected or not pending <= expected:
        raise ValueError(f"{label} contains out-of-range dates")
    if completed & blocked or completed & pending or blocked & pending:
        raise ValueError(f"{label} states must be disjoint")
    if completed | blocked | pending != expected:
        raise ValueError(f"{label} states must partition expected dates")
    return expected_dates


def _date_range(first: date, last: date) -> tuple[date, ...]:
    if last < first:
        return ()
    return tuple(
        first + timedelta(days=index)
        for index in range((last - first).days + 1)
    )


def _time_range(first: datetime, end_exclusive: datetime) -> tuple[datetime, ...]:
    if first >= end_exclusive:
        return ()
    count = int((end_exclusive - first) / _FIVE_MINUTES)
    return tuple(first + index * _FIVE_MINUTES for index in range(count))


def _ceil_five_minutes(value: datetime) -> datetime:
    utc = _utc(value)
    epoch_ms = int(utc.timestamp() * 1000)
    ceiled_ms = ((epoch_ms + _FIVE_MINUTES_MS - 1) // _FIVE_MINUTES_MS) * _FIVE_MINUTES_MS
    return datetime.fromtimestamp(ceiled_ms / 1000, tz=UTC)


def _validate_source(source: str) -> None:
    if source not in DERIVATIVES_SOURCES:
        raise ValueError("unsupported Bybit derivatives source")


def _validate_symbol(symbol: str) -> None:
    if (
        not symbol
        or symbol != symbol.strip().upper()
        or not symbol.endswith("USDT")
        or not symbol.isalnum()
    ):
        raise ValueError("derivatives coverage symbol must be normalized USDT")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("derivatives coverage timestamp must be timezone-aware")
    return value.astimezone(UTC)
