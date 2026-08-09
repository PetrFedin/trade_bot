from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from app.runtime.sandbox_qualification_v101 import (
    AccountSnapshot,
    AmbiguousMutation,
    Approval,
    ApprovalKey,
    ApprovalReplay,
    EventStore,
    KillSwitchStore,
    OrderSnapshot,
    OrderStatus,
    Plan,
    Policy,
    QualificationCorruption,
    QualificationError,
    QualificationService,
    Side,
    StaleGeneration,
    State,
    StreamEvidence,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def order(*, status=OrderStatus.ACKNOWLEDGED, price="10", filled="0", updated_at=NOW):
    return OrderSnapshot(
        client_order_id="astra-q-1",
        broker_order_id="broker-1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        limit_price=Decimal(price),
        status=status,
        filled_quantity=Decimal(filled),
        updated_at=updated_at,
    )


class Gateway:
    paper_only = True
    writes_enabled = True
    credential_fingerprint = "a" * 16
    rest_endpoint = QualificationService.PAPER_REST
    stream_endpoint = QualificationService.PAPER_STREAM

    def __init__(self):
        self.account = AccountSnapshot("acct-1", "ACTIVE", "USD", Decimal("1000"))
        self.open_orders = []
        self.current = None
        self.submit_calls = 0
        self.replace_calls = 0
        self.cancel_calls = 0
        self.list_calls = 0
        self.submit_error = None
        self.replace_error = None
        self.cancel_error = None
        self.lookup_error = None
        self.cleanup_open_reads = 0

    def get_account(self):
        return self.account

    def list_open_orders(self):
        self.list_calls += 1
        if self.cleanup_open_reads > 0 and self.current is not None:
            self.cleanup_open_reads -= 1
            return [self.current]
        return list(self.open_orders)

    def submit_limit_order(self, **kwargs):
        self.submit_calls += 1
        if self.submit_error:
            raise self.submit_error
        self.current = order(price=str(kwargs["limit_price"]))
        return self.current

    def replace_limit_order(self, **kwargs):
        self.replace_calls += 1
        if self.replace_error:
            raise self.replace_error
        self.current = order(status=OrderStatus.REPLACED, price=str(kwargs["limit_price"]))
        return self.current

    def cancel_order(self, **kwargs):
        self.cancel_calls += 1
        if self.cancel_error:
            raise self.cancel_error
        self.current = order(status=OrderStatus.CANCELLED, price="11")
        return self.current

    def get_order_by_client_order_id(self, client_order_id):
        if self.lookup_error:
            raise self.lookup_error
        return self.current


def plan(**changes):
    value = Plan(
        qualification_id="qual-1",
        generation=7,
        expected_account_id="acct-1",
        client_order_id="astra-q-1",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("1"),
        initial_limit_price=Decimal("10"),
        replacement_limit_price=Decimal("11"),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        approval_id="approval-1",
        require_replace=True,
    )
    return replace(value, **changes).seal()


def key():
    return ApprovalKey("x" * 40)


def approval(signing_key=None, **changes):
    value = Approval(
        approval_id="approval-1",
        operator_id="operator-a",
        nonce="nonce-0123456789abcdef",
        generation=7,
        account_id="acct-1",
        symbol="AAPL",
        side=Side.BUY,
        maximum_quantity=Decimal("1"),
        maximum_notional=Decimal("100"),
        issued_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        allow_paper_mutations=True,
    )
    return replace(value, **changes).seal(signing_key or key())


def stream(**changes):
    value = StreamEvidence(
        captured_at=NOW,
        generation=7,
        authenticated=True,
        listening=True,
        credential_fingerprint="a" * 16,
        rest_endpoint=QualificationService.PAPER_REST,
        stream_endpoint=QualificationService.PAPER_STREAM,
    )
    return replace(value, **changes)


def service(tmp_path: Path, gateway=None, selected_plan=None, selected_policy=None):
    return QualificationService(
        gateway=gateway or Gateway(),
        plan=selected_plan or plan(),
        approval_key=key(),
        event_store=EventStore(tmp_path / "events.jsonl"),
        kill_switch=KillSwitchStore(tmp_path / "kill.json"),
        policy=selected_policy or Policy(allowed_symbols=frozenset({"AAPL"})),
        sleeper=lambda _: None,
    )


def probe_and_arm(instance):
    assert instance.probe(now=NOW, expected_generation=7, stream=stream()).state is State.PROBE_VERIFIED
    assert instance.arm(approval=approval(), now=NOW + timedelta(seconds=1), expected_generation=7).state is State.ARMED


def test_happy_round_trip(tmp_path):
    instance = service(tmp_path)
    probe_and_arm(instance)
    result = instance.execute(now=NOW + timedelta(seconds=2), expected_generation=7)
    assert result.success
    assert result.state is State.VERIFIED
    assert result.read_only_probe_verified
    assert result.paper_round_trip_verified
    assert result.cleanup_verified
    assert not result.kill_switch_engaged
    assert len(instance.events) == 10
    assert instance.event_store.verify()


def test_round_trip_without_replace(tmp_path):
    gateway = Gateway()
    instance = service(tmp_path, gateway, plan(require_replace=False, replacement_limit_price=None))
    probe_and_arm(instance)
    result = instance.execute(now=NOW + timedelta(seconds=2), expected_generation=7)
    assert result.success
    assert gateway.replace_calls == 0


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"paper_only": False}, "NOT_PAPER_GATEWAY"),
        ({"rest_endpoint": "https://example.com"}, "REST_ENDPOINT_NOT_PAPER"),
        ({"stream_endpoint": "wss://example.com"}, "STREAM_ENDPOINT_NOT_PAPER"),
        ({"credential_fingerprint": "b" * 16}, "CREDENTIAL_FINGERPRINT_MISMATCH"),
    ],
)
def test_security_probe_quarantines(tmp_path, change, reason):
    gateway = Gateway()
    for name, value in change.items():
        setattr(gateway, name, value)
    instance = service(tmp_path, gateway)
    result = instance.probe(now=NOW, expected_generation=7, stream=stream())
    assert result.state is State.QUARANTINED
    assert reason in result.reasons
    assert result.kill_switch_engaged


