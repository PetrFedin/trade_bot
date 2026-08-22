from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from app.marketdata.bybit_research_universe import (
    BybitResearchInstrument,
    BybitResearchTicker,
    BybitResearchUniverseClient,
    BybitResearchUniverseHttpJson,
    BybitResearchUniversePolicy,
    select_bybit_research_universe,
    validate_bybit_public_research_host,
)

_DAY_MS = 86_400_000
_NOW_MS = 2_000 * _DAY_MS


def _instrument(
    symbol: str,
    *,
    launch_days_ago: int = 400,
    base_coin: str | None = None,
    symbol_type: str = "innovation",
    status: str = "Trading",
    contract_type: str = "LinearPerpetual",
    quote_coin: str = "USDT",
    settle_coin: str = "USDT",
    is_pre_listing: bool = False,
) -> BybitResearchInstrument:
    base = symbol.removesuffix("USDT") if base_coin is None else base_coin
    return BybitResearchInstrument(
        symbol=symbol,
        base_coin=base,
        quote_coin=quote_coin,
        settle_coin=settle_coin,
        contract_type=contract_type,
        status=status,
        symbol_type=symbol_type,
        launch_time_ms=_NOW_MS - launch_days_ago * _DAY_MS,
        delivery_time_ms=0,
        is_pre_listing=is_pre_listing,
    )


def _ticker(
    symbol: str,
    *,
    turnover: str,
    oi_value: str,
    bid: str = "99.9",
    ask: str = "100.1",
    funding: str = "0.0001",
) -> BybitResearchTicker:
    return BybitResearchTicker(
        symbol=symbol,
        last_price=Decimal("100"),
        bid_price=Decimal(bid),
        ask_price=Decimal(ask),
        turnover_24h_usdt=Decimal(turnover),
        volume_24h=Decimal("1000000"),
        open_interest=Decimal("500000"),
        open_interest_value_usdt=Decimal(oi_value),
        funding_rate=Decimal(funding),
        price_24h_fraction=Decimal("0.02"),
    )


def _policy(*, top_n: int = 3) -> BybitResearchUniversePolicy:
    return BybitResearchUniversePolicy(
        top_n=top_n,
        minimum_listing_days=90,
        minimum_turnover_24h_usdt=Decimal("1000000"),
        minimum_open_interest_value_usdt=Decimal("1000000"),
        maximum_spread_bps=Decimal("50"),
        maximum_abs_funding_rate=Decimal("0.01"),
    )


def test_dynamic_universe_ranks_only_eligible_crypto_perpetuals() -> None:
    instruments = (
        _instrument("BTCUSDT", launch_days_ago=1500),
        _instrument("ETHUSDT", launch_days_ago=1400),
        _instrument("SOLUSDT", launch_days_ago=900),
        _instrument("USDCUSDT", base_coin="USDC"),
        _instrument("XAUUSDT", base_coin="XAU", symbol_type="commodity"),
        _instrument("NEWUSDT", launch_days_ago=10),
    )
    tickers = (
        _ticker("BTCUSDT", turnover="1000000000", oi_value="500000000"),
        _ticker("ETHUSDT", turnover="700000000", oi_value="350000000"),
        _ticker("SOLUSDT", turnover="400000000", oi_value="150000000"),
        _ticker("USDCUSDT", turnover="900000000", oi_value="200000000"),
        _ticker("XAUUSDT", turnover="800000000", oi_value="250000000"),
        _ticker("NEWUSDT", turnover="600000000", oi_value="100000000"),
    )

    selection = select_bybit_research_universe(
        instruments,
        tickers,
        observed_at_ms=_NOW_MS,
        policy=_policy(),
    )

    assert selection.complete_top_n is True
    assert selection.blockers == ()
    assert [item.symbol for item in selection.selected] == [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
    ]
    assert selection.selected[0].score >= selection.selected[1].score
    assert selection.selected[1].score >= selection.selected[2].score
    assert selection.excluded_reasons["USDCUSDT"] == ("STABLECOIN_BASE_EXCLUDED",)
    assert selection.excluded_reasons["XAUUSDT"] == ("NON_CRYPTO_LINEAR_PRODUCT",)
    assert selection.excluded_reasons["NEWUSDT"] == ("INSUFFICIENT_LISTING_HISTORY",)
    assert selection.research_only is True
    assert selection.strategy_parameters_changed is False
    assert selection.strategy_promotion_allowed is False
    assert selection.demo_activation_allowed is False
    assert selection.live_activation_allowed is False
    assert selection.bybit_live_order_routing_allowed is False


