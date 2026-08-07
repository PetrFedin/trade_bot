from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from app.domain.trading import Bar


@dataclass(frozen=True)
class MarketDataPolicy:
    maximum_last_bar_age: timedelta = timedelta(minutes=2)
    maximum_gap: timedelta = timedelta(minutes=5)
    maximum_jump_fraction: Decimal = Decimal("0.25")

    def validate(self) -> None:
        if self.maximum_last_bar_age <= timedelta(0):
            raise ValueError("maximum_last_bar_age must be positive")
        if self.maximum_gap <= timedelta(0):
            raise ValueError("maximum_gap must be positive")
        if not self.maximum_jump_fraction.is_finite() or self.maximum_jump_fraction <= 0:
            raise ValueError("maximum_jump_fraction must be positive and finite")


@dataclass(frozen=True)
class MarketDataQuality:
    ready: bool
    reasons: tuple[str, ...]
    bars_checked: int
    last_timestamp: datetime | None


def validate_bar_series(
    bars: Sequence[Bar],
    *,
    now: datetime,
    policy: MarketDataPolicy = MarketDataPolicy(),
) -> MarketDataQuality:
    policy.validate()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not bars:
        return MarketDataQuality(False, ("NO_BARS",), 0, None)

    reasons: set[str] = set()
    symbol = bars[0].symbol
    previous: Bar | None = None
    seen: set[datetime] = set()
    for bar in bars:
        try:
            bar.validate()
        except ValueError:
            reasons.add("INVALID_BAR")
            continue
        if bar.symbol != symbol:
            reasons.add("MIXED_SYMBOLS")
        if bar.timestamp in seen:
            reasons.add("DUPLICATE_TIMESTAMP")
        seen.add(bar.timestamp)
        if bar.timestamp > now:
            reasons.add("FUTURE_BAR")
        if previous is not None:
            if bar.timestamp <= previous.timestamp:
                reasons.add("NON_MONOTONIC_TIME")
            elif bar.timestamp - previous.timestamp > policy.maximum_gap:
                reasons.add("BAR_GAP_EXCEEDED")
            if previous.close > 0:
                jump = abs(bar.close - previous.close) / previous.close
                if jump > policy.maximum_jump_fraction:
                    reasons.add("PRICE_JUMP_EXCEEDED")
        previous = bar

    last_timestamp = bars[-1].timestamp
    if last_timestamp <= now and now - last_timestamp > policy.maximum_last_bar_age:
        reasons.add("STALE_LAST_BAR")
    return MarketDataQuality(
        ready=not reasons,
        reasons=tuple(sorted(reasons)),
        bars_checked=len(bars),
        last_timestamp=last_timestamp,
    )
