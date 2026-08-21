# ruff: noqa: E402, I001

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "Bybit recovery terminal-accounting E2E requires ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.application.bybit_operator_control import PostgresBybitOperatorControl
from app.domain.trading import OrderIntent, Side
from app.execution.bybit_demo import BybitDemoOrderAck, BybitDemoPosition
from app.execution.bybit_demo_cash_reconciliation import (
    build_bybit_demo_cash_baseline,
    reconcile_bybit_demo_cash,
)
from app.execution.bybit_demo_session_risk_ledger import (
    realized_all_in_pnl_for_utc_day,
    start_bybit_demo_session_risk_ledger,
)
from app.execution.bybit_demo_trading_runtime import (
    BybitDemoTradingRuntimeStatus,
    run_bybit_demo_trading_runtime,
)
from app.execution.bybit_entry_recovery import BybitEntryRecoveryEnvelope
from app.execution.bybit_entry_recovery_convergence import (
    BybitEntryRecoveryConvergenceStatus,
    converge_bybit_executed_entry_recovery,
)
from app.execution.bybit_order_lookup import BybitOrderTruth
from app.execution.bybit_postgres_cash_state import PostgresBybitDemoCashBaselineStore
from app.execution.bybit_postgres_entry_recovery import PostgresBybitEntryRecoveryStore
from app.execution.bybit_postgres_evidence_state import (
    PostgresBybitDemoSessionRiskLedgerStore,
    PostgresBybitDemoTerminalEvidenceStore,
)
from app.execution.bybit_postgres_runtime_state import (
    PostgresBybitDemoExcursionStore,
    PostgresBybitDemoRuntimeLease,
)
from app.execution.bybit_product_terminal_handoff import persist_product_terminal_state
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.oms.bybit_entry import PostgresBybitEntryOms, bybit_entry_intent_id
from app.oms.bybit_entry_recovery_candidates import PostgresBybitEntryRecoveryCandidateReader
from app.oms.store import OrderState
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan
from app.strategy.crypto_session_risk import CryptoSessionRiskState

NOW = datetime(2026, 8, 21, 18, 0, tzinfo=UTC)
ENTRY_LINK = "ASTRA-DEMO-E-PG-RECOVERY-ACCOUNTING"
ENTRY_TIME_MS = int(NOW.timestamp() * 1000)
EXIT_TIME_MS = ENTRY_TIME_MS + 5_000
ENTRY_PRICE = Decimal("100000")
EXIT_PRICE = Decimal("99000")
QUANTITY = Decimal("0.01")
ENTRY_FEE = Decimal("0.55")
EXIT_FEE = Decimal("0.5445")
GROSS_PNL = Decimal("-10")
ALL_IN_PNL = Decimal("-11.0945")
OPENING_CASH = Decimal("1000")
CLOSING_CASH = OPENING_CASH + ALL_IN_PNL
_MIGRATIONS = (
    Path("migrations/product/005_bybit_runtime_state.sql"),
    Path("migrations/product/007_bybit_cash_reconciliation.sql"),
    Path("migrations/product/008_bybit_entry_recovery.sql"),
)


class _RecoveryFlattenClient:
    live_mainnet_order_routing_allowed = False
    protection_state_read_supported = True

    def __init__(self) -> None:
        self.position: BybitDemoPosition | None = _broker_position()
        self.close_requests = []
        self.protection_attempts = 0

    def set_full_position_protection(self, _request):
        self.protection_attempts += 1
        raise RuntimeError("simulated exchange protection write failure")

    def set_open_ended_position_protection(self, _request):
        self.protection_attempts += 1
        raise RuntimeError("simulated exchange runner protection write failure")

    def place_market_order(self, request) -> BybitDemoOrderAck:
        assert request.reduce_only is True
        self.close_requests.append(request)
        self.position = None
        return BybitDemoOrderAck("broker-recovery-close-1", request.order_link_id, True)

    def get_positions(self, *, settle_coin: str = "USDT"):
        assert settle_coin == "USDT"
        return () if self.position is None else (self.position,)


