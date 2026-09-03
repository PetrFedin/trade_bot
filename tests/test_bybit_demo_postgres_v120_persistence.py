from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.execution.bybit_demo_postgres_audit_role import PostgresBybitDemoAuditRolePolicy
from app.execution.bybit_demo_postgres_runtime_lease import PostgresBybitDemoRuntimeLease
from app.execution.bybit_demo_postgres_v120_persistence import (
    PostgresBybitDemoApprovedEntryAuthorizationStoreV120,
    PostgresBybitDemoEntryProvenanceStoreV120,
    PostgresBybitDemoTerminalEvidenceStoreV120,
)
from app.execution.bybit_demo_v120_persistence_records import (
    BybitDemoApprovedEntryAuthorizationV120,
    BybitDemoEntryDecisionProvenanceV120,
    BybitDemoFallbackAttemptV120,
    BybitDemoTerminalEvidenceFactsV120,
    BybitDemoTerminalEvidenceV120,
)

psycopg = pytest.importorskip("psycopg")
sql = pytest.importorskip("psycopg.sql")
conninfo = pytest.importorskip("psycopg.conninfo")
conninfo_to_dict = conninfo.conninfo_to_dict
make_conninfo = conninfo.make_conninfo

DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "Bybit Demo v120 persistence tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

ROOT = Path(__file__).resolve().parents[1]
V120_MIGRATIONS = (
    ROOT / "migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql",
    ROOT / "migrations/v120/002_bybit_demo_audit_truncate_hardening.sql",
)


@dataclass(frozen=True)
class RuntimeRoleFixture:
    role: str
    runtime_dsn: str


def _hex64(char: str) -> str:
    return char * 64


def _link(label: str) -> str:
    return f"ASTRA-DEMO-{label}-{uuid.uuid4().hex}"


def _approval(entry_link: str) -> BybitDemoApprovedEntryAuthorizationV120:
    return BybitDemoApprovedEntryAuthorizationV120(
        approval_id=uuid.uuid4().hex + uuid.uuid4().hex,
        source_snapshot_id=uuid.uuid4().hex + uuid.uuid4().hex,
        source_evidence_rank=2,
        source_market_rank=4,
        symbol="BTCUSDT",
        side="LONG",
        decision_time="2026-09-03T12:00:00+00:00",
        signal_available_at="2026-09-03T12:00:01+00:00",
        approved_at="2026-09-03T12:00:10+00:00",
        expires_at="2026-09-03T12:02:10+00:00",
        expected_entry_order_link_id=entry_link,
        expected_close_order_link_id=_link("CLOSE"),
        authorized_at="2026-09-03T12:00:10+00:00",
    )


def _entry(entry_link: str) -> BybitDemoEntryDecisionProvenanceV120:
    return BybitDemoEntryDecisionProvenanceV120(
        entry_order_link_id=entry_link,
        symbol="BTCUSDT",
        side="LONG",
        decision_time="2026-09-03T12:00:00+00:00",
        selected_signal_rank=1,
        executable_candidate_count=1,
        candidate_audit_count=2,
        economic_shadow_selected_symbol=None,
        economic_shadow_selected_side=None,
        economic_shadow_differs_from_current=False,
        selected_after_fallback=False,
        fallback_attempts=(
            BybitDemoFallbackAttemptV120(
                symbol="ETHUSDT",
                side="SHORT",
                stage="ACCOUNT_FEE_ECONOMICS",
                reasons=("ACCOUNT_FEE_EXPECTED_NET_PROFIT_BELOW_TARGET",),
                quote_price=Decimal("3200"),
                modeled_entry_price=Decimal("3201"),
            ),
        ),
        expected_net_edge_usd=Decimal("2.00"),
        risk_budget_usdt=Decimal("10"),
        quality_score=Decimal("0.75"),
        target_net_profit_usd=Decimal("3.00"),
        planned_reference_price=Decimal("60000"),
        planned_reference_quantity=Decimal("0.001"),
        planned_notional_usdt=Decimal("60"),
        modeled_round_trip_cost_usdt=Decimal("0.10"),
        pre_entry_quote_price=Decimal("60001"),
        pre_entry_modeled_entry_price=Decimal("60002"),
        pre_entry_original_quantity=Decimal("0.001"),
        pre_entry_adjusted_quantity=Decimal("0.001"),
        pre_entry_quote_resized=False,
        pre_entry_quantity_retention_fraction=Decimal("1"),
        actual_average_entry_price=Decimal("60002.5"),
        actual_filled_quantity=Decimal("0.001"),
        actual_fill_notional_usdt=Decimal("60.0025"),
        actual_fill_adverse_slippage_bps_vs_modeled_entry=Decimal("0.0833305556"),
        account_taker_fee_rate=Decimal("0.00055"),
        exit_mode="FIXED_20_TARGET",
        runner_admission_reasons=(),
        liquidation_safety_reason=None,
        stop_to_liquidation_r=Decimal("9"),
        effective_account_equity_usdt=Decimal("1000"),
        effective_peak_equity_usdt=Decimal("1000"),
        margin_mode="ISOLATED",
    )


