from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.domain.trading import Bar


class HistoricalDataError(ValueError):
    pass


@dataclass(frozen=True)
class HistoricalDataPolicy:
    minimum_bars: int = 20
    maximum_gap: timedelta | None = None
    maximum_jump_fraction: Decimal | None = None

    def validate(self) -> None:
        if self.minimum_bars < 3:
            raise ValueError("minimum_bars must be at least three")
        if self.maximum_gap is not None and self.maximum_gap <= timedelta(0):
            raise ValueError("maximum_gap must be positive when supplied")
        if self.maximum_jump_fraction is not None and (
            not self.maximum_jump_fraction.is_finite() or self.maximum_jump_fraction <= 0
        ):
            raise ValueError("maximum_jump_fraction must be positive and finite when supplied")


@dataclass(frozen=True)
class HistoricalDataset:
    dataset_id: str
    symbol: str
    bars: tuple[Bar, ...]
    canonical_sha256: str
    source_name: str
    schema_version: str = "historical-bars-v1"

    @property
    def first_timestamp(self) -> datetime:
        return self.bars[0].timestamp

    @property
    def last_timestamp(self) -> datetime:
        return self.bars[-1].timestamp


def _canonical_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def canonical_dataset_sha256(bars: tuple[Bar, ...]) -> str:
    if not bars:
        raise HistoricalDataError("historical dataset is empty")
    digest = hashlib.sha256()
    for bar in bars:
        moment = bar.timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
        row = f"{moment}|{bar.symbol}|{_canonical_decimal(bar.close)}\n"
        digest.update(row.encode("utf-8"))
    return digest.hexdigest()


def validate_historical_bars(
    bars: tuple[Bar, ...],
    *,
    policy: HistoricalDataPolicy | None = None,
) -> str:
    policy = HistoricalDataPolicy() if policy is None else policy
    policy.validate()
    if len(bars) < policy.minimum_bars:
        raise HistoricalDataError("INSUFFICIENT_HISTORICAL_BARS")
    symbol = bars[0].symbol
    previous: Bar | None = None
    seen: set[datetime] = set()
    for bar in bars:
        try:
            bar.validate()
        except ValueError as exc:
            raise HistoricalDataError("INVALID_HISTORICAL_BAR") from exc
        if bar.symbol != symbol:
            raise HistoricalDataError("MIXED_HISTORICAL_SYMBOLS")
        if bar.timestamp in seen:
            raise HistoricalDataError("DUPLICATE_HISTORICAL_TIMESTAMP")
        seen.add(bar.timestamp)
        if previous is not None:
            if bar.timestamp <= previous.timestamp:
                raise HistoricalDataError("NON_MONOTONIC_HISTORICAL_TIME")
            if (
                policy.maximum_gap is not None
                and bar.timestamp - previous.timestamp > policy.maximum_gap
            ):
                raise HistoricalDataError("HISTORICAL_GAP_EXCEEDED")
            if policy.maximum_jump_fraction is not None:
                jump = abs(bar.close - previous.close) / previous.close
                if jump > policy.maximum_jump_fraction:
                    raise HistoricalDataError("HISTORICAL_PRICE_JUMP_EXCEEDED")
        previous = bar
    return symbol


class CsvHistoricalBarSource:
    REQUIRED_COLUMNS = ("timestamp", "symbol", "close")

    def __init__(
        self,
        path: str | Path,
        *,
        policy: HistoricalDataPolicy | None = None,
        expected_symbol: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.policy = HistoricalDataPolicy() if policy is None else policy
        self.policy.validate()
        if expected_symbol is not None:
            normalized = expected_symbol.strip().upper()
            if not normalized:
                raise ValueError("expected_symbol cannot be blank")
            self.expected_symbol = normalized
        else:
            self.expected_symbol = None

    def load(self) -> HistoricalDataset:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        bars: list[Bar] = []
        with self.path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None or tuple(reader.fieldnames) != self.REQUIRED_COLUMNS:
                raise HistoricalDataError("HISTORICAL_CSV_SCHEMA_MISMATCH")
            for row_number, row in enumerate(reader, start=2):
                try:
                    timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                except (AttributeError, ValueError) as exc:
                    raise HistoricalDataError(
                        f"INVALID_HISTORICAL_TIMESTAMP:row={row_number}"
                    ) from exc
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise HistoricalDataError(
                        f"NAIVE_HISTORICAL_TIMESTAMP:row={row_number}"
                    )
                symbol = row["symbol"].strip().upper()
                try:
                    close = Decimal(row["close"])
                except (InvalidOperation, TypeError) as exc:
                    raise HistoricalDataError(
                        f"INVALID_HISTORICAL_CLOSE:row={row_number}"
                    ) from exc
                bars.append(Bar(symbol=symbol, timestamp=timestamp.astimezone(UTC), close=close))
        frozen = tuple(bars)
        symbol = validate_historical_bars(frozen, policy=self.policy)
        if self.expected_symbol is not None and symbol != self.expected_symbol:
            raise HistoricalDataError("HISTORICAL_SYMBOL_MISMATCH")
        fingerprint = canonical_dataset_sha256(frozen)
        dataset_id = (
            f"{symbol}:{frozen[0].timestamp.date()}:"
            f"{frozen[-1].timestamp.date()}:{fingerprint[:16]}"
        )
        return HistoricalDataset(
            dataset_id=dataset_id,
            symbol=symbol,
            bars=frozen,
            canonical_sha256=fingerprint,
            source_name=self.path.name,
        )
