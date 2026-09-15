from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from app.execution.alpaca_financial_activities import (
    AlpacaPaperFinancialActivityReader,
    FinancialActivityPage,
    FinancialActivityProjector,
    FinancialActivityRecoveryPolicy,
    FinancialActivityRecoveryService,
    financial_activity_readiness,
)
from app.execution.financial_activity_store import (
    BrokerFinancialActivity,
    FinancialProjectionState,
    SQLiteFinancialActivityStore,
)
from app.portfolio.ledger import PortfolioLedger
from app.portfolio.strict import StrictPortfolioEventStore
from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperCredentialsV100,
    HttpResponseV100,
)

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
ACCOUNT = "paper-account:fingerprint-1"
RELEASE = "release:f21b"
OTHER_ACCOUNT = "paper-account:fingerprint-2"
OTHER_RELEASE = "release:other"


class QueueTransport:
    def __init__(self, responses: list[HttpResponseV100]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method, url, *, headers, body, timeout_seconds):
        self.calls.append({"method": method, "url": url, "body": body})
        return self.responses.pop(0)


class ReplaySource:
    def __init__(self, activities: tuple[BrokerFinancialActivity, ...]) -> None:
        self.activities = activities
        self.calls: list[tuple[datetime, datetime, str | None]] = []

    def page(self, *, after, until, page_size, page_token):
        self.calls.append((after, until, page_token))
        selected = tuple(
            activity
            for activity in self.activities
            if after < activity.occurred_at <= until
        )
        return FinancialActivityPage(selected, None)


def payload(
    activity_id: str,
    activity_type: str,
    net_amount: str,
    *,
    symbol: str | None = None,
    day: str = "2026-09-15",
) -> dict[str, str]:
    result = {
        "id": activity_id,
        "activity_type": activity_type,
        "date": day,
        "net_amount": net_amount,
    }
    if symbol is not None:
        result["symbol"] = symbol
    return result


def activity(
    activity_id: str,
    activity_type: str,
    net_amount: str,
    *,
    occurred_at: datetime = NOW - timedelta(minutes=1),
    symbol: str | None = None,
    account_identity: str = ACCOUNT,
    release_identity: str = RELEASE,
) -> BrokerFinancialActivity:
    raw = payload(
        activity_id,
        activity_type,
        net_amount,
        symbol=symbol,
        day=occurred_at.date().isoformat(),
    )
    return BrokerFinancialActivity(
        activity_id=activity_id,
        activity_type=activity_type,
        net_amount=Decimal(net_amount),
        currency="USD",
        symbol=symbol,
        occurred_at=occurred_at,
        account_identity=account_identity,
        release_identity=release_identity,
        source_cursor="ROOT",
        canonical_payload=json.dumps(raw, sort_keys=True, separators=(",", ":")),
    )


def stack(tmp_path, *, opening_cash: str = "1000"):
    fact_store = SQLiteFinancialActivityStore(tmp_path / "financial.sqlite")
    portfolio = StrictPortfolioEventStore(tmp_path / "portfolio.sqlite")
    ledger = portfolio.replay(opening_cash=Decimal(opening_cash))
    projector = FinancialActivityProjector(
        store=fact_store,
        portfolio=portfolio,
        runtime_ledger=ledger,
        account_identity=ACCOUNT,
        release_identity=RELEASE,
    )
    return fact_store, portfolio, ledger, projector


def test_reader_uses_non_trade_activity_get_and_preserves_raw_payload() -> None:
    body = json.dumps([payload("fee-1", "FEE", "-2.50")]).encode()
    transport = QueueTransport([HttpResponseV100(200, {}, body)])
    reader = AlpacaPaperFinancialActivityReader(
        credentials=AlpacaPaperCredentialsV100(key_id="key", secret_key="secret"),
        transport=transport,
        account_identity=ACCOUNT,
        release_identity=RELEASE,
    )
    page = reader.page(
        after=NOW - timedelta(days=1),
        until=NOW,
        page_size=10,
        page_token=None,
    )
    assert len(page.activities) == 1
    recovered = page.activities[0]
    assert recovered.activity_type == "FEE"
    assert recovered.net_amount == Decimal("-2.50")
    assert recovered.occurred_at == datetime(2026, 9, 15, tzinfo=UTC)
    call = transport.calls[0]
    assert call["method"] == "GET" and call["body"] is None
    parsed = urlparse(str(call["url"]))
    assert parsed.path == "/v2/account/activities"
    query = parse_qs(parsed.query)
    assert query["category"] == ["non_trade_activity"]
    assert query["direction"] == ["asc"]


def test_fee_withdrawal_deposit_and_dividend_project_exact_cash_and_restart(tmp_path) -> None:
    store, portfolio, ledger, projector = stack(tmp_path)
    facts = (
        activity("deposit-1", "CSD", "100"),
        activity("fee-1", "FEE", "-5"),
        activity("dividend-1", "DIV", "20", symbol="AAPL"),
        activity("withdrawal-1", "CSW", "-50"),
    )
    for fact in facts:
        record = store.ingest(fact, ingested_at=NOW)
        assert record.state is FinancialProjectionState.PENDING

    projected, quarantined = projector.project_pending(occurred_at=NOW)
    assert projected == 4 and quarantined == 0
    assert ledger.cash == Decimal("1065")
    assert ledger.external_cash_flow == Decimal("50")
    assert ledger.fees_paid == Decimal("5")
    assert ledger.cash_income == Decimal("20")
    snapshot = ledger.snapshot({})
    assert snapshot.total_pnl == Decimal("15")

    restarted = portfolio.replay(opening_cash=Decimal("1000"))
    assert restarted.cash == Decimal("1065")
    assert restarted.external_cash_flow == Decimal("50")
    assert restarted.fees_paid == Decimal("5")
    assert restarted.cash_income == Decimal("20")
    assert restarted.snapshot({}).total_pnl == Decimal("15")


