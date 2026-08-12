import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

from app.execution.bybit_demo import (
    BybitDemoHttpJson,
    BybitDemoOrderClient,
    BybitDemoOrderRequest,
)
from app.marketdata.bybit_v5 import (
    BybitHttpJson,
    BybitKlineBar,
    BybitKlineRequest,
    BybitPublicKlineClient,
    last_completed_kline_end_ms,
)
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSide,
    CryptoSignal,
    build_trade_plan,
    evaluate_crypto_signal,
    execution_levels,
)


def _trend_bars(symbol: str, *, direction: int, count: int = 36) -> tuple[BybitKlineBar, ...]:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    base = Decimal("100") if direction > 0 else Decimal("130")
    bars = []
    for index in range(count):
        close = base + Decimal(direction) * Decimal("0.5") * Decimal(index)
        bars.append(
            BybitKlineBar(
                symbol=symbol,
                start_time=start + timedelta(minutes=5 * index),
                open=close - Decimal(direction) * Decimal("0.1"),
                high=close * Decimal("1.002"),
                low=close * Decimal("0.998"),
                close=close,
                volume=Decimal("10000"),
                turnover=Decimal("1000000"),
            )
        )
    return tuple(bars)


def _research_config() -> CryptoPerpStrategyConfig:
    return CryptoPerpStrategyConfig(
        minimum_average_turnover_usdt=Decimal("1000"),
        minimum_atr_fraction=Decimal("0.0001"),
        maximum_atr_fraction=Decimal("0.10"),
        minimum_abs_momentum=Decimal("0.001"),
        minimum_quality_score=Decimal("0.10"),
        maximum_one_bar_atr_multiple=Decimal("5"),
    )


def test_completed_kline_cutoff_excludes_current_five_minute_bucket() -> None:
    interval_ms = 5 * 60 * 1000
    now_ms = 10 * interval_ms + 12_345

    assert last_completed_kline_end_ms(now_ms=now_ms, interval="5") == 10 * interval_ms - 1


def test_public_kline_client_normalizes_reverse_bybit_rows() -> None:
    calls: list[str] = []

    def transport(url: str, _headers: dict[str, str]) -> BybitHttpJson:
        calls.append(url)
        query = parse_qs(urlsplit(url).query)
        symbol = query["symbol"][0]
        rows = [
            ["600000", "101", "102", "100", "101.5", "10", "10000"],
            ["300000", "100", "101", "99", "100.5", "11", "11000"],
        ]
        return BybitHttpJson(
            status_code=200,
            headers={},
            payload={
                "retCode": 0,
                "retMsg": "OK",
                "result": {"symbol": symbol, "category": "linear", "list": rows},
            },
        )

    request = BybitKlineRequest(
        symbols=("BTCUSDT", "ETHUSDT"),
        start_ms=300_000,
        end_ms=600_000,
        interval="5",
    )
    acquisition = BybitPublicKlineClient(transport=transport).fetch(request)

    assert acquisition.symbols == ("BTCUSDT", "ETHUSDT")
    assert acquisition.counts_by_symbol() == {"BTCUSDT": 2, "ETHUSDT": 2}
    assert len(calls) == 2
    btc = [bar for bar in acquisition.bars if bar.symbol == "BTCUSDT"]
    assert [bar.start_time.timestamp() for bar in btc] == [300.0, 600.0]
    assert btc[-1].close == Decimal("101.5")


def test_crypto_signal_core_is_symmetric_for_long_and_short() -> None:
    config = _research_config()

    long_evaluation = evaluate_crypto_signal(_trend_bars("BTCUSDT", direction=1), config)
    short_evaluation = evaluate_crypto_signal(_trend_bars("ETHUSDT", direction=-1), config)

    assert long_evaluation.eligible is True
    assert long_evaluation.signal is not None
    assert long_evaluation.signal.side is CryptoSide.LONG
    assert short_evaluation.eligible is True
    assert short_evaluation.signal is not None
    assert short_evaluation.signal.side is CryptoSide.SHORT
    assert long_evaluation.signal.quality_score > 0
    assert short_evaluation.signal.quality_score > 0


