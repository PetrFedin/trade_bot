from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.execution.bybit_product_terminal_handoff as product_handoff
from app.execution.bybit_demo_excursion_runtime import BybitDemoExcursionRuntimeStatus
from app.execution.bybit_demo_managed_trade_poll import BybitDemoManagedTradePollPhase
from app.execution.bybit_demo_terminal_handoff import BybitDemoTerminalHandoffStatus


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


class _SessionStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, events: list[str], *, fail_save: bool = False) -> None:
        self.events = events
        self.fail_save = fail_save
        self.ledger = object()

    def load_current(self):
        self.events.append("session-load")
        return SimpleNamespace(ledger=self.ledger, revision="b" * 64)

    def save(self, ledger, *, expected_revision: str):
        assert ledger is not self.ledger
        assert expected_revision == "b" * 64
        self.events.append("session-save")
        if self.fail_save:
            raise RuntimeError("database unavailable")
        return SimpleNamespace(ledger=ledger, revision="c" * 64)


def _poll():
    checkpoint = SimpleNamespace(
        entry_order_link_id="ASTRA-DEMO-PRODUCT-HANDOFF",
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


def test_product_handoff_orders_evidence_then_session_risk_then_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    new_ledger = object()

    def _apply(ledger, accounting):
        del ledger, accounting
        events.append("session-apply")
        return new_ledger

    def _ack(*, store, expected_revision):
        assert expected_revision == "a" * 64
        store.clear(expected_revision=expected_revision)
        return SimpleNamespace(
            status=BybitDemoExcursionRuntimeStatus.FINAL_ACKNOWLEDGED,
            reasons=(),
            live_mainnet_order_routing_allowed=False,
        )

    monkeypatch.setattr(
        product_handoff,
        "apply_fully_reconciled_trade_to_session_ledger",
        _apply,
    )
    monkeypatch.setattr(
        product_handoff,
        "acknowledge_bybit_demo_excursion_final",
        _ack,
    )

    result = product_handoff.persist_product_terminal_state(
        _poll(),
        evidence_store=_EvidenceStore(events),
        excursion_store=_ExcursionStore(events),
        session_risk_store=_SessionStore(events),
    )

    assert result.status is BybitDemoTerminalHandoffStatus.COMPLETE
    assert result.next_entry_allowed is True
    assert result.checkpoint_cleared is True
    assert events == [
        "evidence",
        "session-load",
        "session-apply",
        "session-save",
        "clear",
    ]


def test_session_risk_failure_keeps_checkpoint_active_for_exact_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    monkeypatch.setattr(
        product_handoff,
        "apply_fully_reconciled_trade_to_session_ledger",
        lambda _ledger, _accounting: object(),
    )

    result = product_handoff.persist_product_terminal_state(
        _poll(),
        evidence_store=_EvidenceStore(events),
        excursion_store=_ExcursionStore(events),
        session_risk_store=_SessionStore(events, fail_save=True),
    )

    assert result.status is BybitDemoTerminalHandoffStatus.EVIDENCE_PERSISTED_ACK_FAILED
    assert result.reasons == ("SESSION_RISK_PERSIST_FAILED:RuntimeError",)
    assert result.evidence_durable is True
    assert result.checkpoint_cleared is False
    assert result.next_entry_allowed is False
    assert "clear" not in events


def test_already_applied_session_outcome_skips_rewrite_but_can_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    session = _SessionStore(events)

    monkeypatch.setattr(
        product_handoff,
        "apply_fully_reconciled_trade_to_session_ledger",
        lambda ledger, _accounting: ledger,
    )

    def _ack(*, store, expected_revision):
        store.clear(expected_revision=expected_revision)
        return SimpleNamespace(
            status=BybitDemoExcursionRuntimeStatus.FINAL_ACKNOWLEDGED,
            reasons=(),
            live_mainnet_order_routing_allowed=False,
        )

    monkeypatch.setattr(
        product_handoff,
        "acknowledge_bybit_demo_excursion_final",
        _ack,
    )

    result = product_handoff.persist_product_terminal_state(
        _poll(),
        evidence_store=_EvidenceStore(events),
        excursion_store=_ExcursionStore(events),
        session_risk_store=session,
    )

    assert result.status is BybitDemoTerminalHandoffStatus.COMPLETE
    assert "session-save" not in events
    assert events[-1] == "clear"
