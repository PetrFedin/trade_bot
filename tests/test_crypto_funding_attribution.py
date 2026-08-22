from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_derivatives_history import (
    BybitDerivativesHistory,
    BybitHistoricalFundingPoint,
)
from app.marketdata.bybit_mark_price_history import (
    BybitMarkPriceHistory,
    BybitMarkPricePoint,
)
from app.strategy.crypto_funding_attribution import (
    build_crypto_funding_attribution,
    diagnose_crypto_funding_attribution,
)

_START = datetime(2026, 1, 1, tzinfo=UTC)


def _ms(hours: int) -> int:
    return int((_START + timedelta(hours=hours)).timestamp() * 1000)


def _replay(*, side: str = "LONG", quantity: str = "0.01") -> dict[str, object]:
    return {
        "closed_trades": [
            {
                "symbol": "BTCUSDT",
                "side": side,
                "entry_time": (_START + timedelta(hours=1)).isoformat(),
                "exit_time": (_START + timedelta(hours=10)).isoformat(),
                "quantity": float(Decimal(quantity)),
                "net_pnl_usdt": 12.0,
            }
        ],
        "strategy_promotion_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def _derivatives(*, rate: str = "0.0001") -> BybitDerivativesHistory:
    return BybitDerivativesHistory(
        symbol="BTCUSDT",
        start_ms=_ms(0),
        end_ms=_ms(12),
        interval="1h",
        open_interest=(),
        account_ratio=(),
        funding=(
            BybitHistoricalFundingPoint("BTCUSDT", _ms(0), Decimal("0.0002")),
            BybitHistoricalFundingPoint("BTCUSDT", _ms(8), Decimal(rate)),
        ),
        request_count=3,
        host="api.bybit.com",
    )


def _mark_history(*, price: str = "100000", include_settlement: bool = True) -> BybitMarkPriceHistory:
    points = []
    if include_settlement:
        points.append(
            BybitMarkPricePoint(
                symbol="BTCUSDT",
                start_time_ms=_ms(8),
                open_price=Decimal(price),
                high_price=Decimal(price) + Decimal("100"),
                low_price=Decimal(price) - Decimal("100"),
                close_price=Decimal(price),
            )
        )
    return BybitMarkPriceHistory(
        symbol="BTCUSDT",
        start_ms=_ms(0),
        end_ms=_ms(12),
        interval="60",
        points=tuple(points),
        request_count=1,
        host="api.bybit.com",
    )


def test_positive_funding_long_pays_and_short_receives_using_exact_mark_open() -> None:
    long_trade = build_crypto_funding_attribution(
        _replay(side="LONG"),
        {"BTCUSDT": _derivatives(rate="0.0001")},
        {"BTCUSDT": _mark_history(price="100000")},
    )[0]
    short_trade = build_crypto_funding_attribution(
        _replay(side="SHORT"),
        {"BTCUSDT": _derivatives(rate="0.0001")},
        {"BTCUSDT": _mark_history(price="100000")},
    )[0]

    assert long_trade.complete is True
    assert long_trade.settlement_count == 1
    settlement = long_trade.settlements[0]
    assert settlement.quantity == Decimal("0.01")
    assert settlement.mark_price_usdt == Decimal("100000")
    assert settlement.position_value_usdt == Decimal("1000.00")
    assert settlement.funding_pnl_usdt == Decimal("-0.100000")
    assert long_trade.funding_pnl_usdt == Decimal("-0.100000")
    assert long_trade.net_pnl_after_funding_usdt == Decimal("11.900000")
    assert short_trade.funding_pnl_usdt == Decimal("0.100000")
    assert short_trade.net_pnl_after_funding_usdt == Decimal("12.100000")


def test_negative_funding_reverses_long_short_economics() -> None:
    long_trade = build_crypto_funding_attribution(
        _replay(side="LONG"),
        {"BTCUSDT": _derivatives(rate="-0.0002")},
        {"BTCUSDT": _mark_history(price="100000")},
    )[0]
    short_trade = build_crypto_funding_attribution(
        _replay(side="SHORT"),
        {"BTCUSDT": _derivatives(rate="-0.0002")},
        {"BTCUSDT": _mark_history(price="100000")},
    )[0]
    assert long_trade.funding_pnl_usdt == Decimal("0.200000")
    assert short_trade.funding_pnl_usdt == Decimal("-0.200000")


def test_exact_settlement_mark_is_required_and_never_interpolated() -> None:
    trade = build_crypto_funding_attribution(
        _replay(),
        {"BTCUSDT": _derivatives()},
        {"BTCUSDT": _mark_history(include_settlement=False)},
    )[0]
    assert trade.complete is False
    assert trade.funding_pnl_usdt is None
    assert trade.net_pnl_after_funding_usdt is None
    assert trade.settlement_count == 0
    assert trade.missing_reasons == (
        f"MARK_PRICE_AT_FUNDING_TIMESTAMP_MISSING:{_ms(8)}",
    )


def test_trade_crossing_no_funding_settlement_is_complete_without_mark_history() -> None:
    derivatives = BybitDerivativesHistory(
        symbol="BTCUSDT",
        start_ms=_ms(0),
        end_ms=_ms(12),
        interval="1h",
        open_interest=(),
        account_ratio=(),
        funding=(
            BybitHistoricalFundingPoint("BTCUSDT", _ms(0), Decimal("0.0001")),
            BybitHistoricalFundingPoint("BTCUSDT", _ms(12), Decimal("0.0001")),
        ),
        request_count=3,
        host="api.bybit.com",
    )
    trade = build_crypto_funding_attribution(
        _replay(),
        {"BTCUSDT": derivatives},
        {},
    )[0]
    assert trade.complete is True
    assert trade.funding_pnl_usdt == Decimal("0")
    assert trade.net_pnl_after_funding_usdt == Decimal("12.0")
    assert trade.settlement_count == 0


def test_missing_derivatives_or_mark_history_is_explicit() -> None:
    missing_derivatives = build_crypto_funding_attribution(
        _replay(),
        {},
        {},
    )[0]
    assert missing_derivatives.complete is False
    assert missing_derivatives.missing_reasons == ("DERIVATIVES_HISTORY_MISSING",)

    missing_mark = build_crypto_funding_attribution(
        _replay(),
        {"BTCUSDT": _derivatives()},
        {},
    )[0]
    assert missing_mark.complete is False
    assert missing_mark.missing_reasons == ("MARK_PRICE_HISTORY_MISSING",)


def test_diagnostics_reconcile_complete_funding_without_promoting_strategy() -> None:
    trade = build_crypto_funding_attribution(
        _replay(),
        {"BTCUSDT": _derivatives()},
        {"BTCUSDT": _mark_history()},
    )[0]
    report = diagnose_crypto_funding_attribution((trade,))
    assert report["complete_trade_count"] == 1
    assert report["incomplete_trade_count"] == 0
    assert report["complete_fraction"] == 1.0
    assert report["trades_crossing_funding"] == 1
    assert report["settlement_count"] == 1
    assert report["replay_net_pnl_usdt_complete_trades"] == 12.0
    assert report["reconstructed_funding_pnl_usdt"] == pytest.approx(-0.1)
    assert report["net_pnl_after_reconstructed_funding_usdt"] == pytest.approx(11.9)
    assert report["funding_dollar_cost_reconciled"] is True
    assert report["strategy_selection_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["live_activation_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False
    assert report["causal_claim_allowed"] is False
    assert report["predictive_guarantee_allowed"] is False


def test_live_capable_replay_and_invalid_quantity_fail_closed() -> None:
    replay = _replay()
    replay["bybit_live_order_routing_allowed"] = True
    with pytest.raises(ValueError, match="bybit_live_order_routing_allowed=false"):
        build_crypto_funding_attribution(
            replay,
            {"BTCUSDT": _derivatives()},
            {"BTCUSDT": _mark_history()},
        )

    replay = _replay(quantity="0")
    with pytest.raises(ValueError, match="quantity must be positive"):
        build_crypto_funding_attribution(
            replay,
            {"BTCUSDT": _derivatives()},
            {"BTCUSDT": _mark_history()},
        )