def test_trade_plan_treats_dollar_target_as_net_edge_gate() -> None:
    signal = CryptoSignal(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        reference_price=Decimal("100000"),
        momentum=Decimal("0.01"),
        atr_fraction=Decimal("0.004"),
        fast_ema=Decimal("100100"),
        slow_ema=Decimal("99900"),
        breakout_strength_atr=Decimal("1.0"),
        one_bar_atr_multiple=Decimal("0.8"),
        average_turnover_usdt=Decimal("10000000"),
        quality_score=Decimal("3"),
        decision_time="2026-08-12T18:00:00+00:00",
    )
    base = _research_config()
    base = CryptoPerpStrategyConfig(
        **{
            **base.__dict__,
            "risk_fraction_per_trade": Decimal("0.01"),
            "expected_move_atr_multiple": Decimal("3"),
        }
    )

    target_15 = build_trade_plan(
        signal,
        equity_usdt=Decimal("1000"),
        config=base.with_target(Decimal("15")),
    )
    target_25 = build_trade_plan(
        signal,
        equity_usdt=Decimal("1000"),
        config=base.with_target(Decimal("25")),
    )

    assert target_15.eligible is True
    assert target_15.plan is not None
    assert target_15.plan.notional_usdt == Decimal("2000.0")
    assert target_15.plan.expected_net_edge_usd > Decimal("15")
    assert target_25.eligible is False
    assert "EXPECTED_NET_PROFIT_BELOW_TARGET" in target_25.reasons


def test_execution_levels_are_directionally_symmetric() -> None:
    base = _research_config()
    signal = CryptoSignal(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        reference_price=Decimal("100"),
        momentum=Decimal("0.01"),
        atr_fraction=Decimal("0.01"),
        fast_ema=Decimal("101"),
        slow_ema=Decimal("99"),
        breakout_strength_atr=Decimal("1"),
        one_bar_atr_multiple=Decimal("1"),
        average_turnover_usdt=Decimal("1000000"),
        quality_score=Decimal("3"),
        decision_time="2026-08-12T18:00:00+00:00",
    )
    config = base.with_target(Decimal("1"))
    long_evaluation = build_trade_plan(signal, equity_usdt=Decimal("1000"), config=config)
    assert long_evaluation.plan is not None
    long_levels = execution_levels(
        long_evaluation.plan,
        entry_price=Decimal("100"),
        config=config,
    )

    short_signal = CryptoSignal(**{**signal.__dict__, "side": CryptoSide.SHORT})
    short_evaluation = build_trade_plan(
        short_signal,
        equity_usdt=Decimal("1000"),
        config=config,
    )
    assert short_evaluation.plan is not None
    short_levels = execution_levels(
        short_evaluation.plan,
        entry_price=Decimal("100"),
        config=config,
    )

    assert long_levels.stop_price < long_levels.entry_price < long_levels.target_price
    assert short_levels.target_price < short_levels.entry_price < short_levels.stop_price
    assert long_levels.entry_price - long_levels.stop_price == (
        short_levels.stop_price - short_levels.entry_price
    )


def test_demo_gateway_signs_and_routes_market_order_only_to_demo_contract() -> None:
    captured: dict[str, object] = {}
    timestamp = 1_700_000_000_000

    def transport(
        method: str,
        url: str,
        headers: dict[str, str],
        body: str | None,
    ) -> BybitDemoHttpJson:
        captured.update(method=method, url=url, headers=headers, body=body)
        parsed_body = json.loads(body or "{}")
        return BybitDemoHttpJson(
            status_code=200,
            headers={},
            payload={
                "retCode": 0,
                "retMsg": "OK",
                "result": {
                    "orderId": "demo-order-1",
                    "orderLinkId": parsed_body["orderLinkId"],
                },
            },
        )

    client = BybitDemoOrderClient(
        api_key="demo-key",
        api_secret="demo-secret",
        transport=transport,
        clock_ms=lambda: timestamp,
    )
    request = BybitDemoOrderRequest(
        symbol="BTCUSDT",
        side="Buy",
        quantity=Decimal("0.01"),
        order_link_id="ASTRA-DEMO-ABC123",
    )
    acknowledgement = client.place_market_order(request)

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api-demo.bybit.com/v5/order/create"
    payload = json.loads(str(captured["body"]))
    assert payload["category"] == "linear"
    assert payload["orderType"] == "Market"
    assert payload["qty"] == "0.01"
    assert payload["reduceOnly"] is False
    assert payload["orderLinkId"] == request.order_link_id
    headers = captured["headers"]
    assert isinstance(headers, dict)
    signing_body = str(captured["body"])
    plain = f"{timestamp}demo-key5000{signing_body}"
    expected_signature = hmac.new(
        b"demo-secret",
        plain.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-BAPI-SIGN"] == expected_signature
    assert client.environment == "BYBIT_DEMO"
    assert client.live_mainnet_order_routing_allowed is False
    assert acknowledgement.accepted is True
    assert acknowledgement.live_mainnet_order is False
