from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.runtime.paper_broker_contract_v99 import (
    BrokerAccount,
    BrokerOrder,
    BrokerOrderStatus,
)
from tools.external_paper_mutation_drill import (
    CONFIRMATION_PHRASE,
    MutationDrillError,
    MutationDrillInputs,
    execute_drill,
    inputs_from_environment,
    load_readonly_evidence,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


class FakeBroker:
    paper_order_writes_enabled = True

    def __init__(self) -> None:
        self.account = BrokerAccount(
            account_id="paper-account",
            status="ACTIVE",
            currency="USD",
            buying_power=Decimal("1000"),
        )
        self.orders: dict[str, BrokerOrder] = {}
        self.submit_calls = 0
        self.replace_calls = 0
        self.cancel_calls = 0

    def get_account(self) -> BrokerAccount:
        return self.account

    def list_open_orders(self):
        return [
            order
            for order in self.orders.values()
            if order.status is not BrokerOrderStatus.CANCELLED
        ]

    def submit_limit_order(self, **kwargs) -> BrokerOrder:
        self.submit_calls += 1
        order = BrokerOrder(
            client_order_id=kwargs["client_order_id"],
            broker_order_id="paper-order-1",
            instrument=kwargs["instrument"],
            side=kwargs["side"],
            quantity=kwargs["quantity"],
            limit_price=kwargs["limit_price"],
            status=BrokerOrderStatus.ACKNOWLEDGED,
            filled_quantity=Decimal("0"),
            updated_at=NOW,
        )
        self.orders[order.client_order_id] = order
        return order

    def replace_limit_order(
        self, *, broker_order_id: str, limit_price: Decimal
    ) -> BrokerOrder:
        self.replace_calls += 1
        current = next(
            order
            for order in self.orders.values()
            if order.broker_order_id == broker_order_id
        )
        updated = replace(
            current,
            broker_order_id="paper-order-2",
            limit_price=limit_price,
            status=BrokerOrderStatus.REPLACED,
        )
        self.orders[updated.client_order_id] = updated
        return updated

    def cancel_order(self, *, broker_order_id: str) -> BrokerOrder:
        self.cancel_calls += 1
        current = next(
            order
            for order in self.orders.values()
            if order.broker_order_id == broker_order_id
        )
        updated = replace(current, status=BrokerOrderStatus.CANCELLED)
        self.orders[updated.client_order_id] = updated
        return updated

    def get_order_by_client_order_id(self, client_order_id: str):
        return self.orders.get(client_order_id)


def valid_inputs() -> MutationDrillInputs:
    return MutationDrillInputs(
        confirmation=CONFIRMATION_PHRASE,
        initial_limit_price=Decimal("5.00"),
        replacement_limit_price=Decimal("4.99"),
        github_actor="PetrFedin",
        github_run_id="123456",
        github_run_attempt=1,
        generation=77,
    )


def readonly_report() -> dict[str, object]:
    return {
        "provider": "alpaca",
        "environment": "paper",
        "account_status": "ACTIVE",
        "trading_blocked": False,
        "stream_authenticated": True,
        "stream_listening": True,
        "reasons": [],
        "credential_fingerprint": "redacted-fingerprint",
        "paper_order_writes_enabled": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
    }


def test_manual_inputs_require_exact_confirmation_and_less_marketable_replace() -> None:
    invalid_confirmation = replace(valid_inputs(), confirmation="YES")
    with pytest.raises(MutationDrillError, match="OPERATOR_CONFIRMATION_MISMATCH"):
        invalid_confirmation.validate()

    more_marketable = replace(
        valid_inputs(), replacement_limit_price=Decimal("5.01")
    )
    with pytest.raises(
        MutationDrillError,
        match="REPLACEMENT_MUST_REDUCE_BUY_MARKETABILITY",
    ):
        more_marketable.validate()

    excessive_notional = replace(valid_inputs(), initial_limit_price=Decimal("25.01"))
    with pytest.raises(MutationDrillError, match="INITIAL_NOTIONAL_LIMIT_EXCEEDED"):
        excessive_notional.validate()


def test_environment_gate_requires_manual_main_and_mutation_kill_switch(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("ASTRA_ALPACA_PAPER_MUTATION_ENABLED", "ENABLED")
    with pytest.raises(MutationDrillError, match="MANUAL_WORKFLOW_DISPATCH_REQUIRED"):
        inputs_from_environment(
            confirmation=CONFIRMATION_PHRASE,
            initial_limit_price=Decimal("5.00"),
            replacement_limit_price=Decimal("4.99"),
        )

    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("ASTRA_ALPACA_PAPER_MUTATION_ENABLED", "DISABLED")
    with pytest.raises(MutationDrillError, match="PAPER_MUTATION_KILL_SWITCH_DISABLED"):
        inputs_from_environment(
            confirmation=CONFIRMATION_PHRASE,
            initial_limit_price=Decimal("5.00"),
            replacement_limit_price=Decimal("4.99"),
        )


def test_readonly_evidence_must_be_active_authenticated_and_clean(tmp_path: Path) -> None:
    path = tmp_path / "readonly.json"
    path.write_text(
        '{"provider":"alpaca","environment":"paper","account_status":"ACTIVE",'
        '"trading_blocked":false,"stream_authenticated":true,'
        '"stream_listening":true,"reasons":[],"paper_order_writes_enabled":false,'
        '"external_order_routing_allowed":false,"live_trading_allowed":false}',
        encoding="utf-8",
    )
    assert load_readonly_evidence(path)["account_status"] == "ACTIVE"

    path.write_text(
        '{"provider":"alpaca","environment":"paper","account_status":"ACTIVE",'
        '"trading_blocked":false,"stream_authenticated":false,'
        '"stream_listening":true,"reasons":[],"paper_order_writes_enabled":false,'
        '"external_order_routing_allowed":false,"live_trading_allowed":false}',
        encoding="utf-8",
    )
    with pytest.raises(MutationDrillError, match="READONLY_STREAM_NOT_AUTHENTICATED"):
        load_readonly_evidence(path)


def test_clean_drill_is_bounded_submit_replace_cancel_with_no_residual(tmp_path: Path) -> None:
    broker = FakeBroker()
    report = execute_drill(
        broker=broker,
        inputs=valid_inputs(),
        readonly_evidence=readonly_report(),
        output_directory=tmp_path,
        now=NOW,
    )
    assert report["qualification"] == "PASS"
    assert report["outcome"] == "CANCELLED_CLEAN"
    assert report["paper_broker_mutation_verified"] is True
    assert report["filled_quantity"] == "0"
    assert report["residual_paper_exposure"] is False
    assert report["paper_order_writes_enabled_for_drill"] is True
    assert report["probe_executed"] is True
    assert report["mutation_executed"] is True
    assert report["journal_states"] == [
        "PREFLIGHT",
        "SUBMITTED",
        "REPLACED",
        "CANCELLED",
        "COMPLETED",
    ]
    assert report["journal_event_count"] == 5
    assert report["external_order_routing_allowed"] is False
    assert report["live_trading_allowed"] is False
    assert (broker.submit_calls, broker.replace_calls, broker.cancel_calls) == (1, 1, 1)
    assert (tmp_path / "roundtrip-journal.jsonl").exists()
