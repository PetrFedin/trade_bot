from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.marketdata.bybit_research_universe import BybitResearchInstrument

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True, order=True)
class BybitFullPeriod5mWorkItem:
    archive_date: date
    symbol: str

    def validate(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("full-period 5m work-item symbol is invalid")
        if not self.symbol.endswith("USDT") or not self.symbol.isalnum():
            raise ValueError("full-period 5m work-item requires normalized USDT symbol")


@dataclass(frozen=True)
class BybitFullPeriod5mSymbolCoverage:
    symbol: str
    launch_date: date
    last_archive_date: date
    expected_dates: tuple[date, ...]
    completed_dates: tuple[date, ...]
    blocked_dates: tuple[date, ...]
    pending_dates: tuple[date, ...]

    def validate(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("full-period 5m coverage symbol is invalid")
        if self.last_archive_date < self.launch_date:
            raise ValueError("full-period 5m coverage interval is empty")
        for name, values in (
            ("expected_dates", self.expected_dates),
            ("completed_dates", self.completed_dates),
            ("blocked_dates", self.blocked_dates),
            ("pending_dates", self.pending_dates),
        ):
            if tuple(sorted(set(values))) != values:
                raise ValueError(f"full-period 5m {name} must be unique and chronological")
        expected = set(self.expected_dates)
        completed = set(self.completed_dates)
        blocked = set(self.blocked_dates)
        pending = set(self.pending_dates)
        if not expected:
            raise ValueError("full-period 5m coverage needs expected dates")
        if self.expected_dates[0] != self.launch_date:
            raise ValueError("full-period 5m expected coverage must start on launch date")
        if self.expected_dates[-1] != self.last_archive_date:
            raise ValueError("full-period 5m expected coverage must end on latest archive day")
        if not completed <= expected or not blocked <= expected or not pending <= expected:
            raise ValueError("full-period 5m coverage contains out-of-range dates")
        if completed & blocked or completed & pending or blocked & pending:
            raise ValueError("full-period 5m coverage states must be disjoint")
        if completed | blocked | pending != expected:
            raise ValueError("full-period 5m coverage states must partition expected dates")

    @property
    def expected_day_count(self) -> int:
        return len(self.expected_dates)

    @property
    def completed_day_count(self) -> int:
        return len(self.completed_dates)

    @property
    def blocked_day_count(self) -> int:
        return len(self.blocked_dates)

    @property
    def pending_day_count(self) -> int:
        return len(self.pending_dates)

    @property
    def coverage_fraction(self) -> Decimal:
        if self.expected_day_count == 0:
            return _ZERO
        return Decimal(self.completed_day_count) / Decimal(self.expected_day_count)

    @property
    def full_period_complete(self) -> bool:
        return self.completed_day_count == self.expected_day_count

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "symbol": self.symbol,
            "launch_date": self.launch_date.isoformat(),
            "last_archive_date": self.last_archive_date.isoformat(),
            "expected_day_count": self.expected_day_count,
            "completed_day_count": self.completed_day_count,
            "blocked_day_count": self.blocked_day_count,
            "pending_day_count": self.pending_day_count,
            "coverage_fraction": str(self.coverage_fraction),
            "full_period_complete": self.full_period_complete,
            "first_pending_date": (
                None if not self.pending_dates else self.pending_dates[0].isoformat()
            ),
            "first_blocked_date": (
                None if not self.blocked_dates else self.blocked_dates[0].isoformat()
            ),
        }


@dataclass(frozen=True)
class BybitFullPeriod5mCoveragePlan:
    observed_at: str
    last_archive_date: date
    symbols: tuple[str, ...]
    coverage: tuple[BybitFullPeriod5mSymbolCoverage, ...]
    parameter_retuning_performed: bool = False
    strategy_promotion_allowed: bool = False
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False
    bybit_live_order_routing_allowed: bool = False

    def validate(self) -> None:
        observed = datetime.fromisoformat(self.observed_at)
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("full-period 5m plan observed_at must be timezone-aware")
        if tuple(sorted(set(self.symbols))) != self.symbols:
            raise ValueError("full-period 5m plan symbols must be sorted and unique")
        if not self.symbols:
            raise ValueError("full-period 5m plan requires symbols")
        if tuple(item.symbol for item in self.coverage) != self.symbols:
            raise ValueError("full-period 5m plan coverage does not match symbols")
        for item in self.coverage:
            item.validate()
            if item.last_archive_date != self.last_archive_date:
                raise ValueError("full-period 5m plan has inconsistent archive cutoff")
        if (
            self.parameter_retuning_performed
            or self.strategy_promotion_allowed
            or self.demo_activation_allowed
            or self.live_activation_allowed
            or self.bybit_live_order_routing_allowed
        ):
            raise ValueError("full-period 5m coverage cannot activate trading")

    @property
    def expected_day_count(self) -> int:
        return sum(item.expected_day_count for item in self.coverage)

    @property
    def completed_day_count(self) -> int:
        return sum(item.completed_day_count for item in self.coverage)

    @property
    def blocked_day_count(self) -> int:
        return sum(item.blocked_day_count for item in self.coverage)

    @property
    def pending_day_count(self) -> int:
        return sum(item.pending_day_count for item in self.coverage)

    @property
    def coverage_fraction(self) -> Decimal:
        if self.expected_day_count == 0:
            return _ZERO
        return Decimal(self.completed_day_count) / Decimal(self.expected_day_count)

    @property
    def full_period_complete(self) -> bool:
        return bool(self.coverage) and all(
            item.full_period_complete for item in self.coverage
        )

    def next_work_items(self, *, limit: int) -> tuple[BybitFullPeriod5mWorkItem, ...]:
        self.validate()
        if isinstance(limit, bool) or not 1 <= limit <= 5000:
            raise ValueError("full-period 5m work limit must be within [1, 5000]")
        work = sorted(
            BybitFullPeriod5mWorkItem(archive_date=value, symbol=item.symbol)
            for item in self.coverage
            for value in item.pending_dates
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
            "schema": "BYBIT_FULL_PERIOD_5M_COVERAGE_PLAN_V113",
            "observed_at": self.observed_at,
            "last_archive_date": self.last_archive_date.isoformat(),
            "symbols": list(self.symbols),
            "expected_day_count": self.expected_day_count,
            "completed_day_count": self.completed_day_count,
            "blocked_day_count": self.blocked_day_count,
            "pending_day_count": self.pending_day_count,
            "coverage_fraction": str(self.coverage_fraction),
            "full_period_complete": self.full_period_complete,
            "coverage": [item.to_payload() for item in self.coverage],
            "full_period_claim_allowed": self.full_period_complete,
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


def build_bybit_full_period_5m_plan(
    instruments: Sequence[BybitResearchInstrument],
    *,
    symbols: Sequence[str],
    observed_at: datetime,
    completed_by_symbol: Mapping[str, Sequence[date]] | None = None,
    unavailable_retry_after_by_symbol: Mapping[
        str, Mapping[date, datetime]
    ] | None = None,
) -> BybitFullPeriod5mCoveragePlan:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("full-period 5m observed_at must be timezone-aware")
    cutoff = observed_at.astimezone(UTC)
    last_archive_date = cutoff.date() - timedelta(days=1)
    normalized_symbols = tuple(sorted(symbols))
    if not normalized_symbols or tuple(sorted(set(symbols))) != normalized_symbols:
        raise ValueError("full-period 5m symbols must be sorted and unique")
    by_symbol = {item.symbol: item for item in instruments}
    if any(symbol not in by_symbol for symbol in normalized_symbols):
        raise ValueError("full-period 5m symbol is missing instrument metadata")
    completed_source = {} if completed_by_symbol is None else completed_by_symbol
    unavailable_source = (
        {} if unavailable_retry_after_by_symbol is None else unavailable_retry_after_by_symbol
    )
    coverage: list[BybitFullPeriod5mSymbolCoverage] = []
    for symbol in normalized_symbols:
        instrument = by_symbol[symbol]
        launch = datetime.fromtimestamp(instrument.launch_time_ms / 1000, tz=UTC).date()
        if launch > last_archive_date:
            raise ValueError("full-period 5m symbol has no completed archive days")
        expected = _date_range(launch, last_archive_date)
        expected_set = set(expected)
        completed = tuple(sorted(set(completed_source.get(symbol, ()))))
        if not set(completed) <= expected_set:
            raise ValueError("full-period 5m completed dates exceed expected interval")
        unavailable = unavailable_source.get(symbol, {})
        if any(value.tzinfo is None or value.utcoffset() is None for value in unavailable.values()):
            raise ValueError("full-period 5m retry timestamps must be timezone-aware")
        if not set(unavailable) <= expected_set:
            raise ValueError("full-period 5m unavailable dates exceed expected interval")
        blocked = tuple(
            sorted(
                archive_date
                for archive_date, retry_after in unavailable.items()
                if archive_date not in completed
                and retry_after.astimezone(UTC) > cutoff
            )
        )
        completed_set = set(completed)
        blocked_set = set(blocked)
        pending = tuple(
            value
            for value in expected
            if value not in completed_set and value not in blocked_set
        )
        item = BybitFullPeriod5mSymbolCoverage(
            symbol=symbol,
            launch_date=launch,
            last_archive_date=last_archive_date,
            expected_dates=expected,
            completed_dates=completed,
            blocked_dates=blocked,
            pending_dates=pending,
        )
        item.validate()
        coverage.append(item)
    plan = BybitFullPeriod5mCoveragePlan(
        observed_at=cutoff.isoformat(),
        last_archive_date=last_archive_date,
        symbols=normalized_symbols,
        coverage=tuple(coverage),
    )
    plan.validate()
    return plan


def _date_range(first: date, last: date) -> tuple[date, ...]:
    if last < first:
        return ()
    return tuple(
        first + timedelta(days=offset)
        for offset in range((last - first).days + 1)
    )