def _terminal(entry_link: str) -> BybitDemoTerminalEvidenceV120:
    return BybitDemoTerminalEvidenceV120(
        entry_order_link_id=entry_link,
        checkpoint_revision=_hex64("c"),
        evidence=BybitDemoTerminalEvidenceFactsV120(
            symbol="BTCUSDT",
            side="LONG",
            observation_count=10,
            observed_peak_favorable_r=Decimal("1.2"),
            observed_max_adverse_r=Decimal("-0.2"),
            realized_gross_exit_r=Decimal("0.7"),
            observed_peak_capture_fraction=Decimal("0.5833333333"),
            giveback_from_observed_peak_to_exit_r=Decimal("0.5"),
            exit_exceeded_observed_peak=False,
            partial_close_seen=False,
            realized_gross_pnl_usdt=Decimal("7"),
            realized_net_after_execution_fees_usdt=Decimal("6.5"),
            execution_fees_usdt=Decimal("0.5"),
            account_closed_pnl_usdt=Decimal("6.5"),
            funding_net_usdt=Decimal("-0.1"),
            all_in_net_pnl_usdt=Decimal("6.4"),
            profit_outcome_status="FULLY_RECONCILED_PROFIT",
            positive_peak_nonpositive_gross_exit=False,
            gross_positive_fill_nonpositive=False,
            fill_positive_account_nonpositive=False,
            account_positive_all_in_nonpositive=False,
            positive_peak_nonpositive_all_in=False,
            fully_reconciled_all_in=True,
        ),
    )


def _apply(path: Path) -> None:
    with psycopg.connect(DSN) as connection:
        connection.execute(path.read_text(encoding="utf-8"))
        connection.commit()


def _runtime_dsn(role: str, password: str) -> str:
    values = conninfo_to_dict(DSN)
    values["user"] = role
    values["password"] = password
    return make_conninfo(**values)


def _drop_role(role: str) -> None:
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
        connection.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


@pytest.fixture
def runtime_role() -> RuntimeRoleFixture:
    PostgresBybitDemoRuntimeLease(DSN).migrate()
    for migration in V120_MIGRATIONS:
        _apply(migration)
    role = f"astra_c2a3_runtime_{uuid.uuid4().hex[:12]}"
    password = "astra-c2a3-runtime-test-only"
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER PASSWORD {}").format(
                sql.Identifier(role),
                sql.Literal(password),
            )
        )
    PostgresBybitDemoAuditRolePolicy(DSN).reconcile(runtime_role=role)
    fixture = RuntimeRoleFixture(role=role, runtime_dsn=_runtime_dsn(role, password))
    yield fixture
    _drop_role(role)


