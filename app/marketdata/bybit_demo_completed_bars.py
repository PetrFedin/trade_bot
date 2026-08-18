from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from http.client import HTTPSConnection
from typing import Any
from urllib.parse import urlencode, urlsplit

from app.marketdata.bybit_http import decode_public_json_response
from app.marketdata.bybit_v5 import (
    BybitKlineBar,
    interval_milliseconds,
    last_completed_kline_end_ms,
)

_BYBIT_DEMO_HOST = "api-demo.bybit.com"
_BYBIT_DEMO_KLINE_PATH = "/v5/market/kline"
BYBIT_DEMO_KLINE_URL = f"https://{_BYBIT_DEMO_HOST}{_BYBIT_DEMO_KLINE_PATH}"


@dataclass(frozen=True)
class BybitDemoKlineHttpJson:
    status_code: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


Transport = Callable[[str, Mapping[str, str]], BybitDemoKlineHttpJson]


class BybitDemoCompletedBarClient:
    """Read-only demo-domain kline reader that returns completed contiguous bars only."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, *, transport: Transport | None = None) -> None:
        self._transport = _https_transport if transport is None else transport

    def fetch_completed_range(
        self,
        *,
        symbol: str,
        start_ms: int,
        now_ms: int,
        interval: str = "5",
    ) -> tuple[BybitKlineBar, ...]:
        _validate_symbol(symbol)
        interval_ms = interval_milliseconds(interval)
        if start_ms < 0 or start_ms % interval_ms != 0:
            raise ValueError("demo completed-bar start must be an aligned non-negative timestamp")
        end_ms = last_completed_kline_end_ms(now_ms=now_ms, interval=interval)
        if end_ms < start_ms:
            return ()
        last_start_ms = (end_ms // interval_ms) * interval_ms
        expected_count = ((last_start_ms - start_ms) // interval_ms) + 1
        if expected_count > 1000:
            raise ValueError("demo completed-bar range exceeds one deterministic kline page")
        url = f"{BYBIT_DEMO_KLINE_URL}?{urlencode({'category': 'linear', 'symbol': symbol, 'interval': interval, 'start': str(start_ms), 'end': str(end_ms), 'limit': str(expected_count)})}"
        response = self._transport(url, {"Accept": "application/json"})
        if response.status_code != 200:
            raise ValueError(f"Bybit demo kline request failed:{response.status_code}")
        if response.payload.get("retCode") != 0:
            raise ValueError(f"Bybit demo kline API error:{response.payload.get('retMsg')}")
        result = response.payload.get("result")
        if not isinstance(result, Mapping):
            raise ValueError("Bybit demo kline response missing result")
        if result.get("category") != "linear" or result.get("symbol") != symbol:
            raise ValueError("Bybit demo kline response identity mismatch")
        rows = result.get("list")
        if not isinstance(rows, list) or not all(isinstance(row, list) for row in rows):
            raise ValueError("Bybit demo kline response missing list")

        by_start: dict[int, BybitKlineBar] = {}
        for row in rows:
            start, bar = _parse_bar(symbol, row)
            if start_ms <= start <= last_start_ms:
                if start in by_start:
                    raise ValueError("duplicate Bybit demo completed kline")
                by_start[start] = bar
        expected_starts = tuple(
            range(start_ms, last_start_ms + interval_ms, interval_ms)
        )
        if tuple(sorted(by_start)) != expected_starts:
            raise ValueError("Bybit demo completed-bar range is not contiguous")
        return tuple(by_start[start] for start in expected_starts)


def _parse_bar(symbol: str, row: list[Any]) -> tuple[int, BybitKlineBar]:
    if len(row) < 7:
        raise ValueError("Bybit demo kline row is too short")
    try:
        start_ms = int(row[0])
        bar = BybitKlineBar(
            symbol=symbol,
            start_time=datetime.fromtimestamp(start_ms / 1000, tz=UTC),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
            turnover=Decimal(str(row[6])),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Bybit demo kline row") from exc
    bar.validate()
    return start_ms, bar


def _validate_symbol(symbol: str) -> None:
    if symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
        raise ValueError("Bybit demo kline symbol must be normalized USDT")


def _https_transport(url: str, headers: Mapping[str, str]) -> BybitDemoKlineHttpJson:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != _BYBIT_DEMO_HOST:
        raise ValueError("Bybit demo kline transport rejected non-demo endpoint")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("Bybit demo kline transport rejected ambiguous URL authority")
    if parsed.port not in (None, 443):
        raise ValueError("Bybit demo kline transport requires HTTPS port 443")
    if parsed.path != _BYBIT_DEMO_KLINE_PATH:
        raise ValueError("Bybit demo kline transport rejected unexpected path")
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    connection = HTTPSConnection(_BYBIT_DEMO_HOST, 443, timeout=10)
    try:
        connection.request("GET", target, headers=dict(headers))
        response = connection.getresponse()
        response_headers = {key: value for key, value in response.getheaders()}
        body = response.read()
        payload = decode_public_json_response(
            status_code=response.status,
            headers=response_headers,
            body=body,
        )
        return BybitDemoKlineHttpJson(
            status_code=response.status,
            headers=response_headers,
            payload=payload,
        )
    finally:
        connection.close()