def test_projection_never_crosses_account_or_release_scope(tmp_path) -> None:
    store, portfolio, ledger, projector = stack(tmp_path)
    store.ingest(activity("mine", "CSD", "10"), ingested_at=NOW)
    store.ingest(
        activity(
            "other-account",
            "CSD",
            "900",
            account_identity=OTHER_ACCOUNT,
        ),
        ingested_at=NOW,
    )
    store.ingest(
        activity(
            "other-release",
            "CSD",
            "800",
            release_identity=OTHER_RELEASE,
        ),
        ingested_at=NOW,
    )

    projected, quarantined = projector.project_pending(occurred_at=NOW)
    assert projected == 1 and quarantined == 0
    assert ledger.cash == Decimal("1010")
    assert portfolio.replay(opening_cash=Decimal("1000")).cash == Decimal("1010")
    assert store.pending_count(
        account_identity=ACCOUNT,
        release_identity=RELEASE,
    ) == 0
    assert store.pending_count(
        account_identity=OTHER_ACCOUNT,
        release_identity=RELEASE,
    ) == 1
    assert store.pending_count(
        account_identity=ACCOUNT,
        release_identity=OTHER_RELEASE,
    ) == 1


def test_unknown_activity_quarantines_and_same_id_changed_payload_conflicts(tmp_path) -> None:
    store, _, ledger, projector = stack(tmp_path)
    unknown = activity("journal-1", "JNLC", "25")
    store.ingest(unknown, ingested_at=NOW)
    projected, quarantined = projector.project_pending(occurred_at=NOW)
    assert projected == 0 and quarantined == 1
    assert store.quarantined_count(
        account_identity=ACCOUNT,
        release_identity=RELEASE,
    ) == 1
    assert ledger.cash == Decimal("1000")

    changed = activity("journal-1", "JNLC", "30")
    record = store.ingest(changed, ingested_at=NOW + timedelta(seconds=1))
    assert record.state is FinancialProjectionState.QUARANTINED
    assert record.reason == "ACTIVITY_ID_CONFLICT"
    assert ledger.cash == Decimal("1000")


def test_crash_after_fact_append_before_projection_resumes_exactly_once(tmp_path) -> None:
    store, portfolio, ledger, _ = stack(tmp_path)
    fact = activity("fee-crash", "FEE", "-7")
    store.ingest(fact, ingested_at=NOW)
    assert store.pending_count(
        account_identity=ACCOUNT,
        release_identity=RELEASE,
    ) == 1
    assert ledger.cash == Decimal("1000")

    restarted_ledger = portfolio.replay(opening_cash=Decimal("1000"))
    restarted_projector = FinancialActivityProjector(
        store=SQLiteFinancialActivityStore(tmp_path / "financial.sqlite"),
        portfolio=StrictPortfolioEventStore(tmp_path / "portfolio.sqlite"),
        runtime_ledger=restarted_ledger,
        account_identity=ACCOUNT,
        release_identity=RELEASE,
    )
    projected, quarantined = restarted_projector.project_pending(
        occurred_at=NOW + timedelta(seconds=1)
    )
    assert projected == 1 and quarantined == 0
    assert restarted_ledger.cash == Decimal("993")

    again, _ = restarted_projector.project_pending(occurred_at=NOW + timedelta(seconds=2))
    assert again == 0
    replayed = portfolio.replay(opening_cash=Decimal("1000"))
    assert replayed.cash == Decimal("993")


def test_recovery_watermark_overlap_replays_without_duplicate_economics(tmp_path) -> None:
    store, portfolio, ledger, projector = stack(tmp_path)
    first = activity(
        "deposit-overlap",
        "CSD",
        "25",
        occurred_at=NOW - timedelta(hours=2),
    )
    source = ReplaySource((first,))
    service = FinancialActivityRecoveryService(
        source=source,
        store=store,
        projector=projector,
        account_identity=ACCOUNT,
        release_identity=RELEASE,
        bootstrap_after=NOW - timedelta(days=2),
        policy=FinancialActivityRecoveryPolicy(
            overlap=timedelta(days=1),
            maximum_window=timedelta(days=7),
        ),
    )

    result = service.recover(until=NOW - timedelta(hours=1), observed_at=NOW)
    assert result.ready
    assert ledger.cash == Decimal("1025")
    state = store.recovery_state(account_identity=ACCOUNT, release_identity=RELEASE)
    assert state is not None and state.recovered_through == NOW - timedelta(hours=1)

    second = service.recover(until=NOW, observed_at=NOW)
    assert second.ready
    assert second.duplicates >= 1
    assert ledger.cash == Decimal("1025")
    assert portfolio.replay(opening_cash=Decimal("1000")).cash == Decimal("1025")
    assert source.calls[1][0] == NOW - timedelta(days=1, hours=1)

    readiness = financial_activity_readiness(
        store,
        account_identity=ACCOUNT,
        release_identity=RELEASE,
        now=NOW,
        maximum_age=timedelta(minutes=5),
    )
    assert readiness.ready
