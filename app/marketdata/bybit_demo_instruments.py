from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http.client import HTTPSConnection
from typing import Any
from urllib.parse import urlencode

from app.marketdata.bybit_http import decode_public_json_response
from app.marketdata.bybit_instruments import BybitInstrumentSpec, _parse_single_instrument

_BYBIT_DEMO_HOST = "api-demo.bybit.com"
_BYBIT_DEMO_INSTRUMENT_PATH = "/v5/market/instruments-info"
BYBIT_DEMO_INSTRUMENT_URL = f"https://{_BYBIT_DEMO_HOST}{_BYBIT_DEMO_INSTRUMENT_PATH}"


@dataclass(frozen=True)
class BybitDemoInstrumentHttpJson:
    status_code: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


Transport = Callable[[str, Mapping[str, str]], BybitDemoInstrumentHttpJson]


class BybitDemoInstrumentClient:
    """Read current linear instrument constraints from the Demo trading domain only."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, *, transport: Transport | None = None) -> None:
        self._transport = _https_transport if transport is None else transport

    def fetch_symbol(self, symbol: str) -> BybitInstrumentSpec:
        return self.fetch_symbols((symbol,))[symbol]

    def fetch_symbols(self, symbols: Sequence[str]) -> dict[str, BybitInstrumentSpec]:
        normalized = tuple(symbol.strip().upper() for symbol in symbols)
        if len(normalized) < 1 or normalized != tuple(symbols):
            raise ValueError("Bybit Demo instrument symbols must be normalized uppercase")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Bybit Demo instrument symbols must be unique")

        specs: dict[str, BybitInstrumentSpec] = {}
        for symbol in normalized:
            if not symbol.endswith("USDT"):
                raise ValueError("Bybit Demo instrument must be a USDT symbol")
            query = urlencode({"category": "linear", "symbol": symbol})
            url = f"{BYBIT_DEMO_INSTRUMENT_URL}?{query}"
            response = self._transport(url, {"Accept": "application/json"})
            if response.status_code != 200:
                raise ValueError(
                    f"Bybit Demo instrument request failed:{response.status_code}"
                )
            if response.payload.get("retCode") != 0:
                raise ValueError(
                    f"Bybit Demo instrument API error:{response.payload.get('retMsg')}"
                )
            spec = _parse_single_instrument(
                response.payload,
                expected_symbol=symbol,
            )
            spec.validate()
            specs[symbol] = spec
        return specs


def _https_transport(
    url: str,
    headers: Mapping[str, str],
) -> BybitDemoInstrumentHttpJson:
    prefix = f"https://{_BYBIT_DEMO_HOST}"
    if not url.startswith(prefix):
        raise ValueError("Bybit Demo instrument transport rejected non-demo host")
    target = url[len(prefix) :]
    if not target.startswith(_BYBIT_DEMO_INSTRUMENT_PATH):
        raise ValueError("Bybit Demo instrument transport rejected unsupported path")

    connection = HTTPSConnection(_BYBIT_DEMO_HOST, 443, timeout=10)
    try:
        connection.request("GET", target, headers=dict(headers))
        response = connection.getresponse()
        body = response.read()
        response_headers = {key: value for key, value in response.getheaders()}
    finally:
        connection.close()
    payload = decode_public_json_response(
        status_code=response.status,
        headers=response_headers,
        body=body,
    )
    return BybitDemoInstrumentHttpJson(
        status_code=response.status,
        headers=response_headers,
        payload=payload,
    )


__all__ = [
    "BYBIT_DEMO_INSTRUMENT_URL",
    "BybitDemoInstrumentClient",
    "BybitDemoInstrumentHttpJson",
]