class _TerminalTradeClient:
    live_mainnet_order_routing_allowed = False

    def __init__(self, *, close_order_link_id: str) -> None:
        self.close_order_link_id = close_order_link_id

    def get_positions(self, *, settle_coin: str = "USDT"):
        assert settle_coin == "USDT"
        return ()

    def get_executions(
        self,
        *,
        symbol: str,
        order_link_id: str | None = None,
        limit: int = 100,
    ):
        assert symbol == "BTCUSDT"
        assert limit == 100
        rows = (
            {
                "execId": "exec-entry-recovery-accounting",
                "orderId": "broker-entry-recovery-accounting",
                "orderLinkId": ENTRY_LINK,
                "symbol": "BTCUSDT",
                "side": "Buy",
                "execPrice": str(ENTRY_PRICE),
                "execQty": str(QUANTITY),
                "execFee": str(ENTRY_FEE),
                "execPnl": "0",
                "execTime": str(ENTRY_TIME_MS),
                "isMaker": False,
                "execType": "Trade",
            },
            {
                "execId": "exec-recovery-close-accounting",
                "orderId": "broker-recovery-close-1",
                "orderLinkId": self.close_order_link_id,
                "symbol": "BTCUSDT",
                "side": "Sell",
                "execPrice": str(EXIT_PRICE),
                "execQty": str(QUANTITY),
                "execFee": str(EXIT_FEE),
                "execPnl": str(GROSS_PNL),
                "execTime": str(EXIT_TIME_MS),
                "isMaker": False,
                "execType": "Trade",
            },
        )
        if order_link_id is None:
            return rows
        return tuple(row for row in rows if row["orderLinkId"] == order_link_id)


class _TerminalAccountingClient:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self) -> None:
        self.closed_pnl_reads = 0
        self.funding_reads = 0

    def get_closed_pnl(self, *, symbol: str, limit: int = 100, max_pages: int = 10):
        self.closed_pnl_reads += 1
        assert symbol == "BTCUSDT"
        assert limit == 100
        assert max_pages == 10
        return (
            {
                "symbol": "BTCUSDT",
                "orderId": "broker-recovery-close-1",
                "side": "Buy",
                "qty": str(QUANTITY),
                "avgEntryPrice": str(ENTRY_PRICE),
                "avgExitPrice": str(EXIT_PRICE),
                "closedPnl": str(ALL_IN_PNL),
                "openFee": str(ENTRY_FEE),
                "closeFee": str(EXIT_FEE),
                "createdTime": str(ENTRY_TIME_MS),
                "updatedTime": str(EXIT_TIME_MS),
            },
        )

    def get_transaction_log(
        self,
        *,
        symbol: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 50,
        max_pages: int = 20,
        transaction_type: str = "SETTLEMENT",
    ):
        self.funding_reads += 1
        assert symbol == "BTCUSDT"
        assert start_time_ms == ENTRY_TIME_MS
        assert end_time_ms == EXIT_TIME_MS
        assert limit == 50
        assert max_pages == 20
        assert transaction_type == "SETTLEMENT"
        return ()


class _ReadOnlyMarketDataBoundary:
    live_mainnet_order_routing_allowed = False


def _instrument() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.10"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("100"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _strategy() -> CryptoPerpStrategyConfig:
    return CryptoPerpStrategyConfig(taker_fee_rate=Decimal("0.00055"))


def _envelope() -> BybitEntryRecoveryEnvelope:
    return BybitEntryRecoveryEnvelope(
        entry_order_link_id=ENTRY_LINK,
        order_side="Buy",
        approved_order_quantity=QUANTITY,
        trade_plan=CryptoTradePlan(
            symbol="BTCUSDT",
            side=CryptoSide.LONG,
            decision_time=NOW.isoformat(),
            reference_price=ENTRY_PRICE,
            notional_usdt=ENTRY_PRICE * QUANTITY,
            reference_quantity=QUANTITY,
            risk_budget_usdt=Decimal("20"),
            stop_fraction=Decimal("0.01"),
            estimated_round_trip_cost_usdt=ENTRY_FEE + EXIT_FEE,
            estimated_stop_loss_after_cost_usdt=Decimal("11.0945"),
            target_net_profit_usd=Decimal("20"),
            required_move_fraction=Decimal("0.0210945"),
            expected_move_fraction=Decimal("0.05"),
            expected_net_edge_usd=Decimal("48.9055"),
            quality_score=Decimal("0.92"),
        ),
        instrument=_instrument(),
        strategy_config=_strategy(),
        planned_exit_mode="FIXED_20_TARGET",
    )


def _broker_position() -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=QUANTITY,
        average_price=ENTRY_PRICE,
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("50000"),
    )


