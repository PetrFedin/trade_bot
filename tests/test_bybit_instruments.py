"""Boundary coverage for #143: venue payload to specification, acceptance 12.

Parsing is tested against a recorded payload so the mapping is checkable without a
network, and so a malformed field fails at the boundary rather than downstream.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.domain.instrument import InstrumentStatus
from app.marketdata import bybit_instruments as venue

OBSERVED = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)

# Recorded from GET /v5/market/instruments-info?category=linear&symbol=BTCUSDT
PAYLOAD = {
    "symbol": "BTCUSDT",
    "status": "Trading",
    "baseCoin": "BTC",
    "quoteCoin": "USDT",
    "settleCoin": "USDT",
    "contractType": "LinearPerpetual",
    "priceFilter": {"minPrice": "0.10", "maxPrice": "1999999.80", "tickSize": "0.10"},
    "lotSizeFilter": {
        "maxOrderQty": "1500.000",
        "minOrderQty": "0.001",
        "qtyStep": "0.001",
        "maxMktOrderQty": "150.000",
        "minNotionalValue": "5",
    },
    "leverageFilter": {"minLeverage": "1", "maxLeverage": "150.00", "leverageStep": "0.01"},
}


def test_recorded_payload_maps_every_rule() -> None:
    spec = venue.parse_instrument(PAYLOAD, category="linear", observed_at=OBSERVED)
    assert spec.symbol == "BTCUSDT"
    assert spec.status is InstrumentStatus.TRADING
    assert spec.tick_size == Decimal("0.10")
    assert spec.quantity_step == Decimal("0.001")
    assert spec.minimum_quantity == Decimal("0.001")
    assert spec.maximum_quantity == Decimal("1500.000")
    assert spec.minimum_notional == Decimal("5")
    assert spec.maximum_market_quantity == Decimal("150.000")
    assert spec.maximum_leverage == Decimal("150.00")
    assert spec.settle_coin == "USDT"


def test_parsed_spec_validates() -> None:
    venue.parse_instrument(PAYLOAD, category="linear", observed_at=OBSERVED).validate()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Trading", InstrumentStatus.TRADING),
        ("PreLaunch", InstrumentStatus.PRE_LAUNCH),
        ("Settling", InstrumentStatus.SETTLING),
        ("Delivering", InstrumentStatus.DELIVERING),
        ("Closed", InstrumentStatus.CLOSED),
    ],
)
def test_known_statuses_map(raw: str, expected: InstrumentStatus) -> None:
    assert venue.parse_status(raw) is expected


def test_an_unknown_status_does_not_become_tradable() -> None:
    """A status the venue adds later must fail closed rather than read as permission."""
    assert venue.parse_status("SomethingNew") is InstrumentStatus.UNKNOWN
    assert venue.parse_status(None) is InstrumentStatus.UNKNOWN


def test_a_missing_required_field_fails_at_the_boundary() -> None:
    broken = {**PAYLOAD, "priceFilter": {"minPrice": "0.10", "maxPrice": "10"}}
    with pytest.raises(venue.BybitInstrumentError, match="tickSize"):
        venue.parse_instrument(broken, category="linear", observed_at=OBSERVED)


def test_a_non_numeric_field_fails_at_the_boundary() -> None:
    broken = {**PAYLOAD, "priceFilter": {**PAYLOAD["priceFilter"], "tickSize": "abc"}}
    with pytest.raises(venue.BybitInstrumentError, match="not a decimal"):
        venue.parse_instrument(broken, category="linear", observed_at=OBSERVED)


def test_a_payload_without_a_symbol_is_refused() -> None:
    with pytest.raises(venue.BybitInstrumentError, match="no symbol"):
        venue.parse_instrument({**PAYLOAD, "symbol": ""}, category="linear")


def test_an_absent_minimum_notional_becomes_zero_not_missing() -> None:
    """Some categories publish no minimum; that is a real zero, not a parse failure."""
    payload = {**PAYLOAD, "lotSizeFilter": {k: v for k, v in PAYLOAD["lotSizeFilter"].items()
                                            if k != "minNotionalValue"}}
    spec = venue.parse_instrument(payload, category="linear", observed_at=OBSERVED)
    assert spec.minimum_notional == Decimal("0")


def test_envelope_refusal_is_surfaced() -> None:
    with pytest.raises(venue.BybitInstrumentError, match="venue refused"):
        venue.parse_response({"retCode": 10001, "retMsg": "params error"}, category="linear")


def test_envelope_without_a_list_is_refused() -> None:
    with pytest.raises(venue.BybitInstrumentError, match="no instrument list"):
        venue.parse_response({"retCode": 0, "result": {}}, category="linear")


def test_envelope_time_becomes_the_source_timestamp() -> None:
    response = {"retCode": 0, "result": {"list": [PAYLOAD]}, "time": 1789656057245}
    spec = venue.parse_response(response, category="linear", observed_at=OBSERVED)[0]
    assert spec.source_timestamp.year == 2026
    assert spec.observed_timestamp == OBSERVED


def test_acceptance_12_module_exposes_no_mutating_call() -> None:
    """Instrument qualification must not be able to touch an account."""
    forbidden = ("create", "submit", "cancel", "amend", "place", "order", "sign")
    exported = [name for name in dir(venue) if not name.startswith("_")]
    offending = [
        name
        for name in exported
        if callable(getattr(venue, name)) and any(word in name.lower() for word in forbidden)
    ]
    assert offending == []


def test_acceptance_12_only_the_public_market_endpoint_is_referenced() -> None:
    assert venue.ENDPOINT == "https://api.bybit.com/v5/market/instruments-info"
    assert "order" not in venue.ENDPOINT


def test_the_module_opens_no_sockets() -> None:
    """Transport is injected here as it is everywhere else under app/.

    A module that reaches the network on its own cannot be exercised from a recorded
    payload, and it puts a url-opening call inside the surface the security scan gates.
    """
    import ast
    from pathlib import Path as _Path

    tree = ast.parse(_Path(venue.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"urllib", "socket", "http", "requests", "httpx", "aiohttp"}


def test_load_instrument_uses_the_supplied_source() -> None:
    calls: list[tuple[str, str]] = []

    def source(*, category: str, symbol: str) -> dict:
        calls.append((category, symbol))
        return {"retCode": 0, "result": {"list": [PAYLOAD]}}

    spec = venue.load_instrument(source, "BTCUSDT", observed_at=OBSERVED)
    assert calls == [("linear", "BTCUSDT")]
    assert spec.symbol == "BTCUSDT"


def test_load_instrument_refuses_an_empty_list() -> None:
    def source(*, category: str, symbol: str) -> dict:
        return {"retCode": 0, "result": {"list": []}}

    with pytest.raises(venue.BybitInstrumentError, match="no rules"):
        venue.load_instrument(source, "NOPEUSDT")
