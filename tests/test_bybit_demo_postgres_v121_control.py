from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.execution.bybit_demo_postgres_v121_control import (
    PostgresBybitDemoControlJournalReaderV121,
    PostgresBybitDemoControlJournalWriterV121,
)
from app.execution.bybit_demo_postgres_v121_control_role import (
    PostgresBybitDemoControlJournalRolePolicyV121,
)
from app.execution.bybit_demo_v121_control_records import (
    BybitDemoControlModeV121,
    create_arm_control_event_v121,
    create_halt_control_event_v121,
)

psycopg = pytest.importorskip("psycopg")
sql = pytest.importorskip("psycopg.sql")
conninfo = pytest.importorskip("psycopg.conninfo")
conninfo_to_dict = conninfo.conninfo_to_dict
make_conninfo = conninfo.make_conninfo

DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "C2B0 v121 control-journal tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

_MIGRATION_001 = Path("migrations/v121/001_bybit_demo_control_plane.sql")
_MIGRATION_002 = Path("migrations/v121/002_bybit_demo_control_truncate_hardening.sql")
_FROZEN_001_SHA256 = "a03a738d7036c59338ef2ebe085a28a266fd0b440d97ca8e3fae59379efe8e21"
_CANONICAL_PREFLIGHT = (
    '{"account":"demo","schema":"CONNECTED_PREFLIGHT_V1",'
    '"status":"READY_FOR_MANUAL_OPERATOR_APPROVAL"}'
)
_NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class ControlRoleFixture:
    reader_role: str
    writer_role: str
    reader_dsn: str
    writer_dsn: str


def _role_dsn(role: str, password: str) -> str:
    values = conninfo_to_dict(DSN)
    values["user"] = role
    values["password"] = password
    return make_conninfo(**values)


def _apply_v121_migrations() -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(_MIGRATION_001.read_text(encoding="utf-8"))
        connection.execute(_MIGRATION_002.read_text(encoding="utf-8"))


def _drop_role(role: str) -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture(scope="module", autouse=True)
def v121_schema() -> None:
    _apply_v121_migrations()


@pytest.fixture
def control_roles(v121_schema: None) -> ControlRoleFixture:
    suffix = uuid.uuid4().hex[:10]
    reader_role = f"astra_c2b0_reader_{suffix}"
    writer_role = f"astra_c2b0_writer_{suffix}"
    reader_password = "astra-c2b0-reader-test-only"
    writer_password = "astra-c2b0-writer-test-only"
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                sql.Identifier(reader_role),
                sql.Literal(reader_password),
            )
        )
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                sql.Identifier(writer_role),
                sql.Literal(writer_password),
            )
        )
    PostgresBybitDemoControlJournalRolePolicyV121(DSN).reconcile(
        reader_role=reader_role,
        writer_role=writer_role,
    )
    fixture = ControlRoleFixture(
        reader_role=reader_role,
        writer_role=writer_role,
        reader_dsn=_role_dsn(reader_role, reader_password),
        writer_dsn=_role_dsn(writer_role, writer_password),
    )
    yield fixture
    _drop_role(reader_role)
    _drop_role(writer_role)


def test_historical_v121_001_is_byte_preserved(v121_schema: None) -> None:
    assert hashlib.sha256(_MIGRATION_001.read_bytes()).hexdigest() == _FROZEN_001_SHA256


def test_runtime_adapters_have_no_migration_broker_or_order_authority(
    control_roles: ControlRoleFixture,
) -> None:
    reader = PostgresBybitDemoControlJournalReaderV121(control_roles.reader_dsn)
    writer = PostgresBybitDemoControlJournalWriterV121(control_roles.writer_dsn)

    for adapter in (reader, writer):
        assert not hasattr(adapter, "migrate")
        assert adapter.automatic_migration_allowed is False
        assert adapter.runtime_ddl_allowed is False
        assert adapter.order_writes_supported is False
        assert adapter.order_submission_supported is False
        assert adapter.broker_network_supported is False
        assert adapter.market_data_reads_supported is False
        assert adapter.strategy_decisions_supported is False
        assert adapter.live_mainnet_order_routing_allowed is False


def test_reader_writer_privileges_are_exact_and_non_owner(
    control_roles: ControlRoleFixture,
) -> None:
    evidence = PostgresBybitDemoControlJournalRolePolicyV121(DSN).inspect(
        reader_role=control_roles.reader_role,
        writer_role=control_roles.writer_role,
    )

    assert evidence.ready is True
    assert evidence.append_trigger_ready is True
    assert evidence.truncate_trigger_ready is True

    assert evidence.reader.table_privileges == ("SELECT",)
    assert evidence.reader.sequence_privileges == ()
    assert evidence.writer.table_privileges == ("INSERT", "SELECT")
    assert evidence.writer.sequence_privileges == ("USAGE",)

    for role_state in (evidence.reader, evidence.writer):
        assert role_state.database_create is False
        assert role_state.schema_usage is True
        assert role_state.schema_create is False
        assert role_state.owns_schema is False
        assert role_state.owns_table is False
        assert role_state.owns_sequence is False
        assert role_state.role_memberships == ()
        assert role_state.owner_role_memberships == ()
        assert role_state.mutation_function_execute is False
        assert role_state.superuser is False
        assert role_state.createdb is False
        assert role_state.createrole is False
        assert role_state.replication is False
        assert role_state.bypassrls is False
        assert role_state.can_login is True