def _broker_truth() -> BybitOrderTruth:
    return BybitOrderTruth(
        order_id="broker-entry-recovery-accounting",
        order_link_id=ENTRY_LINK,
        symbol="BTCUSDT",
        side="Buy",
        quantity=QUANTITY,
        cumulative_executed_quantity=QUANTITY,
        status="Filled",
        reject_reason="EC_NoError",
    )


def _intent() -> OrderIntent:
    return OrderIntent(
        intent_id=bybit_entry_intent_id(ENTRY_LINK),
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=QUANTITY,
        limit_price=ENTRY_PRICE,
        created_at=NOW,
        strategy_id="bybit-crypto-perp-v2",
    )


@pytest.fixture(autouse=True)
def clean_recovery_accounting_state() -> None:
    oms = PostgresBybitEntryOms(DSN)
    oms.migrate()
    operator = PostgresBybitOperatorControl(DSN)
    operator.migrate()
    PostgresBybitDemoRuntimeLease(DSN, lease_name="recovery-accounting-migration").migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        for migration in _MIGRATIONS:
            connection.execute(migration.read_text(encoding="utf-8"))
        connection.execute(
            "TRUNCATE astra_oms_outbox, astra_oms_events, astra_oms_orders "
            "RESTART IDENTITY CASCADE"
        )
        connection.execute(
            "TRUNCATE astra_bybit_runtime_events, astra_bybit_trades, "
            "astra_bybit_runtime_leases"
        )
        connection.execute(
            "TRUNCATE astra_bybit_terminal_evidence, astra_bybit_entry_provenance, "
            "astra_bybit_session_risk_ledger"
        )
        connection.execute("TRUNCATE astra_bybit_cash_baseline")
        connection.execute("TRUNCATE astra_bybit_entry_recovery")
        connection.execute("TRUNCATE astra_bybit_operator_actions")
        connection.execute(
            """UPDATE astra_bybit_operator_state
            SET mode='PAUSED', generation=1, updated_at=%s,
                updated_by='SYSTEM', reason='RECOVERY_ACCOUNTING_E2E_RESET'
            WHERE singleton=TRUE""",
            (NOW,),
        )
    operator.resume(
        actor="recovery-accounting-e2e",
        reason="authorize original demo entry before simulated crash",
        occurred_at=NOW,
        action_id="recovery-accounting-e2e-resume",
    )


