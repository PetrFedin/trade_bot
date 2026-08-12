from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from http.client import HTTPSConnection
from typing import Any
from urllib.parse import urlencode, urlsplit

from app.strategy.crypto_perp import CryptoSide

_BYBIT_MAINNET_HOST = "api.bybit.com"
_BYBIT_INSTRUMENT_PATH = "/v5/market/instruments-info"
BYBIT_INSTRUMENT_URL = f"https://{_BYBIT_MAINNET_HOST}{_BYBIT_INSTRUMENT_PATH}"


@dataclass(frozen=True)
class BybitInstrumentSpec:
    symbol: str
    status: str
    contract_type: str
    base_coin: str
    quote_coin: str
    settle_coin: str
    tick_size: Decimal
    min_order_qty: Decimal
    qty_step: Decimal
    min_notional_value: Decimal
    max_market_order_qty: Decimal
    max_leverage: Decimal
    funding_interval_minutes: int

    def validate(self) -> None:
        if self.symbol != self.symbol.strip().upper() or not self.symbol:
            raise ValueError("Bybit instrument symbol must be normalized uppercase")
        if self.status != "Trading":
            raise ValueError("Bybit instrument must be Trading")
        if self.contract_type != "LinearPerpetual":
            raise ValueError("Bybit crypto strategy requires LinearPerpetual")
        if self.quote_coin != "USDT" or self.settle_coin != "USDT":
            raise ValueError("Bybit crypto strategy currently requires USDT settlement")
        positive_decimals = (
            self.tick_size,
            self.min_order_qty,
            self.qty_step,
            self.min_notional_value,
            self.max_market_order_qty,
            self.max_leverage,
        )
        if any(not value.is_finite() or value <= 0 for value in positive_decimals):
            raise ValueError("Bybit instrument numeric limits must be positive and finite")
        if self.min_order_qty > self.max_market_order_qty:
            raise ValueError("Bybit min order quantity exceeds max market quantity")
        if self.funding_interval_minutes <= 0:
            raise ValueError("Bybit funding interval must be positive")

    def normalize_market_quantity(
        self,
        desired_quantity: Decimal,
        *,
        reference_price: Decimal,
    ) -> Decimal | None:
        """Round quantity down without increasing risk; return None if not tradable."""

        self.validate()
        if not desired_quantity.is_finite() or desired_quantity <= 0:
            raise ValueError("desired Bybit quantity must be positive and finite")
        if not reference_price.is_finite() or reference_price <= 0:
            raise ValueError("Bybit reference price must be positive and finite")
        capped = min(desired_quantity, self.max_market_order_qty)
        units = (capped / self.qty_step).to_integral_value(rounding=ROUND_DOWN)
        normalized = units * self.qty_step
        if normalized < self.min_order_qty:
            return None
        if normalized * reference_price < self.min_notional_value:
            return None
        return normalized

    def normalize_target_price(self, side: CryptoSide, raw_price: Decimal) -> Decimal:
        """Make a profit target no easier after exchange tick quantization."""

        self.validate()
        _validate_price(raw_price)
        if side is CryptoSide.LONG:
            return _quantize_to_step(raw_price, self.tick_size, ROUND_UP)
        return _quantize_to_step(raw_price, self.tick_size, ROUND_DOWN)

    def normalize_protective_stop_price(
        self,
        side: CryptoSide,
        raw_price: Decimal,
    ) -> Decimal:
        """Use adverse rounding so historical protection is not overstated."""

        self.validate()
        _validate_price(raw_price)
        if side is CryptoSide.LONG:
            return _quantize_to_step(raw_price, self.tick_size, ROUND_DOWN)
        return _quantize_to_step(raw_price, self.tick_size, ROUND_UP)


@dataclass(frozen=True)
class BybitInstrumentHttpJson:
    status_code: int
    headers: Mapping[str, str]
    payload: Mapping[str, Any]


Transport = Callable[[str, Mapping[str, str]], BybitInstrumentHttpJson]