def test_universe_uses_cross_sectional_liquidity_oi_spread_and_history_score() -> None:
    instruments = (
        _instrument("AAAUSDT", launch_days_ago=1000),
        _instrument("BBBUSDT", launch_days_ago=500),
        _instrument("CCCUSDT", launch_days_ago=200),
    )
    tickers = (
        _ticker(
            "AAAUSDT",
            turnover="100000000",
            oi_value="100000000",
            bid="99.99",
            ask="100.01",
        ),
        _ticker(
            "BBBUSDT",
            turnover="90000000",
            oi_value="90000000",
            bid="99.95",
            ask="100.05",
        ),
        _ticker(
            "CCCUSDT",
            turnover="80000000",
            oi_value="80000000",
            bid="99.90",
            ask="100.10",
        ),
    )

    selection = select_bybit_research_universe(
        instruments,
        tickers,
        observed_at_ms=_NOW_MS,
        policy=_policy(),
    )

    assert [item.symbol for item in selection.selected] == [
        "AAAUSDT",
        "BBBUSDT",
        "CCCUSDT",
    ]
    first = selection.selected[0]
    assert first.turnover_percentile > selection.selected[-1].turnover_percentile
    assert first.open_interest_percentile > selection.selected[-1].open_interest_percentile
    assert first.spread_quality_percentile > selection.selected[-1].spread_quality_percentile
    assert first.history_percentile > selection.selected[-1].history_percentile
    assert Decimal("0") <= first.score <= Decimal("1")


def test_market_guardrails_reject_illiquid_wide_and_extreme_funding_candidates() -> None:
    instruments = (
        _instrument("LOWUSDT"),
        _instrument("WIDEUSDT"),
        _instrument("FUNDUSDT"),
    )
    tickers = (
        _ticker("LOWUSDT", turnover="10", oi_value="10"),
        _ticker(
            "WIDEUSDT",
            turnover="100000000",
            oi_value="100000000",
            bid="90",
            ask="110",
        ),
        _ticker(
            "FUNDUSDT",
            turnover="100000000",
            oi_value="100000000",
            funding="0.02",
        ),
    )

    selection = select_bybit_research_universe(
        instruments,
        tickers,
        observed_at_ms=_NOW_MS,
        policy=_policy(top_n=1),
    )

    assert selection.selected == ()
    assert selection.complete_top_n is False
    assert selection.blockers == ("INSUFFICIENT_ELIGIBLE_SYMBOLS",)
    assert set(selection.excluded_reasons["LOWUSDT"]) == {
        "TURNOVER_24H_BELOW_MINIMUM",
        "OPEN_INTEREST_VALUE_BELOW_MINIMUM",
    }
    assert "SPREAD_ABOVE_MAXIMUM" in selection.excluded_reasons["WIDEUSDT"]
    assert selection.excluded_reasons["FUNDUSDT"] == ("FUNDING_RATE_EXTREME",)


def test_missing_ticker_is_explicit_and_duplicate_inputs_fail_closed() -> None:
    selection = select_bybit_research_universe(
        (_instrument("BTCUSDT"),),
        (),
        observed_at_ms=_NOW_MS,
        policy=_policy(top_n=1),
    )
    assert selection.excluded_reasons == {"BTCUSDT": ("TICKER_MISSING",)}

    with pytest.raises(ValueError, match="duplicate symbol"):
        select_bybit_research_universe(
            (_instrument("BTCUSDT"), _instrument("BTCUSDT")),
            (),
            observed_at_ms=_NOW_MS,
            policy=_policy(top_n=1),
        )


def test_only_audited_bybit_mainnet_hosts_are_accepted() -> None:
    assert validate_bybit_public_research_host("api.bybit.com") == "api.bybit.com"
    assert validate_bybit_public_research_host("api.bybit.eu") == "api.bybit.eu"
    with pytest.raises(ValueError, match="allowlist"):
        validate_bybit_public_research_host("example.com")
    with pytest.raises(ValueError, match="allowlist"):
        validate_bybit_public_research_host("API.BYBIT.COM")


