from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from app.observability.readiness import (
    OperationalReadinessEvaluator,
    OperationalSnapshot,
)
from app.oms.store import OrderRecord
from app.runtime.account_reconciliation_truth import (
    AccountReconciliationBlocked,
    AccountReconciliationGate,
)
from app.runtime.paper_dispatch_control import (
    DispatchAuthorization,
    DispatchBlocked,
    PaperDispatchControlStore,
)

OperationalSnapshotProvider = Callable[[], OperationalSnapshot]


class PaperFinalDispatchGuard:
    """Last-mile fail-closed guard immediately before submit ownership is claimed.

    Operational readiness and durable broker-account reconciliation are re-evaluated
    for every risk-increasing outbound submit attempt. A missing, stale or mismatched
    account truth durably HALTs new entries before the caller can acquire submit
    capability. Risk-reducing SELL operations are not blocked by account-truth drift.
    """

    def __init__(
        self,
        *,
        control: PaperDispatchControlStore,
        readiness: OperationalReadinessEvaluator,
        snapshot_provider: OperationalSnapshotProvider | None,
        account_reconciliation_gate: AccountReconciliationGate | None = None,
    ) -> None:
        self.control = control
        self.readiness = readiness
        self.snapshot_provider = snapshot_provider
        self.account_reconciliation_gate = account_reconciliation_gate

    @staticmethod
    def _time(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        return value.astimezone(UTC)

    def authorize(
        self,
        record: OrderRecord,
        *,
        occurred_at: datetime,
    ) -> DispatchAuthorization:
        moment = self._time(occurred_at)
        if self.account_reconciliation_gate is not None:
            try:
                self.account_reconciliation_gate.authorize_order(record, moment)
            except AccountReconciliationBlocked as exc:
                self._halt(exc.reasons, occurred_at=moment)
                raise DispatchBlocked(exc.reasons) from exc

        if self.snapshot_provider is None:
            reasons = ("OPERATIONAL_SNAPSHOT_REQUIRED",)
            self._halt(reasons, occurred_at=moment)
            raise DispatchBlocked(reasons)

        try:
            snapshot = self.snapshot_provider()
            result = self.readiness.evaluate(snapshot)
        except Exception as exc:
            reasons = ("OPERATIONAL_SNAPSHOT_INVALID",)
            self._halt(reasons, occurred_at=moment)
            raise DispatchBlocked(reasons) from exc

        if not result.ready_for_paper_operation:
            reasons = result.reasons or ("OPERATIONAL_READINESS_FAILED",)
            self._halt(reasons, occurred_at=moment)
            raise DispatchBlocked(reasons)

        return self.control.authorize(
            intent_id=record.intent_id,
            occurred_at=moment,
        )

    def _halt(self, reasons: tuple[str, ...], *, occurred_at: datetime) -> None:
        self.control.halt(
            operator_id="system:final-dispatch",
            reason="OPERATIONAL_READINESS_FAILED:" + ",".join(sorted(set(reasons))),
            occurred_at=occurred_at,
        )