class BybitInstrumentClient:
    """Read-only V5 instrument specification client for selected USDT perpetuals."""

    def __init__(self, *, transport: Transport | None = None) -> None:
        self._transport = _https_transport if transport is None else transport

    def fetch_symbols(self, symbols: Sequence[str]) -> dict[str, BybitInstrumentSpec]:
        normalized = tuple(symbol.strip().upper() for symbol in symbols)
        if len(normalized) < 1 or normalized != tuple(symbols):
            raise ValueError("Bybit instrument symbols must already be normalized uppercase")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Bybit instrument symbols must be unique")
        specs: dict[str, BybitInstrumentSpec] = {}
        for symbol in normalized:
            url = f"{BYBIT_INSTRUMENT_URL}?{urlencode({'category': 'linear', 'symbol': symbol})}"
            response = self._transport(url, {"Accept": "application/json"})
            if response.status_code != 200:
                raise ValueError(f"Bybit instrument request failed:{response.status_code}")
            if response.payload.get("retCode") != 0:
                raise ValueError(f"Bybit instrument API error:{response.payload.get('retMsg')}")
            spec = _parse_single_instrument(response.payload, expected_symbol=symbol)
            spec.validate()
            specs[symbol] = spec
        return specs


def _parse_single_instrument(
    payload: Mapping[str, Any],
    *,
    expected_symbol: str,
) -> BybitInstrumentSpec:
    result = payload.get("result")
    if not isinstance(result, Mapping) or result.get("category") != "linear":
        raise ValueError("Bybit instrument response missing linear result")
    rows = result.get("list")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise ValueError("Bybit instrument response must contain exactly one instrument")
    row = rows[0]
    if row.get("symbol") != expected_symbol:
        raise ValueError("Bybit instrument response symbol mismatch")
    price_filter = row.get("priceFilter")
    lot_filter = row.get("lotSizeFilter")
    leverage_filter = row.get("leverageFilter")
    if not isinstance(price_filter, Mapping):
        raise ValueError("Bybit instrument response missing priceFilter")
    if not isinstance(lot_filter, Mapping):
        raise ValueError("Bybit instrument response missing lotSizeFilter")
    if not isinstance(leverage_filter, Mapping):
        raise ValueError("Bybit instrument response missing leverageFilter")
    funding_interval = row.get("fundingInterval")
    try:
        funding_interval_minutes = int(funding_interval)
    except (TypeError, ValueError) as exc:
        raise ValueError("Bybit instrument fundingInterval is invalid") from exc
    return BybitInstrumentSpec(
        symbol=_required_text(row, "symbol"),
        status=_required_text(row, "status"),
        contract_type=_required_text(row, "contractType"),
        base_coin=_required_text(row, "baseCoin"),
        quote_coin=_required_text(row, "quoteCoin"),
        settle_coin=_required_text(row, "settleCoin"),
        tick_size=_required_decimal(price_filter, "tickSize"),
        min_order_qty=_required_decimal(lot_filter, "minOrderQty"),
        qty_step=_required_decimal(lot_filter, "qtyStep"),
        min_notional_value=_required_decimal(lot_filter, "minNotionalValue"),
        max_market_order_qty=_required_decimal(lot_filter, "maxMktOrderQty"),
        max_leverage=_required_decimal(leverage_filter, "maxLeverage"),
        funding_interval_minutes=funding_interval_minutes,
    )


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Bybit instrument response missing {field}")
    return value


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value in (None, ""):
        raise ValueError(f"Bybit instrument response missing {field}")
    try:
        parsed = Decimal(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Bybit instrument response has invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"Bybit instrument response has non-finite {field}")
    return parsed


def _validate_price(price: Decimal) -> None:
    if not price.is_finite() or price <= 0:
        raise ValueError("Bybit price must be positive and finite")


def _quantize_to_step(price: Decimal, step: Decimal, rounding: str) -> Decimal:
    units = (price / step).to_integral_value(rounding=rounding)
    return units * step


def _https_transport(url: str, headers: Mapping[str, str]) -> BybitInstrumentHttpJson:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != _BYBIT_MAINNET_HOST:
        raise ValueError("Bybit instrument transport rejected non-allowlisted endpoint")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise ValueError("Bybit instrument transport rejected ambiguous URL authority")
    if parsed.port not in (None, 443):
        raise ValueError("Bybit instrument transport requires HTTPS port 443")
    if parsed.path != _BYBIT_INSTRUMENT_PATH:
        raise ValueError("Bybit instrument transport rejected unexpected path")
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    connection = HTTPSConnection(_BYBIT_MAINNET_HOST, 443, timeout=30)
    try:
        connection.request("GET", target, headers=dict(headers))
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Bybit instrument response must be a JSON object")
        return BybitInstrumentHttpJson(
            status_code=response.status,
            headers={key: value for key, value in response.getheaders()},
            payload=payload,
        )
    finally:
        connection.close()
