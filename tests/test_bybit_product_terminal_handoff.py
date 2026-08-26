from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.execution.bybit_product_terminal_handoff as product_handoff
from app.execution.bybit_demo_excursion_runtime import BybitDemoExcursionRuntimeStatus
from app.execution.bybit_demo_managed_trade_poll import BybitDemoManagedTradePollPhase
from app.execution.bybit_demo_session_risk_runtime import BybitDemoSessionRiskCommitReceipt
from app.execution.bybit_demo_terminal_handoff import BybitDemoTerminalHandoffStatus

_ENTRY = "ASTRA-DEMO-PRODUCT-HANDOFF"


class _EvidenceStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def persist(self, **_kwargs):
        self.events.append("evidence")
        return SimpleNamespace(live_mainnet_order_routing_allowed=False)


class _ExcursionStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def clear(self, *, expected_revision: str) -> None:
        assert expected_revision == "a" * 64
        self.events.append("clear")


class _RiskCommitter:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    automatic_reset_allowed = False
    initialized_session_required = True

    def __init__(
        self,
        events: list[str],
        *,
        fail: bool = False,
        idempotent: bool = False,
    ) -> None:
        self.events = events
        self.fail = fail
        self.idempotent = idempotent

    def commit(self, _accounting: object) -> BybitDemoSessionRiskCommitReceipt:
        self.events.append("risk")
        if self.fail:
            raise RuntimeError("database unavailable")
        return BybitDemoSessionRiskCommitReceipt(
            ledger_revision_sha256="c" * 64,
            outcome_count=1,
            entry_order_link_id=_ENTRY,
            idempotent_existing_outcome=self.idempotent,
        )


def _poll():
    checkpoint = SimpleNamespace(
        entry_order_link_id=_ENTRY,
        revision="a" * 64,
    )
    return SimpleNamespace(
        live_mainnet_order_routing_allowed=False,
        phase=BybitDemoManagedTradePollPhase.TERMINAL_EVIDENCE_READY,
        fully_reconciled_all_in=True,
        profit_evidence=SimpleNamespace(fully_reconciled_all_in=True),
        accounting=SimpleNamespace(
            lifecycle=SimpleNamespace(next_entry_allowed=True),
        ),
        excursion=SimpleNamespace(checkpoint=checkpoint),
        terminal_evidence_ack_required=True,
    )


def _ack(*, store, expected_revision):
    assert expected_revision == "a" * 64
    store.clear(expected_revision=expected_revision)
    return SimpleNamespace(
        status=BybitDemoExcursionRuntimeStatus.FINAL_ACKNOWLEDGED,
        reasons=(),
        live_mainnet_order_routing_allowed=False,
    )


def test_product_handoff_orders_evidence_then_v122_risk_then_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(product_handoff, "acknowledge_bybit_demo_excursion_final", _ack)

    result = product_handoff.persist_product_terminal_state(
        _poll(),
        evidence_store=_EvidenceStore(events),
        excursion_store=_ExcursionStore(events),
        session_risk_committer=_RiskCommitter(events),
    )

    assert result.status is BybitDemoTerminalHandoffStatus.COMPLETE
    assert result.evidence_durable is True
    assert result.session_risk_durable is True
    assert result.session_risk_receipt is not None
    assert result.next_entry_allowed is True
    assert result.checkpoint_cleared is True
    assert events == ["evidence", "risk", "clear"]


def test_session_risk_failure_keeps_checkpoint_active_for_exact_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(product_handoff, "acknowledge_bybit_demo_excursion_final", _ack)

    result = product_handoff.persist_product_terminal_state(
        _poll(),
        evidence_store=_EvidenceStore(events),
        excursion_store=_ExcursionStore(events),
        session_risk_committer=_RiskCommitter(events, fail=True),
    )

    assert result.status is BybitDemoTerminalHandoffStatus.SESSION_RISK_PERSIST_FAILED
    assert result.reasons == ("SESSION_RISK_PERSIST_FAILED:RuntimeError",)
    assert result.evidence_durable is True
    assert result.session_risk_durable is False
    assert result.checkpoint_cleared is False
    assert result.next_entry_allowed is False
    assert events == ["evidence", "risk"]


def test_idempotent_session_outcome_can_finish_exact_checkpoint_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(product_handoff, "acknowledge_bybit_demo_excursion_final", _ack)

    result = product_handoff.persist_product_terminal_state(
        _poll(),
        evidence_store=_EvidenceStore(events),
        excursion_store=_ExcursionStore(events),
        session_risk_committer=_RiskCommitter(events, idempotent=True),
    )

    assert result.status is BybitDemoTerminalHandoffStatus.COMPLETE
    assert result.session_risk_receipt is not None
    assert result.session_risk_receipt.idempotent_existing_outcome is True
    assert result.session_risk_durable is True
    assert events == ["evidence", "risk", "clear"]
