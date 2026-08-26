from __future__ import annotations

from typing import Protocol

from app.execution.bybit_demo_excursion_runtime import acknowledge_bybit_demo_excursion_final
from app.execution.bybit_demo_managed_trade_poll import BybitDemoManagedTradePollResult
from app.execution.bybit_demo_terminal_handoff import (
    BybitDemoSessionRiskCommitter,
    BybitDemoTerminalEvidenceStore,
    BybitDemoTerminalHandoffResult,
    persist_and_acknowledge_bybit_demo_terminal_evidence,
)


class _ExcursionStore(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool

    def clear(self, *, expected_revision: str) -> None: ...


def persist_product_terminal_state(
    poll: BybitDemoManagedTradePollResult,
    *,
    evidence_store: BybitDemoTerminalEvidenceStore,
    excursion_store: _ExcursionStore,
    session_risk_committer: BybitDemoSessionRiskCommitter,
) -> BybitDemoTerminalHandoffResult:
    """Product facade over the one canonical v122 terminal commit sequence.

    Product code previously maintained a second session-risk write path backed by the legacy
    ``astra_bybit_session_risk_ledger`` table. That competing authority is intentionally removed
    here: product terminal completion now delegates to the same evidence -> v122 risk -> exact ACK
    implementation used by the canonical Demo trading runtime.
    """

    return persist_and_acknowledge_bybit_demo_terminal_evidence(
        poll,
        evidence_store=evidence_store,
        session_risk_committer=session_risk_committer,
        excursion_store=excursion_store,
        acknowledge=acknowledge_bybit_demo_excursion_final,
    )


__all__ = ["persist_product_terminal_state"]
