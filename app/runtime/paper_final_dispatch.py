from __future__ import annotations

from datetime import UTC, datetime

from app.domain.trading import Side
from app.execution.financial_activity_gate import (
    FinancialActivityTruthProvider,
    financial_activity_truth_block_reasons,
)
from app.observability.authority import AuthoritativeOperationalSnapshotAssembler
from app.observability.readiness import OperationalReadinessEvaluator
from app.oms.portfolio_reconciliation import PortfolioReconciliationStore
from app.oms.store import OrderRecord
from app.runtime.paper_dispatch_control import (
    DispatchAuthorization,
    DispatchBlocked,
    PaperDispatchControlStore,
)

class PaperFinalDispatchGuard:
    """Last-mile fail-closed guard immediately before submit ownership is claimed.

    Operational readiness is re-evaluated for every outbound submit attempt.
    Durable portfolio reconciliation and broker financial-activity truth are
    re-read for BUY/new-risk dispatch without disabling risk-reducing exits.
    A missing or degraded operational snapshot durably HALTs new entries before
    the caller can acquire submit capability. Financial-truth failures block the
    individual BUY without globally HALTING exit authority.
    """

    def __init__(
        self,
        *,
        control: PaperDispatchControlStore,
        readiness: OperationalReadinessEvaluator,
        snapshot_authority: AuthoritativeOperationalSnapshotAssembler | None,
        financial_activity_truth: FinancialActivityTruthProvider,
        portfolio_reconciliation: PortfolioReconciliationStore | None = None,
    ) -> None:
        if financial_activity_truth is None:
            raise ValueError("financial_activity_truth is required")
        self.control = control
        self.readiness = readiness
        self.snapshot_authority = snapshot_authority
        self.financial_activity_truth = financial_activity_truth
        self.portfolio_reconciliation = portfolio_reconciliation

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
        self._known_reconciliation_gate(record)
        self._financial_activity_gate(record, occurred_at=moment)
        if self.snapshot_authority is None:
            reasons = ("OPERATIONAL_SNAPSHOT_REQUIRED",)
            self._halt(reasons, occurred_at=moment)
            raise DispatchBlocked(reasons)

        try:
            snapshot = self.snapshot_authority.assemble(
                now=moment,
                new_risk=record.side is Side.BUY,
            )
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

    def _known_reconciliation_gate(self, record: OrderRecord) -> None:
        if record.side is not Side.BUY or self.portfolio_reconciliation is None:
            return
        latest = self.portfolio_reconciliation.latest()
        if latest is None or latest.matched:
            return
        raise DispatchBlocked(("BROKER_PORTFOLIO_NOT_RECONCILED", *latest.reasons))

    def _financial_activity_gate(
        self,
        record: OrderRecord,
        *,
        occurred_at: datetime,
    ) -> None:
        if record.side is not Side.BUY:
            return
        reasons = financial_activity_truth_block_reasons(
            self.financial_activity_truth,
            now=occurred_at,
        )
        if reasons:
            raise DispatchBlocked(("BROKER_FINANCIAL_ACTIVITY_NOT_READY", *reasons))

    def _halt(self, reasons: tuple[str, ...], *, occurred_at: datetime) -> None:
        self.control.halt(
            operator_id="system:final-dispatch",
            reason="OPERATIONAL_READINESS_FAILED:" + ",".join(sorted(set(reasons))),
            occurred_at=occurred_at,
        )
