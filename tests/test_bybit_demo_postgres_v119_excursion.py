from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal

import pytest

from app.execution.bybit_demo_postgres_runtime_lease import PostgresBybitDemoRuntimeLease
from app.execution.bybit_demo_postgres_runtime_role import PostgresBybitDemoRuntimeRolePolicy
from app.execution.bybit_demo_postgres_v119_excursion import PostgresBybitDemoExcursionStoreV119
from app.execution.bybit_demo_v119_excursion_records import BybitDemoExcursionStateV119

psycopg = pytest.importorskip("psycopg")
sql = pytest.importorskip("psycopg.sql")
conninfo = pytest.importorskip("psycopg.conninfo")
conninfo_to_dict = conninfo.conninfo_to_dict
make_conninfo = conninfo.make_conninfo

DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "C2A4 v119 excursion tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )


@dataclass(frozen=True)
class RuntimeRoleFixture:
    role: str
    runtime_dsn: str


def _state(*, observation_count: int = 0) -> BybitDemoExcursionStateV119:
    return BybitDemoExcursionStateV119(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=Decimal("60000"),
        initial_quantity=Decimal("0.010"),
        stop_fraction=Decimal("0.02"),
        observation_count=observation_count,
        latest_server_time_ms=None if observation_count == 0 else 1_700_000_000_000,
        latest_mark_price=None if observation_count == 0 else Decimal("60600"),
        latest_gross_r=Decimal("0") if observation_count == 0 else Decimal("0.5"),
        observed_peak_favorable_r=(
            Decimal("0") if observation_count == 0 else Decimal("0.7")
        ),
        observed_trough_r=Decimal("-0.1") if observation_count else Decimal("0"),
        latest_giveback_from_peak_r=(
            Decimal("0") if observation_count == 0 else Decimal("0.2")
        ),
        current_quantity=Decimal("0.010"),
        partial_close_seen=False,
        exchange_unrealised_pnl_usdt=(
            None if observation_count == 0 else Decimal("6.00")
        ),
        projected_initial_quantity_gross_pnl_usdt=(
            Decimal("0") if observation_count == 0 else Decimal("6.00")
        ),
        current_quantity_gross_pnl_usdt=(
            Decimal("0") if observation_count == 0 else Decimal("6.00")
        ),
    )


def _runtime_dsn(role: str, password: str) -> str:
    values = conninfo_to_dict(DSN)
    values["user"] = role
    values["password"] = password
    return make_conninfo(**values)


def _clear_checkpoint() -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute("DELETE FROM astra_bybit_demo_active_excursion_v119")


def _drop_role(role: str) -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture
def runtime_role() -> RuntimeRoleFixture:
    PostgresBybitDemoRuntimeLease(DSN).migrate()
    _clear_checkpoint()
    role = f"astra_c2a4_runtime_{uuid.uuid4().hex[:12]}"
    password = "astra-c2a4-runtime-test-only"
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER PASSWORD {}").format(
                sql.Identifier(role),
                sql.Literal(password),
            )
        )
    PostgresBybitDemoRuntimeRolePolicy(DSN).reconcile(runtime_role=role)
    fixture = RuntimeRoleFixture(role=role, runtime_dsn=_runtime_dsn(role, password))
    yield fixture
    _clear_checkpoint()
    _drop_role(role)


def test_runtime_adapter_has_no_migration_order_or_network_authority(
    runtime_role: RuntimeRoleFixture,
) -> None:
    store = PostgresBybitDemoExcursionStoreV119(runtime_role.runtime_dsn)

    assert not hasattr(store, "migrate")
    assert store.automatic_migration_allowed is False
    assert store.runtime_ddl_allowed is False
    assert store.order_writes_supported is False
    assert store.broker_network_supported is False
    assert store.market_data_reads_supported is False
    assert store.strategy_decisions_supported is False
    assert store.arm_halt_supported is False
    assert store.live_mainnet_order_routing_allowed is False


def test_initialize_load_and_duplicate_initialize_fail_closed(
    runtime_role: RuntimeRoleFixture,
) -> None:
    store = PostgresBybitDemoExcursionStoreV119(runtime_role.runtime_dsn)
    state = _state()
    link = f"ASTRA-DEMO-C2A4-{uuid.uuid4().hex}"

    initialized = store.initialize(entry_order_link_id=link, state=state)
    loaded = store.load()

    assert loaded == initialized
    assert loaded.state == state

    with pytest.raises(FileExistsError, match="already exists"):
        store.initialize(entry_order_link_id=link, state=state)


