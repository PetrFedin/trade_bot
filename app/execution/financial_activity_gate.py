from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.execution.alpaca_financial_activities import (
    FinancialActivityReadiness,
    financial_activity_readiness,
)
from app.execution.financial_activity_store import FinancialActivityStore


class FinancialActivityTruthProvider(Protocol):
    """Read-only authoritative view used to admit new risk."""

    def readiness(self, *, now: datetime) -> FinancialActivityReadiness: ...


@dataclass(frozen=True)
class BoundFinancialActivityTruthProvider:
    """Bind durable financial-activity truth to one account and release.

    ``maximum_age`` is an operational admission freshness budget. It is
    intentionally separate from the wider historical recovery window used by
    the recovery service.
    """

    store: FinancialActivityStore
    account_identity: str
    release_identity: str
    maximum_age: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if not self.account_identity.strip():
            raise ValueError("account_identity is required")
        if not self.release_identity.strip():
            raise ValueError("release_identity is required")
        if self.maximum_age <= timedelta(0):
            raise ValueError("maximum_age must be positive")

    def readiness(self, *, now: datetime) -> FinancialActivityReadiness:
        return financial_activity_readiness(
            self.store,
            account_identity=self.account_identity,
            release_identity=self.release_identity,
            now=now,
            maximum_age=self.maximum_age,
        )


def financial_activity_truth_block_reasons(
    provider: FinancialActivityTruthProvider,
    *,
    now: datetime,
) -> tuple[str, ...]:
    """Return deterministic fail-closed reasons for new-risk admission.

    Provider/storage failures are deliberately collapsed to one public reason;
    callers must never convert an unavailable financial truth source into an
    optimistic admission decision.
    """

    try:
        readiness = provider.readiness(now=now)
    except Exception:
        return ("FINANCIAL_ACTIVITY_TRUTH_INVALID",)
    if readiness.ready:
        return ()
    return tuple(
        sorted(
            {
                "BROKER_FINANCIAL_ACTIVITY_NOT_READY",
                *readiness.reasons,
            }
        )
    )
