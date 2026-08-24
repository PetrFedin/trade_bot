from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.marketdata.bybit_liquidation_forward import (
    BybitLiquidationProtocolError,
    aggregate_liquidations_5m,
    build_all_liquidation_topics,
    parse_bybit_all_liquidation_message,
    validate_bybit_public_liquidation_ws_host,
)


def _payload() -> dict[str, object]:
    return {
        "topic": "allLiquidation.BTCUSDT",
        "type": "snapshot",
        "ts": 1760000123456,
        "data": [
            {
                "T": 1760000123001,
                "s": "BTCUSDT",
                "S": "Buy",
                "v": "2",
                "p": "100",
            },
            {
                "T": 1760000123999,
                "s": "BTCUSDT",
                "S": "Sell",
                "v": "1",
                "p": "110",
            },
        ],
    }


def test_parse_preserves_bybit_position_side_semantics_and_estimated_notional() -> None:
    events = parse_bybit_all_liquidation_message(
        _payload(),
        expected_symbols=("BTCUSDT", "ETHUSDT"),
    )

    assert len(events) == 2
    long_liquidation, short_liquidation = events
    assert long_liquidation.raw_position_side == "Buy"
    assert long_liquidation.liquidated_position_side == "LONG"
    assert long_liquidation.estimated_notional_usdt == Decimal("200")
    assert short_liquidation.raw_position_side == "Sell"
    assert short_liquidation.liquidated_position_side == "SHORT"
    assert short_liquidation.estimated_notional_usdt == Decimal("110")
    assert long_liquidation.exchange_event_id_available is False
    assert long_liquidation.historical_backfill_available is False
    assert long_liquidation.trade_actionable is False
    assert long_liquidation.live_mainnet_order_routing_allowed is False


def test_liquidation_event_identity_is_deterministic_for_exact_message_occurrence() -> None:
    first = parse_bybit_all_liquidation_message(_payload())
    second = parse_bybit_all_liquidation_message(_payload())

    assert [row.event_id for row in first] == [row.event_id for row in second]
    assert first[0].event_id != first[1].event_id


def test_liquidation_five_minute_aggregate_reconciles_sides_and_imbalance() -> None:
    events = parse_bybit_all_liquidation_message(_payload())

    buckets = aggregate_liquidations_5m(events)

    assert len(buckets) == 1
    bucket = buckets[0]
    assert bucket.event_count == 2
    assert bucket.long_liquidation_count == 1
    assert bucket.short_liquidation_count == 1
    assert bucket.long_estimated_notional_usdt == Decimal("200")
    assert bucket.short_estimated_notional_usdt == Decimal("110")
    assert bucket.total_estimated_notional_usdt == Decimal("310")
    assert bucket.long_minus_short_estimated_notional_usdt == Decimal("90")
    assert bucket.normalized_long_minus_short_imbalance == Decimal("90") / Decimal("310")
    assert bucket.largest_event_estimated_notional_usdt == Decimal("200")


def test_liquidation_parser_rejects_topic_row_and_subscription_drift() -> None:
    payload = _payload()
    payload["topic"] = "allLiquidation.ETHUSDT"
    with pytest.raises(BybitLiquidationProtocolError, match="does not match topic"):
        parse_bybit_all_liquidation_message(payload)

    with pytest.raises(BybitLiquidationProtocolError, match="outside subscription"):
        parse_bybit_all_liquidation_message(_payload(), expected_symbols=("ETHUSDT",))


def test_liquidation_parser_rejects_invalid_or_nonpositive_economics() -> None:
    payload = _payload()
    payload["data"] = [
        {
            "T": 1760000123001,
            "s": "BTCUSDT",
            "S": "Buy",
            "v": "NaN",
            "p": "100",
        }
    ]
    with pytest.raises(BybitLiquidationProtocolError, match="positive and finite"):
        parse_bybit_all_liquidation_message(payload)

    payload = _payload()
    payload["data"] = [
        {
            "T": 1760000123001,
            "s": "BTCUSDT",
            "S": "Buy",
            "v": "1",
            "p": "0",
        }
    ]
    with pytest.raises(BybitLiquidationProtocolError, match="positive and finite"):
        parse_bybit_all_liquidation_message(payload)


def test_liquidation_event_cannot_claim_unavailable_backfill_or_live_routing() -> None:
    event = parse_bybit_all_liquidation_message(_payload())[0]

    with pytest.raises(ValueError, match="cannot claim unavailable/live capabilities"):
        replace(event, historical_backfill_available=True).validate()
    with pytest.raises(ValueError, match="cannot claim unavailable/live capabilities"):
        replace(event, live_mainnet_order_routing_allowed=True).validate()


def test_liquidation_topics_and_public_hosts_are_strictly_bounded() -> None:
    assert build_all_liquidation_topics(("BTCUSDT", "ETHUSDT")) == (
        "allLiquidation.BTCUSDT",
        "allLiquidation.ETHUSDT",
    )
    with pytest.raises(ValueError, match="duplicate"):
        build_all_liquidation_topics(("BTCUSDT", "BTCUSDT"))
    with pytest.raises(ValueError, match="1..50"):
        build_all_liquidation_topics(())
    assert validate_bybit_public_liquidation_ws_host("stream.bybit.com") == "stream.bybit.com"
    with pytest.raises(ValueError, match="not allowlisted"):
        validate_bybit_public_liquidation_ws_host("example.com")
