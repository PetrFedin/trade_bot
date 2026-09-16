from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.continuity import SQLiteOperationalContinuityStore
from app.marketdata.decision_leases import (
    DecisionLeasePolicy,
    DecisionSafetyEvidence,
    SQLiteDecisionLeaseStore,
    StaleDecisionLease,
    decision_safety_evidence_id,
)
from app.marketdata.operational import OperationalBar, SQLiteOperationalMarketDataStore

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
STRATEGY = "paper-momentum-v1"


def bar() -> OperationalBar:
    open_time = NOW - timedelta(minutes=10)
    close_time = open_time + timedelta(minutes=5)
    return OperationalBar(
        provider="BYBIT",
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        interval_seconds=300,
        open_time=open_time,
        close_time=close_time,
        source_timestamp=close_time - timedelta(milliseconds=1),
        received_at=close_time + timedelta(seconds=1),
        source_event_id="kline.5.BTCUSDT:test",
        is_final=True,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("10"),
        revision=0,
    )


def stack(tmp_path):
    path = tmp_path / "marketdata.sqlite"
    marketdata = SQLiteOperationalMarketDataStore(path)
    SQLiteOperationalContinuityStore(path)
    value = bar()
    ticket = marketdata.record_finalized_for_strategy(
        value,
        strategy_id=STRATEGY,
        recorded_at=NOW - timedelta(minutes=4),
    )
    leases = SQLiteDecisionLeaseStore(path)
    return path, marketdata, leases, ticket


def safety(receipt, *, observed_at: datetime) -> DecisionSafetyEvidence:
    evidence_id = decision_safety_evidence_id(
        ticket_id=receipt.ticket.ticket_id,
        owner_id=receipt.owner_id,
        release_identity=receipt.release_identity,
        fencing_token=receipt.fencing_token,
        checkpoint_id=None,
        first_bar_id=None,
        last_bar_id=None,
        bar_ids=(),
        continuity_reasons=("CONTINUITY_CHECKPOINT_REQUIRED",),
        readiness_reasons=(),
        control_mode="HALTED",
        control_version=1,
        observed_at=observed_at,
    )
    return DecisionSafetyEvidence(
        evidence_id=evidence_id,
        ticket_id=receipt.ticket.ticket_id,
        owner_id=receipt.owner_id,
        release_identity=receipt.release_identity,
        fencing_token=receipt.fencing_token,
        checkpoint_id=None,
        first_bar_id=None,
        last_bar_id=None,
        bar_ids=(),
        continuity_reasons=("CONTINUITY_CHECKPOINT_REQUIRED",),
        readiness_reasons=(),
        control_mode="HALTED",
        control_version=1,
        observed_at=observed_at,
    )


def test_active_lease_is_exclusive_and_expired_lease_reclaims_with_new_fence(tmp_path) -> None:
    _, _, leases, _ = stack(tmp_path)
    policy = DecisionLeasePolicy(lease_ttl=timedelta(seconds=5))
    first = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-a",
        release_identity="release-a",
        occurred_at=NOW,
        policy=policy,
    )
    assert first is not None and first.fencing_token == 1
    assert leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-b",
        release_identity="release-a",
        occurred_at=NOW + timedelta(seconds=4),
        policy=policy,
    ) is None

    second = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-b",
        release_identity="release-b",
        occurred_at=NOW + timedelta(seconds=5),
        policy=policy,
    )
    assert second is not None
    assert second.ticket.ticket_id == first.ticket.ticket_id
    assert second.fencing_token == 2

    with pytest.raises(StaleDecisionLease, match="stale"):
        leases.complete(
            first,
            outcome_id="planning:stale",
            occurred_at=NOW + timedelta(seconds=5),
        )
    with pytest.raises(ValueError, match="DECISION_READY_SAFETY_EVIDENCE_REQUIRED"):
        leases.complete(
            second,
            outcome_id="planning:current",
            occurred_at=NOW + timedelta(seconds=5, milliseconds=1),
        )
    assert leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-c",
        release_identity="release-c",
        occurred_at=NOW + timedelta(seconds=6),
        policy=policy,
    ) is None


def test_lease_is_expired_at_exact_expiry_boundary(tmp_path) -> None:
    _, _, leases, _ = stack(tmp_path)
    policy = DecisionLeasePolicy(lease_ttl=timedelta(seconds=5))
    first = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-a",
        release_identity="release-a",
        occurred_at=NOW,
        policy=policy,
    )
    assert first is not None
    with pytest.raises(StaleDecisionLease, match="expired"):
        leases.complete(
            first,
            outcome_id="planning:too-late",
            occurred_at=first.expires_at,
        )


def test_stale_fence_cannot_write_safety_evidence_after_reclaim(tmp_path) -> None:
    _, _, leases, _ = stack(tmp_path)
    policy = DecisionLeasePolicy(lease_ttl=timedelta(seconds=1))
    first = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-a",
        release_identity="release-a",
        occurred_at=NOW,
        policy=policy,
    )
    assert first is not None
    second = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-b",
        release_identity="release-b",
        occurred_at=NOW + timedelta(seconds=1),
        policy=policy,
    )
    assert second is not None and second.fencing_token == 2

    with pytest.raises(StaleDecisionLease, match="stale"):
        leases.record_safety(safety(first, observed_at=NOW + timedelta(seconds=1)))
    current = safety(second, observed_at=NOW + timedelta(seconds=1, milliseconds=1))
    assert leases.record_safety(current)
    assert not leases.record_safety(current)


def test_release_makes_ticket_immediately_reclaimable_and_increments_fence(tmp_path) -> None:
    _, _, leases, _ = stack(tmp_path)
    first = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-a",
        release_identity="release-a",
        occurred_at=NOW,
    )
    assert first is not None
    assert leases.release(first, occurred_at=NOW + timedelta(seconds=1))
    second = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-b",
        release_identity="release-b",
        occurred_at=NOW + timedelta(seconds=1),
    )
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1


def test_lease_event_and_safety_journals_are_append_only(tmp_path) -> None:
    path, _, leases, _ = stack(tmp_path)
    receipt = leases.claim_next(
        strategy_id=STRATEGY,
        owner_id="worker-a",
        release_identity="release-a",
        occurred_at=NOW,
    )
    assert receipt is not None
    evidence = safety(receipt, observed_at=NOW + timedelta(milliseconds=1))
    assert leases.record_safety(evidence)

    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "UPDATE operational_decision_lease_events SET owner_id='tampered'"
            )
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "DELETE FROM operational_decision_safety_evidence"
            )
    finally:
        connection.close()