def test_writer_append_and_reader_rehydrate_halt_and_arm(
    control_roles: ControlRoleFixture,
) -> None:
    writer = PostgresBybitDemoControlJournalWriterV121(control_roles.writer_dsn)
    reader = PostgresBybitDemoControlJournalReaderV121(control_roles.reader_dsn)

    halt = create_halt_control_event_v121(
        operator_id=f"operator-{uuid.uuid4().hex}",
        reason="manual C2B0 halt",
        now=_NOW,
    )
    assert writer.append(halt) == halt
    assert reader.load_event(event_id=halt.event_id) == halt

    arm = create_arm_control_event_v121(
        operator_id=f"operator-{uuid.uuid4().hex}",
        reason="manual C2B0 arm",
        preflight_canonical_record=_CANONICAL_PREFLIGHT,
        now=_NOW + timedelta(seconds=1),
        preflight_observed_at=_NOW,
        ttl_seconds=120,
    )
    assert writer.append(arm) == arm
    assert reader.load_latest_event() == arm
    decision = reader.read_decision(now=_NOW + timedelta(seconds=2))
    assert decision.mode is BybitDemoControlModeV121.ARMED_NEW_ENTRIES
    assert decision.new_entry_allowed is True
    assert decision.latest_event_id == arm.event_id


def test_duplicate_event_id_fails_closed(control_roles: ControlRoleFixture) -> None:
    writer = PostgresBybitDemoControlJournalWriterV121(control_roles.writer_dsn)
    event = create_halt_control_event_v121(
        operator_id=f"operator-{uuid.uuid4().hex}",
        reason="duplicate-proof halt",
        now=_NOW + timedelta(seconds=3),
    )
    writer.append(event)
    with pytest.raises(FileExistsError, match="already exists"):
        writer.append(event)


def test_reader_writer_cannot_mutate_or_use_ddl(control_roles: ControlRoleFixture) -> None:
    with psycopg.connect(control_roles.reader_dsn, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                """INSERT INTO astra_bybit_demo_control_event_v121(
                   event_id,event_kind,operator_id,reason,created_at)
                   VALUES (%s,'HALT_NEW_ENTRIES','reader','forbidden',now())""",
                ("1" * 64,),
            )

    with psycopg.connect(control_roles.writer_dsn, autocommit=True) as connection:
        for statement in (
            "UPDATE astra_bybit_demo_control_event_v121 SET reason='forbidden'",
            "DELETE FROM astra_bybit_demo_control_event_v121",
            "TRUNCATE TABLE astra_bybit_demo_control_event_v121",
            "ALTER TABLE astra_bybit_demo_control_event_v121 ADD COLUMN forbidden boolean",
            "DROP TABLE astra_bybit_demo_control_event_v121",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(statement)


def test_owner_update_delete_and_truncate_are_physically_rejected(v121_schema: None) -> None:
    event = create_halt_control_event_v121(
        operator_id=f"bootstrap-{uuid.uuid4().hex}",
        reason="owner append-only proof",
        now=_NOW + timedelta(seconds=4),
    )
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            """INSERT INTO astra_bybit_demo_control_event_v121(
               event_id,event_kind,operator_id,reason,created_at,
               immutable_record,order_submission_supported,
               live_mainnet_order_routing_allowed)
               VALUES (%s,%s,%s,%s,%s,true,false,false)""",
            (
                event.event_id,
                event.event_kind.value,
                event.operator_id,
                event.reason,
                event.created_at,
            ),
        )
        with pytest.raises(psycopg.Error, match="append-only"):
            connection.execute(
                "UPDATE astra_bybit_demo_control_event_v121 SET reason='forbidden'"
            )
        with pytest.raises(psycopg.Error, match="append-only"):
            connection.execute("DELETE FROM astra_bybit_demo_control_event_v121")
        with pytest.raises(psycopg.Error, match="append-only"):
            connection.execute("TRUNCATE TABLE astra_bybit_demo_control_event_v121")


def test_tampered_arm_row_fails_rehydration_and_decision_halts(
    control_roles: ControlRoleFixture,
) -> None:
    reader = PostgresBybitDemoControlJournalReaderV121(control_roles.reader_dsn)
    observed_at = _NOW + timedelta(seconds=9)
    created_at = _NOW + timedelta(seconds=10)
    armed_until = created_at + timedelta(seconds=120)
    tampered_event_id = hashlib.sha256(
        f"c2b0-tamper:{uuid.uuid4().hex}".encode()
    ).hexdigest()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            """INSERT INTO astra_bybit_demo_control_event_v121(
               event_id,event_kind,operator_id,reason,
               preflight_status,preflight_record_sha256,preflight_canonical_record,
               preflight_observed_at,armed_until,created_at,
               immutable_record,order_submission_supported,
               live_mainnet_order_routing_allowed)
               VALUES (%s,'ARM_NEW_ENTRIES',%s,%s,
               'READY_FOR_MANUAL_OPERATOR_APPROVAL',%s,%s,%s,%s,%s,
               true,false,false)""",
            (
                tampered_event_id,
                f"tamper-{uuid.uuid4().hex}",
                "tampered preflight hash",
                "a" * 64,
                _CANONICAL_PREFLIGHT,
                observed_at,
                armed_until,
                created_at,
            ),
        )

    with pytest.raises(ValueError, match="audit hash mismatch|checksum mismatch"):
        reader.load_latest_event()
    decision = reader.read_decision(now=created_at + timedelta(seconds=1))
    assert decision.mode is BybitDemoControlModeV121.HALTED
    assert decision.new_entry_allowed is False
    assert decision.reasons == ("DEMO_CONTROL_EVENT_INVALID",)