def test_exact_compare_and_swap_save_rejects_stale_and_cross_trade_identity(
    runtime_role: RuntimeRoleFixture,
) -> None:
    store = PostgresBybitDemoExcursionStoreV119(runtime_role.runtime_dsn)
    link = f"ASTRA-DEMO-C2A4-{uuid.uuid4().hex}"
    initialized = store.initialize(entry_order_link_id=link, state=_state())

    updated = store.save(
        entry_order_link_id=link,
        state=_state(observation_count=1),
        expected_revision=initialized.revision,
    )
    assert updated.revision != initialized.revision
    assert store.load() == updated

    with pytest.raises(RuntimeError, match="revision changed concurrently"):
        store.save(
            entry_order_link_id=link,
            state=_state(observation_count=2),
            expected_revision=initialized.revision,
        )

    with pytest.raises(ValueError, match="orderLinkId mismatch"):
        store.save(
            entry_order_link_id=f"ASTRA-DEMO-C2A4-OTHER-{uuid.uuid4().hex}",
            state=_state(observation_count=2),
            expected_revision=updated.revision,
        )


def test_compare_and_swap_clear_rejects_stale_revision(
    runtime_role: RuntimeRoleFixture,
) -> None:
    store = PostgresBybitDemoExcursionStoreV119(runtime_role.runtime_dsn)
    link = f"ASTRA-DEMO-C2A4-{uuid.uuid4().hex}"
    initialized = store.initialize(entry_order_link_id=link, state=_state())
    updated = store.save(
        entry_order_link_id=link,
        state=_state(observation_count=1),
        expected_revision=initialized.revision,
    )

    with pytest.raises(RuntimeError, match="revision changed before clear"):
        store.clear(expected_revision=initialized.revision)

    store.clear(expected_revision=updated.revision)
    with pytest.raises(FileNotFoundError):
        store.load()


def test_load_rejects_tampered_revision_and_unknown_state(
    runtime_role: RuntimeRoleFixture,
) -> None:
    store = PostgresBybitDemoExcursionStoreV119(runtime_role.runtime_dsn)
    link = f"ASTRA-DEMO-C2A4-{uuid.uuid4().hex}"
    initialized = store.initialize(entry_order_link_id=link, state=_state())

    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            """UPDATE astra_bybit_demo_active_excursion_v119
               SET revision=%s
               WHERE checkpoint_name='ACTIVE'""",
            ("a" * 64,),
        )
    with pytest.raises(ValueError, match="checksum mismatch"):
        store.load()

    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            """UPDATE astra_bybit_demo_active_excursion_v119
               SET revision=%s,
                   state_json=state_json || '{\"hidden_future_outcome\":\"1\"}'::jsonb
               WHERE checkpoint_name='ACTIVE'""",
            (initialized.revision,),
        )
    with pytest.raises(ValueError, match="keys are invalid"):
        store.load()


def test_c2a1_privileges_remain_exact_and_runtime_ddl_is_rejected(
    runtime_role: RuntimeRoleFixture,
) -> None:
    evidence = PostgresBybitDemoRuntimeRolePolicy(DSN).inspect(runtime_role=runtime_role.role)
    assert evidence.ready is True
    assert evidence.excursion_privileges == ("DELETE", "INSERT", "SELECT", "UPDATE")
    assert evidence.runtime_owned_tables == ()
    assert evidence.schema_create is False
    assert evidence.database_create is False

    with psycopg.connect(runtime_role.runtime_dsn, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("TRUNCATE TABLE astra_bybit_demo_active_excursion_v119")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "ALTER TABLE astra_bybit_demo_active_excursion_v119 "
                "ADD COLUMN forbidden_c2a4 boolean"
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("DROP TABLE astra_bybit_demo_active_excursion_v119")


def test_runtime_cannot_persist_unsafe_state(
    runtime_role: RuntimeRoleFixture,
) -> None:
    store = PostgresBybitDemoExcursionStoreV119(runtime_role.runtime_dsn)
    link = f"ASTRA-DEMO-C2A4-{uuid.uuid4().hex}"

    with pytest.raises(ValueError, match="strategy promotion"):
        store.initialize(
            entry_order_link_id=link,
            state=replace(_state(), strategy_promotion_allowed=True),
        )
