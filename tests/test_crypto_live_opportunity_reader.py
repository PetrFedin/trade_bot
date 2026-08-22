from __future__ import annotations

from app.strategy.crypto_live_opportunity_reader import PostgresCryptoLiveOpportunityReader


def test_review_queue_reader_exposes_no_order_surface() -> None:
    reader = PostgresCryptoLiveOpportunityReader("postgresql://example.invalid/astra")
    assert reader.live_mainnet_order_routing_allowed is False
    assert reader.order_writes_supported is False
    assert not hasattr(reader, "place_order")
    assert not hasattr(reader, "cancel_order")
    assert not hasattr(reader, "amend_order")


def test_review_queue_limit_fails_before_any_database_connection() -> None:
    reader = PostgresCryptoLiveOpportunityReader("postgresql://example.invalid/astra")
    for invalid in (0, 51, True):
        try:
            reader.latest_review_queue(limit=invalid)
        except ValueError as exc:
            assert "within [1, 50]" in str(exc)
        else:
            raise AssertionError("invalid review queue limit must fail closed")
