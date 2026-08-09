from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.runtime.paper_broker_contract_v99 import (
    BrokerAccount,
    BrokerMutationError,
    BrokerOrder,
    BrokerOrderStatus,
    OrderSide,
)
from app.runtime.paper_broker_roundtrip_v99 import (
    AdmissionEvidenceV99,
    FileRoundTripJournalV99,
    JournalCorruption,
    PaperBrokerRoundTripServiceV99,
    RoundTripOutcome,
    RoundTripPlanV99,
    RoundTripPolicyV99,
    RoundTripState,
    StaleGeneration,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


class FakeBroker:
    paper_order_writes_enabled = True

    def __init__(self) -> None:
        self.account = BrokerAccount("paper-1", "ACTIVE", "USD", Decimal("10000"))
        self.orders: dict[str, BrokerOrder] = {}
        self.submit_calls = 0
        self.replace_calls = 0
        self.cancel_calls = 0
        self.ambiguous_submit = False
        self.fill_on_submit = False

    def get_account(self) -> BrokerAccount:
        return self.account

    def list_open_orders(self):
        return [value for value in self.orders.values() if value.status is not BrokerOrderStatus.CANCELLED]

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_calls += 1
        order = BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id="broker-1",
            instrument=kwargs["instrument"],
            side=kwargs["side"],
            quantity=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=BrokerOrderStatus.FILLED if self.fill_on_submit else BrokerOrderStatus.ACKNOWLEDGED,
            filled_quantity=kwargs["quantity"] if self.fill_on_submit else Decimal("0"),
            updated_at=NOW,
        )
        self.orders[order.client_order_id] = order
        if self.ambiguous_submit:
            raise BrokerMutationError("TIMEOUT", "ambiguous submit", ambiguous=True)
        return order

    def replace_limit_order(self, *, broker_order_id: str, limit_price: Decimal) -> BrokerOrder:
        self.replace_calls += 1
        old = next(value for value in self.orders.values() if value.broker_order_id == broker_order_id)
        order = replace(old, limit_price=limit_price, status=BrokerOrderStatus.REPLACED, updated_at=NOW)
        self.orders[order.client_order_id] = order
        return order

    def cancel_order(self, *, broker_order_id: str) -> BrokerOrder:
        self.cancel_calls += 1
        old = next(value for value in self.orders.values() if value.broker_order_id == broker_order_id)
        order = replace(old, status=BrokerOrderStatus.CANCELLED, updated_at=NOW)
        self.orders[order.client_order_id] = order
        return order

    def get_order_by_client_order_id(self, client_order_id: str):
        return self.orders.get(client_order_id)


def plan() -> RoundTripPlanV99:
    return RoundTripPlanV99(
        round_trip_id="rt-1",
        session_id="session-1",
        account_id="paper-1",
        generation=7,
        client_order_id="astra-rt-1",
        instrument="AAPL",
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal("100"),
        replacement_limit_price=Decimal("99"),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=2),
        operator_approval_id="approval-1",
        approval_expires_at=NOW + timedelta(minutes=3),
        decision_digest="a" * 64,
    ).sealed()


def evidence(**changes) -> AdmissionEvidenceV99:
    values = dict(
        session_id="session-1",
        generation=7,
        captured_at=NOW,
        session_running=True,
        paper_order_submission_allowed=True,
        platform_ready=True,
        broker_reliability_ready=True,
        qualification_ready=True,
        kill_switch_engaged=False,
        digest="b" * 64,
    )
    values.update(changes)
    return AdmissionEvidenceV99(**values)


def service(tmp_path: Path, broker: FakeBroker, **kwargs) -> PaperBrokerRoundTripServiceV99:
    return PaperBrokerRoundTripServiceV99(
        broker=broker,
        plan=kwargs.get("plan", plan()),
        evidence=kwargs.get("evidence", evidence()),
        journal=FileRoundTripJournalV99(tmp_path / "roundtrip.jsonl"),
        policy=kwargs.get("policy", RoundTripPolicyV99(allowed_instruments=frozenset({"AAPL"}))),
    )


