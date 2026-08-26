from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.execution.bybit_demo_post_trade_accounting import (
    BybitDemoPostTradeAccountingResult,
)
from app.execution.bybit_demo_postgres_session_risk_store import (
    PostgresBybitDemoSessionRiskLedgerStore,
)
from app.execution.bybit_demo_session_risk_ledger import (
    apply_fully_reconciled_trade_to_session_ledger,
    observe_bybit_demo_session_equity,
)
from app.strategy.crypto_session_risk import CryptoSessionRiskState


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


@dataclass(frozen=True)
class BybitDemoSessionRiskObservation:
    ledger_revision_sha256: str
    outcome_count: int
    session_state: CryptoSessionRiskState
    high_water_advanced: bool
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
        _validate_store(store, role="committer")
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


class PostgresBybitDemoSessionRiskObserver:
    """Persist wallet-equity high-water and reconstruct authoritative session state.

    A persistent supervisor must not keep the session peak only in process memory. Each cycle reads
    the initialized v122 ledger, advances its high-water from the real Demo wallet when necessary,
    persists that change with bounded CAS recovery, and returns the risk state from the durable
    ledger plus the current wallet equity. It never initializes or resets a session.
    """

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    automatic_reset_allowed = False
    initialized_session_required = True

    def __init__(self, store: PostgresBybitDemoSessionRiskLedgerStore) -> None:
        _validate_store(store, role="observer")
        self._store = store

    def observe(
        self,
        *,
        current_equity_usdt: Decimal,
    ) -> BybitDemoSessionRiskObservation:
        if not current_equity_usdt.is_finite() or current_equity_usdt <= 0:
            raise ValueError("Demo session-risk observation equity must be positive and finite")

        current = self._store.load_active()
        previous_peak = current.ledger.effective_peak_equity_usdt
        proposed = observe_bybit_demo_session_equity(
            current.ledger,
            current_equity_usdt=current_equity_usdt,
        )
        checkpoint = current
        if proposed != current.ledger:
            checkpoint = self._save_with_one_cas_recovery(
                proposed,
                expected_revision=current.revision,
                current_equity_usdt=current_equity_usdt,
            )

        state = checkpoint.ledger.to_session_risk_state(
            current_equity_usdt=current_equity_usdt,
        )
        return BybitDemoSessionRiskObservation(
            ledger_revision_sha256=checkpoint.revision,
            outcome_count=len(checkpoint.ledger.outcomes),
            session_state=state,
            high_water_advanced=(
                checkpoint.ledger.effective_peak_equity_usdt > previous_peak
            ),
        )

    def _save_with_one_cas_recovery(
        self,
        proposed,
        *,
        expected_revision: str,
        current_equity_usdt: Decimal,
    ):
        try:
            return self._store.save(
                proposed,
                expected_revision=expected_revision,
            )
        except RuntimeError as exc:
            if "revision changed concurrently" not in str(exc):
                raise
            reloaded = self._store.load_active()
            reconciled = observe_bybit_demo_session_equity(
                reloaded.ledger,
                current_equity_usdt=current_equity_usdt,
            )
            if reconciled == reloaded.ledger:
                return reloaded
            try:
                return self._store.save(
                    reconciled,
                    expected_revision=reloaded.revision,
                )
            except RuntimeError as retry_exc:
                if "revision changed concurrently" not in str(retry_exc):
                    raise
                raise RuntimeError(
                    "Demo session-risk high-water changed concurrently twice; "
                    "explicit recovery required"
                ) from retry_exc


def _validate_store(
    store: PostgresBybitDemoSessionRiskLedgerStore,
    *,
    role: str,
) -> None:
    if store.live_mainnet_order_routing_allowed or store.order_writes_supported:
        raise ValueError(f"Demo session-risk {role} rejected unsafe PostgreSQL store")
    if store.automatic_reset_allowed:
        raise ValueError(f"Demo session-risk {role} forbids automatic reset")


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
    "BybitDemoSessionRiskObservation",
    "PostgresBybitDemoSessionRiskCommitter",
    "PostgresBybitDemoSessionRiskObserver",
]
