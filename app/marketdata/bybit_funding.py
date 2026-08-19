from __future__ import annotations

import http.client
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.parse import urlencode

_HOST = "api.bybit.com"
_PATH = "/v5/market/funding/history"
_DAY_MS = 24 * 60 * 60 * 1000
_MAX_LIMIT = 200


class BybitFundingTransport(Protocol):
    def get_json(self, *, path: str, query_string: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class BybitFundingRateRecord:
    symbol: str
    funding_time: datetime
    funding_rate: Decimal

    def validate(self) -> None:
        if self.symbol != self.symbol.strip().upper() or not self.symbol.endswith("USDT"):
            raise ValueError("funding symbol must be normalized USDT symbol")
        if self.funding_time.tzinfo is None:
            raise ValueError("funding timestamp must be timezone-aware")
        if not self.funding_rate.is_finite():
            raise ValueError("funding rate must be finite")


@dataclass(frozen=True)
class BybitFundingHistory:
    symbol: str
    start_time: datetime
    end_time: datetime
    records: tuple[BybitFundingRateRecord, ...]
    request_count: int
    source: str = "BYBIT_V5_PUBLIC_FUNDING_HISTORY"
    live_mainnet_order_routing_allowed: bool = False


class BybitFundingHttpsTransport:
    """Exact-host public HTTPS GET transport with no authenticated/write surface."""

    host = _HOST
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def get_json(self, *, path: str, query_string: str) -> Mapping[str, Any]:
        if path != _PATH:
            raise ValueError("funding transport only permits the V5 funding-history path")
        if "#" in query_string:
            raise ValueError("funding query must not contain fragments")
        target = path if not query_string else f"{path}?{query_string}"
        connection = http.client.HTTPSConnection(_HOST, 443, timeout=10)
        try:
            connection.request("GET", target)
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()
        if response.status != 200:
            raise RuntimeError(f"Bybit funding-history HTTP status {response.status}")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Bybit funding-history returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Bybit funding-history response must be an object")
        return payload


class BybitFundingHistoryClient:
    """Read-only funding-rate acquisition in bounded one-day windows."""

    host = _HOST
    path = _PATH
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, *, transport: BybitFundingTransport | None = None) -> None:
        self._transport = BybitFundingHttpsTransport() if transport is None else transport

    def fetch_history(
        self,
        *,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
    ) -> BybitFundingHistory:
        _validate_symbol(symbol)
        start = _utc(start_time)
        end = _utc(end_time)
        if end <= start:
            raise ValueError("funding history end_time must be after start_time")

        records: dict[datetime, BybitFundingRateRecord] = {}
        request_count = 0
        window_start = start
        while window_start < end:
            window_end = min(end, window_start + timedelta(days=1))
            rows = self._fetch_window(
                symbol=symbol,
                start_time=window_start,
                end_time=window_end,
            )
            request_count += 1
            if len(rows) >= _MAX_LIMIT:
                raise RuntimeError(
                    "Bybit funding-history daily window reached API limit; "
                    "refusing possible truncation"
                )
            for record in rows:
                if not start <= record.funding_time <= end:
                    continue
                existing = records.get(record.funding_time)
                if existing is not None and existing.funding_rate != record.funding_rate:
                    raise RuntimeError("conflicting Bybit funding rates for one funding timestamp")
                records[record.funding_time] = record
            window_start = window_end

        ordered = tuple(records[key] for key in sorted(records))
        return BybitFundingHistory(
            symbol=symbol,
            start_time=start,
            end_time=end,
            records=ordered,
            request_count=request_count,
        )

    def _fetch_window(
        self,
        *,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
    ) -> tuple[BybitFundingRateRecord, ...]:
        query = urlencode(
            sorted(
                {
                    "category": "linear",
                    "symbol": symbol,
                    "startTime": str(_milliseconds(start_time)),
                    "endTime": str(_milliseconds(end_time)),
                    "limit": str(_MAX_LIMIT),
                }.items()
            )
        )
        payload = self._transport.get_json(path=_PATH, query_string=query)
        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit funding-history retCode {payload.get('retCode')}")
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise RuntimeError("Bybit funding-history result must be an object")
        raw_rows = result.get("list")
        if not isinstance(raw_rows, list):
            raise RuntimeError("Bybit funding-history result.list must be an array")
        records: list[BybitFundingRateRecord] = []
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                raise RuntimeError("Bybit funding-history row must be an object")
            row_symbol = raw.get("symbol")
            if row_symbol != symbol:
                raise RuntimeError("Bybit funding-history row symbol mismatch")
            timestamp = _integer(raw, "fundingRateTimestamp")
            rate = _decimal(raw, "fundingRate")
            record = BybitFundingRateRecord(
                symbol=symbol,
                funding_time=datetime.fromtimestamp(timestamp / 1000, tz=UTC),
                funding_rate=rate,
            )
            record.validate()
            records.append(record)
        return tuple(sorted(records, key=lambda item: item.funding_time))


def _validate_symbol(symbol: str) -> None:
    if symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
        raise ValueError("funding symbol must be normalized USDT symbol")
    base = symbol[:-4]
    if not base or not base.isalnum():
        raise ValueError("funding symbol contains invalid characters")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("funding history timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _integer(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Bybit funding-history invalid {field}") from exc
    if parsed < 0:
        raise RuntimeError(f"Bybit funding-history invalid {field}")
    return parsed


def _decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"Bybit funding-history invalid {field}") from exc
    if not parsed.is_finite():
        raise RuntimeError(f"Bybit funding-history invalid {field}")
    return parsed
