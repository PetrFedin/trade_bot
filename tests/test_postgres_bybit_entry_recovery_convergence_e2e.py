# ruff: noqa: E402, I001

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "PostgreSQL Bybit entry recovery convergence requires ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.application.bybit_operator_control import PostgresBybitOperatorControl
from app.domain.trading import OrderIntent, Side
from app.execution.bybit_demo import (
    BybitDemoOrderAck,
    BybitDemoPosition,
    BybitDemoProtectionAck,
    BybitDemoProtectionRequest,
    BybitDemoRunnerProtectionRequest,
)
from app.execution.bybit_demo_protection_client import BybitDemoProtectionPosition
from app.execution.bybit_entry_recovery import BybitEntryRecoveryEnvelope
from app.execution.bybit_entry_recovery_convergence import (
    BybitEntryRecoveryConvergenceStatus,
    converge_bybit_executed_entry_recovery,
)
from app.execution.bybit_order_lookup import BybitOrderTruth
from app.execution.bybit_postgres_entry_recovery import PostgresBybitEntryRecoveryStore
from app.execution.bybit_postgres_runtime_state import (
    PostgresBybitDemoExcursionStore,
    PostgresBybitDemoRuntimeLease,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.oms.bybit_entry import PostgresBybitEntryOms, bybit_entry_intent_id
from app.oms.store import OrderState
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan

NOW = datetime(2026, 8, 21, 16, 30, tzinfo=UTC)
ENTRY_LINK = "ASTRA-DEMO-E-PG-CRASH-RECOVER"
_RECOVERY_MIGRATION = Path("migrations/product/008_bybit_entry_recovery.sql")


class _ProtectionClient:
    live_mainnet_order_routing_allowed = False
    protection_state_read_supported = True

    def __init__(self, position: BybitDemoPosition) -> None:
        self.position: BybitDemoPosition = position
        self.market_orders = []
        self.protection_writes = 0

    def set_full_position_protection(
        self,
        request: BybitDemoProtectionRequest,
    ) -> BybitDemoProtectionAck:
        self.protection_writes += 1
        self.position = BybitDemoProtectionPosition(
            symbol=self.position.symbol,
            side=self.position.side,
            size=self.position.size,
            average_price=self.position.average_price,
            unrealised_pnl=self.position.unrealised_pnl,
            liquidation_price=self.position.liquidation_price,
            take_profit_price=request.take_profit_price,
            stop_loss_price=request.stop_loss_price,
            trailing_stop_distance=None,
        )
        return BybitDemoProtectionAck(
            symbol=request.symbol,
            take_profit_price=request.take_profit_price,
            stop_loss_price=request.stop_loss_price,
        )

    def set_open_ended_position_protection(
        self,
        request: BybitDemoRunnerProtectionRequest,
    ):
        raise AssertionError(f"fixed recovery unexpectedly requested runner: {request}")

    def place_market_order(self, request) -> BybitDemoOrderAck:
        self.market_orders.append(request)
        raise AssertionError("qualified protected recovery must not place a market order")

    def get_positions(self, *, settle_coin: str = "USDT"):
        assert settle_coin == "USDT"
        return (self.position,)


@pytest.fixture(autouse=True)
def clean_product_recovery_state() -> None:
    oms = PostgresBybitEntryOms(DSN)
    oms.migrate()
    operator = PostgresBybitOperatorControl(DSN)
    operator.migrate()
    migrator = PostgresBybitDemoRuntimeLease(DSN, lease_name="entry-recovery-migration")
    migrator.migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(_RECOVERY_MIGRATION.read_text(encoding="utf-8"))
        connection.execute(
            "TRUNCATE astra_oms_outbox, astra_oms_events, astra_oms_orders "
            "RESTART IDENTITY CASCADE"
        )
        connection.execute(
            "TRUNCATE astra_bybit_runtime_events, astra_bybit_trades, "
            "astra_bybit_runtime_leases"
        )
        connection.execute("TRUNCATE astra_bybit_entry_recovery")
        connection.execute("TRUNCATE astra_bybit_operator_actions")
        connection.execute(
            """UPDATE astra_bybit_operator_state
            SET mode='PAUSED', generation=1, updated_at=%s,
                updated_by='SYSTEM', reason='RECOVERY_E2E_RESET'
            WHERE singleton=TRUE""",
            (NOW,),
        )
    operator.resume(
        actor="recovery-e2e",
        reason="authorize the original pre-crash demo entry",
        occurred_at=NOW,
        action_id="recovery-e2e-resume",
    )


def _envelope() -> BybitEntryRecoveryEnvelope:
    return BybitEntryRecoveryEnvelope(
        entry_order_link_id=ENTRY_LINK,
        order_side="Buy",
        approved_order_quantity=Decimal("0.01"),
        trade_plan=CryptoTradePlan(
            symbol="BTCUSDT",
            side=CryptoSide.LONG,
            decision_time="2026-08-21T16:29:00+00:00",
            reference_price=Decimal("100000"),
            notional_usdt=Decimal("1000"),
            reference_quantity=Decimal("0.01"),
            risk_budget_usdt=Decimal("20"),
            stop_fraction=Decimal("0.01"),
            estimated_round_trip_cost_usdt=Decimal("1.10"),
            estimated_stop_loss_after_cost_usdt=Decimal("11.10"),
            target_net_profit_usd=Decimal("20"),
            required_move_fraction=Decimal("0.0211"),
            expected_move_fraction=Decimal("0.05"),
            expected_net_edge_usd=Decimal("48.90"),
            quality_score=Decimal("0.92"),
        ),
        instrument=BybitInstrumentSpec(
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
        ),
        strategy_config=CryptoPerpStrategyConfig(taker_fee_rate=Decimal("0.00055")),
        planned_exit_mode="FIXED_20_TARGET",
    )


def _original_intent() -> OrderIntent:
    return OrderIntent(
        intent_id=bybit_entry_intent_id(ENTRY_LINK),
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=Decimal("0.01"),
        limit_price=Decimal("100000"),
        created_at=NOW,
        strategy_id="bybit-crypto-perp-v2",
    )


def _broker_position() -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal("0.01"),
        average_price=Decimal("100000"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("50000"),
    )


def _broker_truth() -> BybitOrderTruth:
    return BybitOrderTruth(
        order_id="broker-pg-crash-entry-1",
        order_link_id=ENTRY_LINK,
        symbol="BTCUSDT",
        side="Buy",
        quantity=Decimal("0.01"),
        cumulative_executed_quantity=Decimal("0.01"),
        status="Filled",
        reject_reason="EC_NoError",
    )


def test_real_postgres_crash_after_entry_recovers_fence_checkpoint_and_oms_without_second_entry() -> None:
    now = [NOW]
    oms = PostgresBybitEntryOms(DSN)
    claim = oms.claim_entry_submission(
        _original_intent(),
        client_order_id=ENTRY_LINK,
        occurred_at=NOW,
    )
    assert claim.record.state is OrderState.SUBMIT_STARTED
    assert claim.mutation_allowed is True

    recovery_store = PostgresBybitEntryRecoveryStore(DSN)
    receipt = recovery_store.persist(_envelope())
    assert receipt.entry_order_link_id == ENTRY_LINK

    old_runtime = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name="entry-recovery-e2e",
        ttl_seconds=10,
        clock=lambda: now[0],
        process_id=101,
    )
    old_lease = old_runtime.acquire()
    assert old_lease.fencing_token == 1

    now[0] = NOW + timedelta(seconds=11)
    recovery_runtime = PostgresBybitDemoRuntimeLease(
        DSN,
        lease_name="entry-recovery-e2e",
        ttl_seconds=10,
        clock=lambda: now[0],
        process_id=202,
    )
    excursion_store = PostgresBybitDemoExcursionStore(
        DSN,
        runtime_lease=recovery_runtime,
        clock=lambda: now[0],
    )
    client = _ProtectionClient(_broker_position())

    result = converge_bybit_executed_entry_recovery(
        claim.record,
        order_truth=_broker_truth(),
        positions=(_broker_position(),),
        recovery_store=recovery_store,
        runtime_lease=recovery_runtime,
        excursion_store=excursion_store,
        entry_oms=oms,
        client=client,
        occurred_at=now[0],
    )

    assert result.status is BybitEntryRecoveryConvergenceStatus.ACTIVE_MANAGEMENT_READY
    assert result.stale_lease_recovered is True
    assert result.runtime_lease_released is True
    assert result.next_entry_allowed is False
    assert result.checkpoint is not None
    assert result.checkpoint.entry_order_link_id == ENTRY_LINK
    assert result.checkpoint.state.entry_price == Decimal("100000")
    assert result.checkpoint.state.initial_quantity == Decimal("0.01")
    assert excursion_store.load() == result.checkpoint
    assert client.protection_writes == 1
    assert client.market_orders == []

    final_oms = oms.get(bybit_entry_intent_id(ENTRY_LINK))
    assert final_oms is not None
    assert final_oms.state is OrderState.FILLED
    assert final_oms.filled_quantity == Decimal("0.01")
    assert final_oms.broker_order_id == "broker-pg-crash-entry-1"
    assert oms.count_unresolved_entry_submissions() == 0

    with pytest.raises(RuntimeError):
        old_runtime.heartbeat(owner_token=old_lease.owner_token)
    with pytest.raises(FileNotFoundError):
        recovery_runtime.inspect()

    with psycopg.connect(DSN) as connection:
        events = connection.execute(
            """SELECT event_type, fencing_token
            FROM astra_bybit_runtime_events
            WHERE lease_name=%s""",
            ("entry-recovery-e2e",),
        ).fetchall()
    assert len(events) == 5
    assert set(events) == {
        ("LEASE_ACQUIRED", 1),
        ("LEASE_RECOVERED_AFTER_RECONCILIATION", 1),
        ("LEASE_ACQUIRED", 2),
        ("TRADE_INITIALIZED", 2),
        ("LEASE_RELEASED", 2),
    }