def test_stores_have_no_migration_or_order_write_capability(runtime_role: RuntimeRoleFixture) -> None:
    stores = (
        PostgresBybitDemoApprovedEntryAuthorizationStoreV120(runtime_role.runtime_dsn),
        PostgresBybitDemoEntryProvenanceStoreV120(runtime_role.runtime_dsn),
        PostgresBybitDemoTerminalEvidenceStoreV120(runtime_role.runtime_dsn),
    )

    for store in stores:
        assert not hasattr(store, "migrate")
        assert store.automatic_migration_allowed is False
        assert store.runtime_ddl_allowed is False
        assert store.order_writes_supported is False
        assert store.live_mainnet_order_routing_allowed is False


def test_approval_store_is_idempotent_and_detects_identity_conflict(
    runtime_role: RuntimeRoleFixture,
) -> None:
    store = PostgresBybitDemoApprovedEntryAuthorizationStoreV120(runtime_role.runtime_dsn)
    approval = _approval(_link("APPROVAL"))

    first = store.persist(approval)
    second = store.persist(approval)
    loaded = store.load(entry_order_link_id=approval.expected_entry_order_link_id)

    assert first.idempotent_existing_record is False
    assert second.idempotent_existing_record is True
    assert first.record_sha256 == second.record_sha256 == loaded.record_sha256
    assert loaded.authorization == approval

    with pytest.raises(RuntimeError, match="conflict"):
        store.persist(replace(approval, symbol="ETHUSDT"))


def test_entry_store_is_outcome_free_idempotent_and_detects_conflict(
    runtime_role: RuntimeRoleFixture,
) -> None:
    store = PostgresBybitDemoEntryProvenanceStoreV120(runtime_role.runtime_dsn)
    entry = _entry(_link("PROVENANCE"))

    first = store.persist(entry)
    second = store.persist(entry)
    loaded = store.load(entry_order_link_id=entry.entry_order_link_id)

    assert first.idempotent_existing_record is False
    assert second.idempotent_existing_record is True
    assert loaded.provenance == entry
    assert loaded.provenance.realized_pnl_used_for_selection is False

    with pytest.raises(RuntimeError, match="conflict"):
        store.persist(replace(entry, quality_score=Decimal("0.80")))


def test_terminal_store_loads_fully_reconciled_diagnostics_and_detects_conflict(
    runtime_role: RuntimeRoleFixture,
) -> None:
    store = PostgresBybitDemoTerminalEvidenceStoreV120(runtime_role.runtime_dsn)
    terminal = _terminal(_link("TERMINAL"))

    first = store.persist(terminal)
    second = store.persist(terminal)
    loaded = store.load(entry_order_link_id=terminal.entry_order_link_id)

    assert first.idempotent_existing_record is False
    assert second.idempotent_existing_record is True
    assert loaded.terminal == terminal
    assert loaded.terminal.evidence.fully_reconciled_all_in is True
    assert loaded.terminal.evidence.exit_threshold_retuning_allowed is False

    with pytest.raises(RuntimeError, match="checkpoint identity conflict"):
        store.persist(replace(terminal, checkpoint_revision=_hex64("d")))


def test_c2a2_runtime_privileges_remain_exact_after_c2a3_store_use(
    runtime_role: RuntimeRoleFixture,
) -> None:
    approval = _approval(_link("PRIVILEGE"))
    PostgresBybitDemoApprovedEntryAuthorizationStoreV120(runtime_role.runtime_dsn).persist(
        approval
    )

    evidence = PostgresBybitDemoAuditRolePolicy(DSN).inspect(runtime_role=runtime_role.role)
    assert evidence.ready is True
    assert evidence.approval_privileges == ("INSERT", "SELECT")
    assert evidence.provenance_privileges == ("INSERT", "SELECT")
    assert evidence.terminal_privileges == ("INSERT", "SELECT")
    assert evidence.runtime_owned_tables == ()
    assert evidence.mutation_function_execute is False

    with psycopg.connect(runtime_role.runtime_dsn, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute("TRUNCATE TABLE astra_bybit_demo_entry_provenance_v120")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute(
                "ALTER TABLE astra_bybit_demo_terminal_evidence_v120 "
                "ADD COLUMN forbidden_c2a3 boolean"
            )
