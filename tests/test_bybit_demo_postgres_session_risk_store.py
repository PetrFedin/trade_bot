from __future__ import annotations

import os
from decimal import Decimal

import pytest

from app.execution.bybit_demo_postgres_session_risk_store import (
    PostgresBybitDemoSessionRiskLedgerStore,
)
from app.execution.bybit_demo_session_risk_ledger import (
    BybitDemoSessionRiskLedger,
    BybitDemoSessionTradeOutcome,
    observe_bybit_demo_session_equity,
    start_bybit_demo_session_risk_ledger,
)

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ASTRA_DEMO_SESSION_RISK_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ASTRA_DEMO_SESSION_RISK_TEST_DSN is not configured",
)


def _outcome(
    entry_order_link_id: str,
    *,
    created: int,
    updated: int,
    pnl: str,
    fees: str = "1.25",
) -> BybitDemoSessionTradeOutcome:
    return BybitDemoSessionTradeOutcome(
        entry_order_link_id=entry_order_link_id,
        symbol="BTCUSDT",
        created_time_ms=created,
        updated_time_ms=updated,
        all_in_net_pnl_usdt=Decimal(pnl),
        execution_fees_usdt=Decimal(fees),
    )


def test_postgres_session_risk_survives_restart_and_rejects_history_reset() -> None:
    store = PostgresBybitDemoSessionRiskLedgerStore(_DSN)
    store.migrate()

    with pytest.raises(FileNotFoundError):
        store.load(expected_opening_equity_usdt=Decimal("1000"))

    initial_ledger = start_bybit_demo_session_risk_ledger(
        opening_equity_usdt=Decimal("1000")
    )
    initial = store.initialize(initial_ledger)
    assert initial.ledger.outcomes == ()
    assert store.live_mainnet_order_routing_allowed is False
    assert store.order_writes_supported is False
    assert store.automatic_reset_allowed is False
    assert store.immutable_trade_outcomes is True

    with pytest.raises(FileExistsError):
        store.initialize(initial_ledger)
    with pytest.raises(ValueError, match="without historical outcomes"):
        store.initialize(
            BybitDemoSessionRiskLedger(
                opening_equity_usdt=Decimal("1000"),
                peak_equity_usdt=Decimal("1000"),
                outcomes=(
                    _outcome(
                        "ASTRA-DEMO-E-ILLEGAL-IMPORT",
                        created=1,
                        updated=2,
                        pnl="-1",
                    ),
                ),
            )
        )

    observed = observe_bybit_demo_session_equity(
        initial.ledger,
        current_equity_usdt=Decimal("1125"),
    )
    high_water = store.save(observed, expected_revision=initial.revision)

    first_loss = _outcome(
        "ASTRA-DEMO-E-LOSS-001",
        created=100,
        updated=150,
        pnl="-5",
    )
    after_first = BybitDemoSessionRiskLedger(
        opening_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1125"),
        outcomes=(first_loss,),
    )
    first = store.save(after_first, expected_revision=high_water.revision)

    second_loss = _outcome(
        "ASTRA-DEMO-E-LOSS-002",
        created=200,
        updated=250,
        pnl="-7",
        fees="0.75",
    )
    after_second = BybitDemoSessionRiskLedger(
        opening_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1125"),
        outcomes=(first_loss, second_loss),
    )
    second = store.save(after_second, expected_revision=first.revision)

    restarted = PostgresBybitDemoSessionRiskLedgerStore(_DSN)
    loaded = restarted.load(expected_opening_equity_usdt=Decimal("1000"))
    assert loaded == second
    assert loaded.ledger.peak_equity_usdt == Decimal("1125")
    assert loaded.ledger.cumulative_realized_all_in_pnl_usdt == Decimal("-12")
    state = loaded.ledger.to_session_risk_state(current_equity_usdt=Decimal("988"))
    assert state.peak_equity_usdt == Decimal("1125")
    assert state.consecutive_losses == 2
    assert state.execution_cost_usdt == Decimal("2.00")

    with pytest.raises(RuntimeError, match="revision changed concurrently"):
        store.save(after_second, expected_revision=first.revision)

    removed_history = BybitDemoSessionRiskLedger(
        opening_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1125"),
        outcomes=(second_loss,),
    )
    with pytest.raises(ValueError, match="cannot change or disappear"):
        store.save(removed_history, expected_revision=second.revision)

    mutated_first = _outcome(
        "ASTRA-DEMO-E-LOSS-001",
        created=100,
        updated=150,
        pnl="-4",
    )
    mutated_history = BybitDemoSessionRiskLedger(
        opening_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1125"),
        outcomes=(mutated_first, second_loss),
    )
    with pytest.raises(ValueError, match="cannot change or disappear"):
        store.save(mutated_history, expected_revision=second.revision)

    lowered_peak = BybitDemoSessionRiskLedger(
        opening_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1000"),
        outcomes=(first_loss, second_loss),
    )
    with pytest.raises(ValueError, match="peak equity cannot decrease"):
        store.save(lowered_peak, expected_revision=second.revision)

    with psycopg.connect(_DSN, autocommit=False) as connection:
        with pytest.raises(psycopg.Error):
            with connection.transaction():
                connection.execute(
                    """UPDATE astra_bybit_demo_session_trade_outcome_v122
                       SET all_in_net_pnl_usdt=999
                       WHERE entry_order_link_id='ASTRA-DEMO-E-LOSS-001'"""
                )
        with pytest.raises(psycopg.Error):
            with connection.transaction():
                connection.execute(
                    "DELETE FROM astra_bybit_demo_session_trade_outcome_v122"
                )
        with pytest.raises(psycopg.Error):
            with connection.transaction():
                connection.execute(
                    "TRUNCATE astra_bybit_demo_session_trade_outcome_v122"
                )
        with pytest.raises(psycopg.Error):
            with connection.transaction():
                connection.execute(
                    """UPDATE astra_bybit_demo_session_risk_v122
                       SET peak_equity_usdt=1000
                       WHERE session_name='ACTIVE'"""
                )
        with pytest.raises(psycopg.Error):
            with connection.transaction():
                connection.execute(
                    "DELETE FROM astra_bybit_demo_session_risk_v122"
                )
        with pytest.raises(psycopg.Error):
            with connection.transaction():
                connection.execute("TRUNCATE astra_bybit_demo_session_risk_v122")

    final = restarted.load(expected_opening_equity_usdt=Decimal("1000"))
    assert final == second