def test_stream_generation_quarantines(tmp_path):
    instance = service(tmp_path)
    result = instance.probe(now=NOW, expected_generation=7, stream=stream(generation=8))
    assert result.state is State.QUARANTINED
    assert "STREAM_GENERATION_MISMATCH" in result.reasons


@pytest.mark.parametrize(
    "account_change,reason",
    [
        ({"account_id": "other"}, "ACCOUNT_ID_MISMATCH"),
        ({"status": "INACTIVE"}, "ACCOUNT_NOT_ACTIVE"),
        ({"currency": "EUR"}, "ACCOUNT_CURRENCY_MISMATCH"),
        ({"trading_blocked": True}, "ACCOUNT_NOT_TRADABLE"),
        ({"buying_power": Decimal("0")}, "ACCOUNT_NOT_TRADABLE"),
    ],
)
def test_account_probe_blocks(tmp_path, account_change, reason):
    gateway = Gateway()
    gateway.account = replace(gateway.account, **account_change)
    result = service(tmp_path, gateway).probe(now=NOW, expected_generation=7, stream=stream())
    assert result.state is State.BLOCKED
    assert reason in result.reasons


def test_open_order_baseline_blocks(tmp_path):
    gateway = Gateway()
    gateway.open_orders = [order()]
    result = service(tmp_path, gateway).probe(now=NOW, expected_generation=7, stream=stream())
    assert "OPEN_ORDER_BASELINE_NOT_EMPTY" in result.reasons
    assert "DUPLICATE_CLIENT_ORDER_ID" in result.reasons


def test_stream_not_ready_and_stale_plan_block(tmp_path):
    old = plan(created_at=NOW - timedelta(minutes=6), expires_at=NOW + timedelta(minutes=1))
    result = service(tmp_path, selected_plan=old).probe(
        now=NOW, expected_generation=7,
        stream=stream(authenticated=False, listening=False, reasons=("NO_AUTH",)),
    )
    assert result.state is State.BLOCKED
    assert set(result.reasons) >= {"PLAN_NOT_CURRENT", "STREAM_NOT_READY"}