def test_recovery_flatten_closes_terminal_accounting_session_pnl_and_cash_on_postgres() -> None:
    now = [NOW]
    oms = PostgresBybitEntryOms(DSN)
    claim = oms.claim_entry_submission(
        _intent(),
        client_order_id=ENTRY_LINK,
        occurred_at=NOW,
    )
    assert claim.record.state is OrderState.SUBMIT_STARTED
    PostgresBybitEntryRecoveryStore(DSN).persist(_envelope())

    session_store = PostgresBybitDemoSessionRiskLedgerStore(DSN)
    initial_session = session_store.initialize(
        start_bybit_demo_session_risk_ledger(opening_equity_usdt=OPENING_CASH)
    )
    cash_store = PostgresBybitDemoCashBaselineStore(DSN)
    cash_store.initialize(
        build_bybit_demo_cash_baseline(
            initial_session.ledger,
            session_revision=initial_session.revision,
            broker_wallet_balance_usdt=OPENING_CASH,
            created_time_ms=ENTRY_TIME_MS - 1,
        )
    )

    old_runtime = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name="recovery-accounting-e2e",
        ttl_seconds=10,
        clock=lambda: now[0],
        process_id=401,
    )
    old_lease = old_runtime.acquire()
    now[0] = NOW + timedelta(seconds=11)

    recovery_runtime = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name="recovery-accounting-e2e",
        ttl_seconds=10,
        clock=lambda: now[0],
        process_id=402,
    )
    excursion_store = PostgresBybitDemoExcursionStore(
        DSN,
        runtime_lease=recovery_runtime,
        clock=lambda: now[0],
    )
    recovery_client = _RecoveryFlattenClient()
    convergence = converge_bybit_executed_entry_recovery(
        claim.record,
        order_truth=_broker_truth(),
        positions=(_broker_position(),),
        recovery_store=PostgresBybitEntryRecoveryStore(DSN),
        runtime_lease=recovery_runtime,
        excursion_store=excursion_store,
        entry_oms=oms,
        client=recovery_client,
        occurred_at=now[0],
    )

    assert convergence.status is BybitEntryRecoveryConvergenceStatus.TERMINAL_HANDOFF_REQUIRED
    assert convergence.checkpoint is not None
    assert convergence.safety_result is not None
    assert convergence.safety_result.broker_position_closed is True
    assert recovery_client.protection_attempts == 1
    assert len(recovery_client.close_requests) == 1
    close_request = recovery_client.close_requests[0]
    assert close_request.reduce_only is True
    assert close_request.quantity == QUANTITY
    assert excursion_store.load() == convergence.checkpoint

    terminal_client = _TerminalTradeClient(close_order_link_id=close_request.order_link_id)
    accounting_client = _TerminalAccountingClient()
    now[0] = datetime.fromtimestamp((EXIT_TIME_MS + 1_000) / 1000, tz=UTC)
    terminal = run_bybit_demo_trading_runtime(
        {},
        instruments={"BTCUSDT": _instrument()},
        strategy_config=_strategy(),
        session_state=CryptoSessionRiskState(
            opening_equity_usdt=OPENING_CASH,
            current_equity_usdt=CLOSING_CASH,
            peak_equity_usdt=OPENING_CASH,
        ),
        now=now[0],
        now_ms=int(now[0].timestamp() * 1000),
        client=terminal_client,
        accounting_client=accounting_client,
        excursion_store=excursion_store,
        completed_bar_client=_ReadOnlyMarketDataBoundary(),
        quote_client=_ReadOnlyMarketDataBoundary(),
        runtime_lease=recovery_runtime,
        terminal_evidence_store=PostgresBybitDemoTerminalEvidenceStore(DSN),
        terminal_handoff=partial(
            persist_product_terminal_state,
            session_risk_store=session_store,
        ),
    )

    assert terminal.status is BybitDemoTradingRuntimeStatus.TERMINAL_HANDOFF_COMPLETE
    assert terminal.next_entry_allowed is True
    assert terminal.runtime_lease_acquired is True
    assert terminal.runtime_lease_released is True
    assert terminal.managed_poll is not None
    assert terminal.managed_poll.accounting is not None
    assert terminal.managed_poll.accounting.fully_reconciled_all_in_net_pnl_usdt == ALL_IN_PNL
    assert terminal.managed_poll.accounting.profit_outcome_status.value == "FULLY_RECONCILED_LOSS"
    assert terminal.terminal_handoff is not None
    assert terminal.terminal_handoff.session_risk_persisted is True
    assert accounting_client.closed_pnl_reads == 1
    assert accounting_client.funding_reads == 1

    with pytest.raises(FileNotFoundError):
        excursion_store.load()
    with pytest.raises(FileNotFoundError):
        recovery_runtime.inspect()
    with pytest.raises(RuntimeError):
        old_runtime.heartbeat(owner_token=old_lease.owner_token)

    final_oms = oms.get(bybit_entry_intent_id(ENTRY_LINK))
    assert final_oms is not None
    assert final_oms.state is OrderState.FILLED
    assert final_oms.filled_quantity == QUANTITY
    assert PostgresBybitEntryRecoveryCandidateReader(oms).load_candidates() == ()

    final_session = session_store.load_current()
    assert len(final_session.ledger.outcomes) == 1
    outcome = final_session.ledger.outcomes[0]
    assert outcome.entry_order_link_id == ENTRY_LINK
    assert outcome.all_in_net_pnl_usdt == ALL_IN_PNL
    assert outcome.execution_fees_usdt == ENTRY_FEE + EXIT_FEE
    assert final_session.ledger.cumulative_realized_all_in_pnl_usdt == ALL_IN_PNL
    assert realized_all_in_pnl_for_utc_day(
        final_session.ledger,
        now_ms=EXIT_TIME_MS + 1_000,
    ) == ALL_IN_PNL

    baseline = cash_store.load()
    cash = reconcile_bybit_demo_cash(
        baseline,
        final_session.ledger,
        broker_wallet_balance_usdt=CLOSING_CASH,
        active_trade=False,
    )
    assert cash.expected_wallet_balance_usdt == CLOSING_CASH
    assert cash.cash_mismatch_usdt == Decimal("0")
    assert cash.reconciled is True

    with psycopg.connect(DSN) as connection:
        terminal_rows = connection.execute(
            """SELECT checkpoint_revision, record_sha256
            FROM astra_bybit_terminal_evidence
            WHERE entry_order_link_id=%s""",
            (ENTRY_LINK,),
        ).fetchall()
    assert len(terminal_rows) == 1
    assert len(str(terminal_rows[0][0])) == 64
    assert len(str(terminal_rows[0][1])) == 64