class _FakeTransport:
    def __init__(self, responses: list[BybitResearchUniverseHttpJson]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def __call__(
        self,
        url: str,
        headers: Mapping[str, str],
    ) -> BybitResearchUniverseHttpJson:
        assert headers == {"Accept": "application/json"}
        self.urls.append(url)
        return self.responses.pop(0)


def _http(payload: Mapping[str, Any]) -> BybitResearchUniverseHttpJson:
    return BybitResearchUniverseHttpJson(status_code=200, headers={}, payload=payload)


def _instrument_row(symbol: str, *, launch_days_ago: int = 400) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "contractType": "LinearPerpetual",
        "status": "Trading",
        "baseCoin": symbol.removesuffix("USDT"),
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "symbolType": "innovation",
        "launchTime": str(_NOW_MS - launch_days_ago * _DAY_MS),
        "deliveryTime": "0",
        "isPreListing": False,
    }


def _ticker_row(symbol: str, *, turnover: str, oi: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "lastPrice": "100",
        "bid1Price": "99.99",
        "ask1Price": "100.01",
        "turnover24h": turnover,
        "volume24h": "1000000",
        "openInterest": "500000",
        "openInterestValue": oi,
        "fundingRate": "0.0001",
        "price24hPcnt": "0.01",
    }


def test_client_paginates_all_linear_instruments_then_fetches_one_ticker_snapshot() -> None:
    transport = _FakeTransport(
        [
            _http(
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "category": "linear",
                        "list": [_instrument_row("BTCUSDT")],
                        "nextPageCursor": "page-2",
                    },
                }
            ),
            _http(
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "category": "linear",
                        "list": [_instrument_row("ETHUSDT")],
                        "nextPageCursor": "",
                    },
                }
            ),
            _http(
                {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "category": "linear",
                        "list": [
                            _ticker_row("BTCUSDT", turnover="100000000", oi="50000000"),
                            _ticker_row("ETHUSDT", turnover="90000000", oi="40000000"),
                        ],
                    },
                }
            ),
        ]
    )
    client = BybitResearchUniverseClient(
        host="api.bybit.eu",
        transport=transport,
    )

    selection = client.fetch_and_select(
        observed_at_ms=_NOW_MS,
        policy=_policy(top_n=2),
    )

    assert selection.host == "api.bybit.eu"
    assert [item.symbol for item in selection.selected] == ["BTCUSDT", "ETHUSDT"]
    assert len(transport.urls) == 3
    assert transport.urls[0].startswith(
        "https://api.bybit.eu/v5/market/instruments-info?category=linear&limit=1000"
    )
    assert "cursor=page-2" in transport.urls[1]
    assert transport.urls[2] == "https://api.bybit.eu/v5/market/tickers?category=linear"
    assert client.live_mainnet_order_routing_allowed is False
    assert client.order_writes_supported is False
    assert not hasattr(client, "place_order")
    assert not hasattr(client, "cancel_order")


def test_client_fails_closed_on_repeated_cursor_and_nonzero_ret_code() -> None:
    repeated = _FakeTransport(
        [
            _http(
                {
                    "retCode": 0,
                    "result": {
                        "category": "linear",
                        "list": [_instrument_row("BTCUSDT")],
                        "nextPageCursor": "same",
                    },
                }
            ),
            _http(
                {
                    "retCode": 0,
                    "result": {
                        "category": "linear",
                        "list": [_instrument_row("ETHUSDT")],
                        "nextPageCursor": "same",
                    },
                }
            ),
        ]
    )
    with pytest.raises(ValueError, match="cursor"):
        BybitResearchUniverseClient(transport=repeated).fetch_instruments()

    failed = _FakeTransport(
        [
            _http(
                {
                    "retCode": 10001,
                    "retMsg": "bad request",
                    "result": {"category": "linear", "list": []},
                }
            )
        ]
    )
    with pytest.raises(ValueError, match="API error"):
        BybitResearchUniverseClient(transport=failed).fetch_tickers()
