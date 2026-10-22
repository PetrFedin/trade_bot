"""Synthetic adversarial orderbook tests: NO actual executed orders."""
from __future__ import annotations

import copy

import pytest

from app.execution.market_depth_quote import (
    DepthSnapshot,
    InstrumentRules,
    QuoteRejected,
    dec,
    quote_market_cross,
)
from tools.depth_execution_research import SIX, build_depth_stress, hash_value


def fixture():
    observations = {}
    for i, symbol in enumerate(SIX):
        observations[symbol] = {
            "instrument": {
                "symbol": symbol,
                "status": "Trading",
                "lotSizeFilter": {
                    "minOrderQty": "1", "maxOrderQty": "100000",
                    "qtyStep": "1", "minOrderAmt": "1",
                },
                "priceFilter": {"tickSize": "0.1"},
            },
            "book": {
                "symbol": symbol, "ts": 1792706086159 + i * 200,
                "bids": [
                    {"price": "99.9", "qty": "100"},
                    {"price": "99.8", "qty": "100"},
                ],
                "asks": [
                    {"price": "100.1", "qty": "100"},
                    {"price": "100.2", "qty": "100"},
                ],
            },
        }
    source = {
        "schema_version": "astra-bybit-six-readonly-orderbook-observation-v1",
        "market_category": "linear",
        "source": "BYBIT_PUBLIC_READONLY_PLUGIN_OBSERVATION",
        "provider_signed": False,
        "historical_orderbook_observed": False,
        "actual_fills_verified": False,
        "live_trading_allowed": False,
        "observations": observations,
    }
    source["observation_sha256"] = hash_value(source)
    return source


def pair():
    item = fixture()["observations"]["BTCUSDT"]
    rules = InstrumentRules.parse(item["instrument"], "BTCUSDT")
    book = DepthSnapshot.parse(
        item["book"], rules, observation_ms=item["book"]["ts"],
    )
    return book, rules


def test_exact_depth_walk_fee_and_no_authority():
    book, rules = pair()
    r = quote_market_cross(
        book, rules, side="BUY", requested_notional=dec("15100", "n"),
        fee_bps=dec("8", "fee"),
    )
    assert r["status"] == "VISIBLE_DEPTH_SUFFICIENT"
    assert r["rounded_required_qty"] == "150"
    assert r["visible_qty_at_haircut"] == "150"
    assert dec(r["full_visible_vwap"], "vwap") > dec("100.1", "best")
    assert r["hypothetical_fill_confirmed"] is False
    assert r["usable_for_live_routing"] is False


def test_buy_sell_adverse_cost_and_fee_monotonic():
    book, rules = pair()
    for side in ("BUY", "SELL"):
        quotes = [
            quote_market_cross(
                book, rules, side=side,
                requested_notional=dec("1000", "n"),
                fee_bps=dec(fee, "fee"),
            )
            for fee in ("8", "10")
        ]
        assert quotes[0]["status"] == "VISIBLE_DEPTH_SUFFICIENT"
        assert dec(quotes[0]["hypothetical_cross_cost_usdt_vs_mid"], "c") > 0
        assert dec(quotes[1]["hypothetical_fee_usdt"], "c") > dec(
            quotes[0]["hypothetical_fee_usdt"], "c"
        )


def test_short_visible_book_must_never_claim_a_fill():
    book, rules = pair()
    r = quote_market_cross(
        book, rules, side="BUY", requested_notional=dec("30000", "n"),
        fee_bps=dec("8", "fee"), depth_haircut=dec("0.5", "h"),
    )
    assert r["status"] == "INSUFFICIENT_VISIBLE_DEPTH"
    assert r["hypothetical_fee_usdt"] is None
    assert r["full_visible_vwap"] is None
    assert dec(r["uncovered_qty"], "remaining") > 0


def test_instrument_filters_reject_small_notional():
    book, rules = pair()
    r = quote_market_cross(
        book, rules, side="BUY", requested_notional=dec("5", "n"),
        fee_bps=dec("8", "fee"),
    )
    assert r["status"] == "INSTRUMENT_FILTER_REJECTED"
    assert r["hypothetical_fill_confirmed"] is False


@pytest.mark.parametrize(
    "tamper", ["crossed", "off_tick", "duplicate", "negative",
               "stale", "future", "symbol"],
)
def test_unordered_stale_or_fake_public_source_rejected(tamper):
    item = copy.deepcopy(fixture()["observations"]["BTCUSDT"])
    raw = item["book"]
    now = raw["ts"]
    if tamper == "crossed":
        raw["asks"][0]["price"] = "99.9"
    elif tamper == "off_tick":
        raw["bids"][0]["price"] = "99.95"
    elif tamper == "duplicate":
        raw["asks"][1]["price"] = "100.1"
    elif tamper == "negative":
        raw["asks"][0]["qty"] = "-2"
    elif tamper == "stale":
        now += 2001
    elif tamper == "future":
        now -= 1
    else:
        raw["symbol"] = "ETHUSDT"
    rules = InstrumentRules.parse(item["instrument"], "BTCUSDT")
    with pytest.raises(QuoteRejected):
        DepthSnapshot.parse(raw, rules, observation_ms=now)


@pytest.mark.parametrize("value", [True, "NaN", "Infinity", "-Infinity", None])
def test_bad_numeric_data_rejected(value):
    with pytest.raises(QuoteRejected):
        dec(value, "number")


def test_six_coin_quote_matrix_and_no_promotion():
    report = build_depth_stress(fixture())
    assert report["symbol_count"] == 6
    assert report["quotes"] == 144
    assert report["displayed_depth_sufficient"] > 0
    assert report["insufficient_displayed_depth"] > 0
    assert report["venue_fills_verified"] is False
    assert report["fee_tier_verified"] is False
    assert report["mark_price_settlement_verified"] is False
    assert report["live_trading_allowed"] is False
    assert report["profitability_proven"] is False
    assert len(report["report_sha256"]) == 64


def test_changed_public_source_hash_and_false_exchange_signature_rejected():
    data = fixture()
    data["observations"]["BTCUSDT"]["book"]["asks"][0]["qty"] = "99999"
    with pytest.raises(QuoteRejected, match="hash"):
        build_depth_stress(data)
    data = fixture()
    data["provider_signed"] = True
    with pytest.raises(QuoteRejected, match="unsafe"):
        build_depth_stress(data)


def test_unsupported_fee_or_liquidity_haircut_rejected():
    book, rules = pair()
    for fee, haircut in (("101", "0"), ("10", "1"), ("-1", "0")):
        with pytest.raises(QuoteRejected):
            quote_market_cross(
                book, rules, side="BUY", requested_notional=dec("1000", "n"),
                fee_bps=dec(fee, "fee"),
                depth_haircut=dec(haircut, "haircut"),
            )
