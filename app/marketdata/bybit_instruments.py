"""Acquire instrument rules from Bybit and turn them into an immutable specification.

Parsing is kept separate from fetching so the mapping can be tested against recorded
venue payloads without a network, and so a malformed field fails loudly at the boundary
rather than becoming a plausible-looking Decimal deeper in the system.

The venue documents that some maximum-quantity fields are revised periodically, so each
snapshot records when it was observed. Nothing here decides whether an order may be
sent; it only states what the venue currently permits.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from app.domain.instrument import InstrumentSpec, InstrumentStatus

ENDPOINT = "https://api.bybit.com/v5/market/instruments-info"
VENUE = "bybit"

_STATUS = {
    "Trading": InstrumentStatus.TRADING,
    "PreLaunch": InstrumentStatus.PRE_LAUNCH,
    "Settling": InstrumentStatus.SETTLING,
    "Delivering": InstrumentStatus.DELIVERING,
    "Closed": InstrumentStatus.CLOSED,
}


class BybitInstrumentError(RuntimeError):
    """Raised when the venue refuses or publishes a payload we cannot trust."""


def parse_status(value: object) -> InstrumentStatus:
    """Map a venue status string, treating anything unrecognised as not tradable."""
    return _STATUS.get(str(value), InstrumentStatus.UNKNOWN)


def _decimal(source: dict, key: str, *, required: bool = True) -> Decimal | None:
    raw = source.get(key)
    if raw is None or raw == "":
        if required:
            raise BybitInstrumentError(f"missing required field {key!r}")
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError) as error:
        raise BybitInstrumentError(f"field {key!r} is not a decimal: {raw!r}") from error


def parse_instrument(
    payload: dict,
    *,
    category: str,
    environment: str = "mainnet",
    observed_at: datetime | None = None,
    source_timestamp: datetime | None = None,
) -> InstrumentSpec:
    """Build a validated specification from one entry of the venue's instrument list."""
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise BybitInstrumentError("payload carries no symbol")

    price_filter = payload.get("priceFilter") or {}
    lot_filter = payload.get("lotSizeFilter") or {}
    leverage_filter = payload.get("leverageFilter") or {}

    observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
    spec = InstrumentSpec(
        venue=VENUE,
        environment=environment,
        category=category,
        symbol=symbol,
        status=parse_status(payload.get("status")),
        base_coin=str(payload.get("baseCoin") or "").strip().upper() or "UNKNOWN",
        quote_coin=str(payload.get("quoteCoin") or "").strip().upper() or "UNKNOWN",
        settle_coin=str(payload.get("settleCoin") or payload.get("quoteCoin") or "").strip().upper()
        or "UNKNOWN",
        tick_size=_decimal(price_filter, "tickSize"),
        minimum_price=_decimal(price_filter, "minPrice"),
        maximum_price=_decimal(price_filter, "maxPrice"),
        quantity_step=_decimal(lot_filter, "qtyStep"),
        minimum_quantity=_decimal(lot_filter, "minOrderQty"),
        maximum_quantity=_decimal(lot_filter, "maxOrderQty"),
        minimum_notional=_decimal(lot_filter, "minNotionalValue", required=False) or Decimal("0"),
        maximum_market_quantity=_decimal(lot_filter, "maxMktOrderQty", required=False),
        minimum_leverage=_decimal(leverage_filter, "minLeverage", required=False),
        maximum_leverage=_decimal(leverage_filter, "maxLeverage", required=False),
        leverage_step=_decimal(leverage_filter, "leverageStep", required=False),
        source_timestamp=(source_timestamp or observed).astimezone(UTC),
        observed_timestamp=observed,
    )
    spec.validate()
    return spec


def parse_response(
    payload: dict,
    *,
    category: str,
    environment: str = "mainnet",
    observed_at: datetime | None = None,
) -> list[InstrumentSpec]:
    """Validate an envelope and return every instrument it carries."""
    if payload.get("retCode") != 0:
        raise BybitInstrumentError(
            f"venue refused: {payload.get('retCode')} {payload.get('retMsg')}"
        )
    rows = (payload.get("result") or {}).get("list")
    if not isinstance(rows, list):
        raise BybitInstrumentError("response carried no instrument list")
    stamp = payload.get("time")
    source = (
        datetime.fromtimestamp(int(stamp) / 1000, tz=UTC)
        if isinstance(stamp, int | str) and str(stamp).isdigit()
        else None
    )
    return [
        parse_instrument(
            row,
            category=category,
            environment=environment,
            observed_at=observed_at,
            source_timestamp=source,
        )
        for row in rows
    ]


def fetch_instrument(
    symbol: str, *, category: str = "linear", timeout: float = 30
) -> InstrumentSpec:
    """Read one instrument's current rules from the public endpoint."""
    query = f"{ENDPOINT}?category={category}&symbol={symbol}"
    try:
        with urllib.request.urlopen(query, timeout=timeout) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError) as error:
        raise BybitInstrumentError(f"instrument request failed: {error}") from error
    specs = parse_response(payload, category=category)
    if not specs:
        raise BybitInstrumentError(f"venue published no rules for {symbol}")
    return specs[0]
