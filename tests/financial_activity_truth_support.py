from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from app.execution.financial_activity_gate import BoundFinancialActivityTruthProvider
from app.execution.financial_activity_store import SQLiteFinancialActivityStore

TEST_ACCOUNT_IDENTITY = "paper-account:test-f21c"
TEST_RELEASE_IDENTITY = "release:test-f21c"


def ready_financial_activity_truth(
    state_directory: str | Path,
    *,
    now: datetime,
    maximum_age: timedelta = timedelta(minutes=5),
    account_identity: str = TEST_ACCOUNT_IDENTITY,
    release_identity: str = TEST_RELEASE_IDENTITY,
) -> BoundFinancialActivityTruthProvider:
    directory = Path(state_directory)
    directory.mkdir(parents=True, exist_ok=True)
    store = SQLiteFinancialActivityStore(directory / "financial_activity_truth.sqlite")
    store.advance_recovery(
        account_identity=account_identity,
        release_identity=release_identity,
        recovered_through=now,
        occurred_at=now,
    )
    return BoundFinancialActivityTruthProvider(
        store=store,
        account_identity=account_identity,
        release_identity=release_identity,
        maximum_age=maximum_age,
    )
