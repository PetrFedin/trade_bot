from __future__ import annotations

from dataclasses import dataclass

from app.execution.bybit_demo_post_trade_accounting import (
    BybitDemoPostTradeAccountingResult,
)
from app.execution.bybit_demo_postgres_session_risk_store import (
    PostgresBybitDemoSessionRiskLedgerStore,
)
from app.execution.bybit_demo_session_risk_ledger import (
    apply_fully_reconciled_trade_to_session_ledger,
)


@dataclass(frozen=True)
class BybitDemoSessionRiskCommitReceipt:
    ledger_revision_sha256: str
    outcome_count: int
    entry_order_link_id: str
    idempotent_existing_outcome: bool
    durable_session_required: bool = True
    automatic_reset_allowed: bool = False
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False


class PostgresBybitDemoSessionRiskCommitter:
    """Commit one fully reconciled terminal trade into the initialized v122 session.

    The committer never initializes or resets a session. It performs one optimistic write and, if
    the revision changed concurrently, one bounded reload. A concurrent writer is accepted only
    when it already committed the exact same terminal economics; otherwise the handoff fails closed.
    """

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    automatic_reset_allowed = False
    initialized_session_required = True

    def __init__(self, store: PostgresBybitDemoSessionRiskLedgerStore) -> None:
        if store.live_mainnet_order_routing_allowed or store.order_writes_supported:
            raise ValueError("Demo session-risk committer rejected unsafe PostgreSQL store")
        if store.automatic_reset_allowed:
            raise ValueError("Demo session-risk committer forbids automatic reset")
        self._store = store

    def commit(
        self,
        accounting: BybitDemoPostTradeAccountingResult,
    ) -> BybitDemoSessionRiskCommitReceipt:
        if accounting.live_mainnet_order_routing_allowed:
            raise ValueError("Demo session-risk committer rejected mainnet-capable accounting")

        current = self._store.load_active()
        proposed = apply_fully_reconciled_trade_to_session_ledger(current.ledger, accounting)
        entry_order_link_id = accounting.trade.entry_order_link_id
        if proposed == current.ledger:
            return _receipt(
                current.revision,
                outcome_count=len(current.ledger.outcomes),
                entry_order_link_id=entry_order_link_id,
                idempotent=True,
            )

        try:
            persisted = self._store.save(
                proposed,
                expected_revision=current.revision,
            )
        except RuntimeError as exc:
            if "revision changed concurrently" not in str(exc):
                raise
            reloaded = self._store.load_active()
            reconciled = apply_fully_reconciled_trade_to_session_ledger(
                reloaded.ledger,
                accounting,
            )
            if reconciled != reloaded.ledger:
                raise RuntimeError(
                    "Demo session-risk concurrent revision requires explicit recovery"
                ) from exc
            return _receipt(
                reloaded.revision,
                outcome_count=len(reloaded.ledger.outcomes),
                entry_order_link_id=entry_order_link_id,
                idempotent=True,
            )

        return _receipt(
            persisted.revision,
            outcome_count=len(persisted.ledger.outcomes),
            entry_order_link_id=entry_order_link_id,
            idempotent=False,
        )


def _receipt(
    revision: str,
    *,
    outcome_count: int,
    entry_order_link_id: str,
    idempotent: bool,
) -> BybitDemoSessionRiskCommitReceipt:
    return BybitDemoSessionRiskCommitReceipt(
        ledger_revision_sha256=revision,
        outcome_count=outcome_count,
        entry_order_link_id=entry_order_link_id,
        idempotent_existing_outcome=idempotent,
    )


__all__ = [
    "BybitDemoSessionRiskCommitReceipt",
    "PostgresBybitDemoSessionRiskCommitter",
]