def test_generation_fence(tmp_path):
    with pytest.raises(StaleGeneration):
        service(tmp_path).probe(now=NOW, expected_generation=6, stream=stream())


@pytest.mark.parametrize(
    "approval_change,reason",
    [
        ({"approval_id": "wrong"}, "APPROVAL_ID_MISMATCH"),
        ({"account_id": "wrong"}, "APPROVAL_ACCOUNT_MISMATCH"),
        ({"symbol": "MSFT"}, "APPROVAL_SCOPE_MISMATCH"),
        ({"maximum_quantity": Decimal("0.5")}, "APPROVAL_QUANTITY_TOO_SMALL"),
        ({"maximum_notional": Decimal("5")}, "APPROVAL_NOTIONAL_TOO_SMALL"),
        ({"expires_at": NOW - timedelta(seconds=1), "issued_at": NOW - timedelta(minutes=2)}, "APPROVAL_NOT_CURRENT"),
    ],
)
def test_approval_scope_blocks(tmp_path, approval_change, reason):
    instance = service(tmp_path)
    instance.probe(now=NOW, expected_generation=7, stream=stream())
    result = instance.arm(approval=approval(**approval_change), now=NOW + timedelta(seconds=1), expected_generation=7)
    assert result.state is State.BLOCKED
    assert reason in result.reasons


def test_invalid_approval_signature_blocks(tmp_path):
    instance = service(tmp_path)
    instance.probe(now=NOW, expected_generation=7, stream=stream())
    foreign = ApprovalKey("y" * 40)
    result = instance.arm(approval=approval(foreign), now=NOW + timedelta(seconds=1), expected_generation=7)
    assert "APPROVAL_SIGNATURE_INVALID" in result.reasons


def test_stale_probe_and_disabled_writes_block(tmp_path):
    gateway = Gateway()
    instance = service(tmp_path, gateway, selected_policy=Policy(maximum_probe_age=timedelta(seconds=1)))
    instance.probe(now=NOW, expected_generation=7, stream=stream())
    gateway.writes_enabled = False
    result = instance.arm(approval=approval(), now=NOW + timedelta(seconds=2), expected_generation=7)
    assert set(result.reasons) >= {"PROBE_EVIDENCE_STALE", "PAPER_WRITES_DISABLED"}


def test_approval_replay_detected(tmp_path):
    instance = service(tmp_path)
    probe_and_arm(instance)
    restarted = QualificationService(
        gateway=instance.gateway,
        plan=instance.plan,
        approval_key=key(),
        event_store=instance.event_store,
        kill_switch=instance.kill_switch,
        policy=instance.policy,
        sleeper=lambda _: None,
    )
    restarted.state = State.PROBE_VERIFIED
    restarted.probe_captured_at = NOW
    with pytest.raises(ApprovalReplay):
        restarted.arm(approval=approval(), now=NOW + timedelta(seconds=1), expected_generation=7)


def test_ambiguous_submit_read_recovery_succeeds(tmp_path):
    gateway = Gateway()
    gateway.submit_error = AmbiguousMutation("timeout")
    gateway.current = order()
    instance = service(tmp_path, gateway)
    probe_and_arm(instance)
    result = instance.execute(now=NOW + timedelta(seconds=2), expected_generation=7)
    assert result.success
    assert gateway.submit_calls == 1


def test_ambiguous_submit_unresolved_enters_recovery(tmp_path):
    gateway = Gateway()
    gateway.submit_error = AmbiguousMutation("timeout")
    instance = service(tmp_path, gateway)
    probe_and_arm(instance)
    result = instance.execute(now=NOW + timedelta(seconds=2), expected_generation=7)
    assert result.state is State.RECOVERING
    assert result.kill_switch_engaged
    recovered = instance.recover_read_only(now=NOW + timedelta(seconds=3), expected_generation=7)
    assert recovered.state is State.BLOCKED


