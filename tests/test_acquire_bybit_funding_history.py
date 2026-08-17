from datetime import UTC, datetime
from decimal import Decimal

from app.marketdata.bybit_funding import (
    BybitFundingHistory,
    BybitFundingRateRecord,
)
from tools.acquire_bybit_funding_history import acquire_funding_history


class _Client:
    def __init__(self, *, fail_symbol: str | None = None) -> None:
        self.fail_symbol = fail_symbol

    def fetch_history(
        self,
        *,
        symbol: str,
        start_time: datetime,
        end_time: datetime,
    ) -> BybitFundingHistory:
        if symbol == self.fail_symbol:
            raise RuntimeError("blocked")
        record = BybitFundingRateRecord(
            symbol=symbol,
            funding_time=start_time,
            funding_rate=Decimal("0.0001"),
        )
        return BybitFundingHistory(
            symbol=symbol,
            start_time=start_time,
            end_time=end_time,
            records=(record,),
            request_count=1,
        )


def test_complete_funding_acquisition_never_claims_usdt_impact_without_marks() -> None:
    report = acquire_funding_history(
        symbols=("BTCUSDT", "ETHUSDT"),
        lookback_days=2,
        client=_Client(),
        now=datetime(2026, 8, 17, 12, tzinfo=UTC),
    )

    assert report["qualification"] == "PASS_BYBIT_FUNDING_HISTORY_ACQUISITION"
    assert report["archive_dates"] == ["2026-08-15", "2026-08-16"]
    assert report["record_counts_by_symbol"] == {"BTCUSDT": 1, "ETHUSDT": 1}
    assert report["blocked_symbols"] == {}
    assert report["blocked_details_by_symbol"] == {}
    assert report["mark_price_evidence_included"] is False
    assert report["funding_usdt_impact_calculated"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["live_activation_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False


def test_external_access_failure_is_normalized_not_misreported_as_pass() -> None:
    report = acquire_funding_history(
        symbols=("BTCUSDT", "ETHUSDT"),
        lookback_days=2,
        client=_Client(fail_symbol="ETHUSDT"),
        now=datetime(2026, 8, 17, 12, tzinfo=UTC),
    )

    assert report["qualification"] == "BLOCKED_BYBIT_FUNDING_EXTERNAL_ACCESS"
    assert report["record_counts_by_symbol"] == {"BTCUSDT": 1}
    assert report["blocked_symbols"] == {"ETHUSDT": "RuntimeError"}
    assert report["blocked_details_by_symbol"] == {"ETHUSDT": "RuntimeError"}
    assert report["funding_usdt_impact_calculated"] is False