def test_clean_submit_replace_cancel_reconcile(tmp_path: Path) -> None:
    broker = FakeBroker()
    result = service(tmp_path, broker).execute(now=NOW + timedelta(seconds=5), expected_generation=7)
    assert result.success
    assert result.state is RoundTripState.COMPLETED
    assert result.outcome is RoundTripOutcome.CANCELLED_CLEAN
    assert result.paper_broker_mutation_verified
    assert (broker.submit_calls, broker.replace_calls, broker.cancel_calls) == (1, 1, 1)
    assert not result.external_order_routing_allowed
    assert not result.live_trading_allowed


def test_second_execute_is_idempotent(tmp_path: Path) -> None:
    broker = FakeBroker()
    runtime = service(tmp_path, broker)
    first = runtime.execute(now=NOW + timedelta(seconds=5), expected_generation=7)
    second = runtime.execute(now=NOW + timedelta(seconds=6), expected_generation=7)
    assert second == first
    assert broker.submit_calls == 1


def test_stale_generation_rejected_before_broker_call(tmp_path: Path) -> None:
    broker = FakeBroker()
    with pytest.raises(StaleGeneration):
        service(tmp_path, broker).execute(now=NOW + timedelta(seconds=5), expected_generation=6)
    assert broker.submit_calls == 0


def test_stale_evidence_blocks_without_mutation(tmp_path: Path) -> None:
    broker = FakeBroker()
    runtime = service(tmp_path, broker, evidence=evidence(captured_at=NOW - timedelta(minutes=5)))
    result = runtime.execute(now=NOW + timedelta(seconds=5), expected_generation=7)
    assert result.state is RoundTripState.BLOCKED
    assert "SESSION_EVIDENCE_STALE" in result.reasons
    assert broker.submit_calls == 0


def test_ambiguous_submit_is_recovered_by_read_only_lookup(tmp_path: Path) -> None:
    broker = FakeBroker()
    broker.ambiguous_submit = True
    result = service(tmp_path, broker).execute(now=NOW + timedelta(seconds=5), expected_generation=7)
    assert result.success
    assert broker.submit_calls == 1


def test_fill_blocks_and_preserves_residual_exposure_evidence(tmp_path: Path) -> None:
    broker = FakeBroker()
    broker.fill_on_submit = True
    result = service(tmp_path, broker).execute(now=NOW + timedelta(seconds=5), expected_generation=7)
    assert result.state is RoundTripState.BLOCKED
    assert result.outcome is RoundTripOutcome.RESIDUAL_PAPER_EXPOSURE
    assert result.filled_quantity == Decimal("1")
    assert broker.cancel_calls == 0


def test_kill_switch_blocks(tmp_path: Path) -> None:
    broker = FakeBroker()
    result = service(tmp_path, broker, evidence=evidence(kill_switch_engaged=True)).execute(
        now=NOW + timedelta(seconds=5), expected_generation=7
    )
    assert "KILL_SWITCH_ENGAGED" in result.reasons
    assert broker.submit_calls == 0


def test_plan_rejects_live_flags() -> None:
    with pytest.raises(ValueError):
        replace(plan(), live_trading_allowed=True, digest="").sealed()


def test_journal_detects_tampering(tmp_path: Path) -> None:
    broker = FakeBroker()
    runtime = service(tmp_path, broker)
    runtime.execute(now=NOW + timedelta(seconds=5), expected_generation=7)
    path = tmp_path / "roundtrip.jsonl"
    path.write_text(path.read_text().replace("CANCELLED_CLEAN", "BROKER_REJECTED"), encoding="utf-8")
    with pytest.raises(JournalCorruption):
        FileRoundTripJournalV99(path).load()