def test_ambiguous_cancel_active_enters_recovery(tmp_path):
    gateway = Gateway()
    gateway.cancel_error = AmbiguousMutation("timeout")
    instance = service(tmp_path, gateway)
    probe_and_arm(instance)
    result = instance.execute(now=NOW + timedelta(seconds=2), expected_generation=7)
    assert result.state is State.RECOVERING
    assert "CANCEL_AMBIGUOUS_ORDER_ACTIVE" in result.reasons


def test_fill_blocks_and_engages_kill_switch(tmp_path):
    gateway = Gateway()
    gateway.submit_limit_order = lambda **_: order(status=OrderStatus.PARTIALLY_FILLED, filled="0.1")
    instance = service(tmp_path, gateway)
    probe_and_arm(instance)
    result = instance.execute(now=NOW + timedelta(seconds=2), expected_generation=7)
    assert result.state is State.BLOCKED
    assert result.kill_switch_engaged
    assert result.filled_quantity > 0


def test_identity_mismatch_quarantines(tmp_path):
    gateway = Gateway()
    gateway.submit_limit_order = lambda **_: replace(order(), symbol="MSFT")
    instance = service(tmp_path, gateway)
    probe_and_arm(instance)
    result = instance.execute(now=NOW + timedelta(seconds=2), expected_generation=7)
    assert result.state is State.QUARANTINED
    assert result.kill_switch_engaged


def test_cleanup_retries_then_verifies(tmp_path):
    gateway = Gateway()
    gateway.cleanup_open_reads = 2
    instance = service(tmp_path, gateway, selected_policy=Policy(cleanup_attempts=3, cleanup_backoff_seconds=0))
    probe_and_arm(instance)
    result = instance.execute(now=NOW + timedelta(seconds=2), expected_generation=7)
    assert result.success


def test_cleanup_failure_blocks(tmp_path):
    gateway = Gateway()
    gateway.cleanup_open_reads = 99
    instance = service(tmp_path, gateway, selected_policy=Policy(cleanup_attempts=2, cleanup_backoff_seconds=0))
    probe_and_arm(instance)
    result = instance.execute(now=NOW + timedelta(seconds=2), expected_generation=7)
    assert result.state is State.BLOCKED
    assert result.kill_switch_engaged
    assert "ORDER_REMAINS_OPEN_OR_NON_TERMINAL" in result.reasons


def test_event_tamper_is_detected(tmp_path):
    instance = service(tmp_path)
    instance.probe(now=NOW, expected_generation=7, stream=stream())
    path = tmp_path / "events.jsonl"
    text = path.read_text().replace("PROBE_VERIFIED", "VERIFIED", 1)
    path.write_text(text)
    assert not instance.event_store.verify()
    with pytest.raises(QualificationCorruption):
        instance.event_store.load()


def test_kill_switch_is_sticky_and_tamper_detected(tmp_path):
    switch = KillSwitchStore(tmp_path / "kill.json")
    first = switch.engage(reason="first", now=NOW, generation=7)
    second = switch.engage(reason="second", now=NOW, generation=8)
    assert first == second
    raw = json.loads((tmp_path / "kill.json").read_text())
    raw["reason"] = "tampered"
    (tmp_path / "kill.json").write_text(json.dumps(raw))
    with pytest.raises(QualificationCorruption):
        switch.status()


def test_plan_and_approval_validation():
    sealed = plan()
    with pytest.raises(QualificationCorruption):
        replace(sealed, quantity=Decimal("2")).validate()
    assert approval().verify(key())
    assert "x" * 20 not in repr(key())
    with pytest.raises(ValueError):
        ApprovalKey.from_environment({})


def test_invalid_lifecycle_calls(tmp_path):
    instance = service(tmp_path)
    with pytest.raises(QualificationError):
        instance.execute(now=NOW, expected_generation=7)
    with pytest.raises(QualificationError):
        instance.recover_read_only(now=NOW, expected_generation=7)
