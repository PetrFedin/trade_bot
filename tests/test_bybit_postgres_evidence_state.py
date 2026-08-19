# ruff: noqa: E402, I001

from __future__ import annotations

import os
from dataclasses import replace
from decimal import Decimal

import pytest

psycopg = pytest.importorskip("psycopg")
DSN = os.environ.get("ASTRA_TEST_POSTGRES_DSN")
if not DSN:
    pytest.skip(
        "Bybit PostgreSQL evidence tests require ASTRA_TEST_POSTGRES_DSN",
        allow_module_level=True,
    )

from app.execution.bybit_demo_entry_provenance import BybitDemoEntryDecisionProvenance
from app.execution.bybit_demo_post_trade_accounting import BybitDemoProfitOutcomeStatus
from app.execution.bybit_demo_profit_preservation_evidence import (
    BybitDemoProfitPreservationEvidence,
)
from app.execution.bybit_demo_ranked_fallback import (
    BybitDemoCandidateFallbackAttempt,
    BybitDemoCandidateFallbackStage,
)
from app.execution.bybit_demo_session_risk_ledger import (
    BybitDemoSessionRiskLedger,
    BybitDemoSessionTradeOutcome,
)
from app.execution.bybit_postgres_evidence_state import (
    PostgresBybitDemoEntryProvenanceStore,
    PostgresBybitDemoSessionRiskLedgerStore,
    PostgresBybitDemoTerminalEvidenceStore,
)
from app.execution.bybit_postgres_runtime_state import PostgresBybitDemoRuntimeLease
from app.strategy.crypto_perp import CryptoSide

_ENTRY = "ASTRA-DEMO-E-PG-EVIDENCE"
_REVISION = "a" * 64


@pytest.fixture(autouse=True)
def clean_evidence_tables() -> None:
    PostgresBybitDemoRuntimeLease(DSN, lease_name="evidence-migrator").migrate()
    with psycopg.connect(DSN, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE astra_bybit_terminal_evidence, astra_bybit_entry_provenance, "
            "astra_bybit_session_risk_ledger"
        )


def _provenance() -> BybitDemoEntryDecisionProvenance:
    return BybitDemoEntryDecisionProvenance(
        entry_order_link_id=_ENTRY,
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-19T10:00:00+00:00",
        selected_signal_rank=1,
        executable_candidate_count=2,
        candidate_audit_count=8,
        economic_shadow_selected_symbol="ETHUSDT",
        economic_shadow_selected_side="LONG",
        economic_shadow_differs_from_current=True,
        selected_after_fallback=True,
        fallback_attempts=(
            BybitDemoCandidateFallbackAttempt(
                symbol="ETHUSDT",
                side="LONG",
                stage=BybitDemoCandidateFallbackStage.PRE_ENTRY_QUOTE,
                reasons=("NEXT_OPEN_EXPECTED_NET_EDGE_BELOW_TARGET",),
                quote_price=Decimal("2000"),
                modeled_entry_price=Decimal("2001"),
            ),
        ),
        expected_net_edge_usd=Decimal("29.6"),
        risk_budget_usdt=Decimal("10"),
        quality_score=Decimal("2.4"),
        target_net_profit_usd=Decimal("20"),
        planned_reference_price=Decimal("100"),
        planned_reference_quantity=Decimal("2"),
        planned_notional_usdt=Decimal("200"),
        modeled_round_trip_cost_usdt=Decimal("0.4"),
        pre_entry_quote_price=Decimal("100"),
        pre_entry_modeled_entry_price=Decimal("100"),
        pre_entry_original_quantity=Decimal("2"),
        pre_entry_adjusted_quantity=Decimal("1.8"),
        pre_entry_quote_resized=True,
        pre_entry_quantity_retention_fraction=Decimal("0.9"),
        actual_average_entry_price=Decimal("100.05"),
        actual_filled_quantity=Decimal("1.8"),
        actual_fill_notional_usdt=Decimal("180.09"),
        actual_fill_adverse_slippage_bps_vs_modeled_entry=Decimal("5"),
        account_taker_fee_rate=Decimal("0.00055"),
        exit_mode="FIXED_20_TARGET",
        runner_admission_reasons=("RUNNER_EXPECTED_EDGE_BELOW_ADMISSION_GATE",),
        liquidation_safety_reason="SAFE",
        stop_to_liquidation_r=Decimal("2.5"),
        effective_account_equity_usdt=Decimal("1000"),
        effective_peak_equity_usdt=Decimal("1050"),
        margin_mode="REGULAR_MARGIN",
    )


def _evidence() -> BybitDemoProfitPreservationEvidence:
    return BybitDemoProfitPreservationEvidence(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        observation_count=12,
        observed_peak_favorable_r=Decimal("1.8"),
        observed_max_adverse_r=Decimal("0.4"),
        realized_gross_exit_r=Decimal("1.1"),
        observed_peak_capture_fraction=Decimal("0.6111111111"),
        giveback_from_observed_peak_to_exit_r=Decimal("0.7"),
        exit_exceeded_observed_peak=False,
        partial_close_seen=False,
        realized_gross_pnl_usdt=Decimal("11"),
        realized_net_after_execution_fees_usdt=Decimal("10.4"),
        execution_fees_usdt=Decimal("0.6"),
        account_closed_pnl_usdt=Decimal("10.35"),
        funding_net_usdt=Decimal("-0.05"),
        all_in_net_pnl_usdt=Decimal("10.30"),
        profit_outcome_status=BybitDemoProfitOutcomeStatus.FULLY_RECONCILED_PROFIT,
        positive_peak_nonpositive_gross_exit=False,
        gross_positive_fill_nonpositive=False,
        fill_positive_account_nonpositive=False,
        account_positive_all_in_nonpositive=False,
        positive_peak_nonpositive_all_in=False,
        fully_reconciled_all_in=True,
    )


def _ledger(*, pnl: str = "-5") -> BybitDemoSessionRiskLedger:
    return BybitDemoSessionRiskLedger(
        opening_equity_usdt=Decimal("1000"),
        outcomes=(
            BybitDemoSessionTradeOutcome(
                entry_order_link_id="ASTRA-DEMO-E-PG-SESSION",
                symbol="BTCUSDT",
                created_time_ms=100,
                updated_time_ms=150,
                all_in_net_pnl_usdt=Decimal(pnl),
                execution_fees_usdt=Decimal("1.25"),
            ),
        ),
    )


def test_entry_provenance_is_immutable_idempotent_and_loadable() -> None:
    store = PostgresBybitDemoEntryProvenanceStore(DSN)

    first = store.persist(_provenance())
    second = store.persist(_provenance())
    loaded = store.load(entry_order_link_id=_ENTRY)

    assert first.record_sha256 == second.record_sha256 == loaded.record_sha256
    assert first.idempotent_existing_record is False
    assert second.idempotent_existing_record is True
    assert loaded.provenance == _provenance()
    assert store.immutable_records is True
    assert store.realized_pnl_storage_allowed is False
    assert store.live_mainnet_order_routing_allowed is False


def test_entry_provenance_conflict_is_rejected() -> None:
    store = PostgresBybitDemoEntryProvenanceStore(DSN)
    store.persist(_provenance())

    with pytest.raises(RuntimeError, match="entry provenance conflict"):
        store.persist(replace(_provenance(), selected_signal_rank=2))


def test_entry_provenance_table_rejects_update_and_delete() -> None:
    PostgresBybitDemoEntryProvenanceStore(DSN).persist(_provenance())

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_bybit_entry_provenance SET record_sha256=%s "
                "WHERE entry_order_link_id=%s",
                ("b" * 64, _ENTRY),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "DELETE FROM astra_bybit_entry_provenance WHERE entry_order_link_id=%s",
                (_ENTRY,),
            )


def test_terminal_evidence_is_immutable_and_idempotent() -> None:
    store = PostgresBybitDemoTerminalEvidenceStore(DSN)

    first = store.persist(
        entry_order_link_id=_ENTRY,
        checkpoint_revision=_REVISION,
        evidence=_evidence(),
    )
    second = store.persist(
        entry_order_link_id=_ENTRY,
        checkpoint_revision=_REVISION,
        evidence=_evidence(),
    )

    assert first.record_sha256 == second.record_sha256
    assert first.idempotent_existing_record is False
    assert second.idempotent_existing_record is True
    assert store.immutable_records is True
    assert store.live_mainnet_order_routing_allowed is False


def test_terminal_evidence_conflict_is_rejected() -> None:
    store = PostgresBybitDemoTerminalEvidenceStore(DSN)
    store.persist(
        entry_order_link_id=_ENTRY,
        checkpoint_revision=_REVISION,
        evidence=_evidence(),
    )

    with pytest.raises(RuntimeError, match="terminal evidence conflict"):
        store.persist(
            entry_order_link_id=_ENTRY,
            checkpoint_revision="b" * 64,
            evidence=_evidence(),
        )


def test_terminal_evidence_table_rejects_update_and_delete() -> None:
    PostgresBybitDemoTerminalEvidenceStore(DSN).persist(
        entry_order_link_id=_ENTRY,
        checkpoint_revision=_REVISION,
        evidence=_evidence(),
    )

    with psycopg.connect(DSN) as connection:
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "UPDATE astra_bybit_terminal_evidence SET record_sha256=%s "
                "WHERE entry_order_link_id=%s",
                ("b" * 64, _ENTRY),
            )
        connection.rollback()
        with pytest.raises(psycopg.errors.RaiseException):
            connection.execute(
                "DELETE FROM astra_bybit_terminal_evidence WHERE entry_order_link_id=%s",
                (_ENTRY,),
            )


def test_session_risk_round_trip_and_optimistic_update() -> None:
    store = PostgresBybitDemoSessionRiskLedgerStore(DSN)

    initial = store.initialize(_ledger())
    loaded = store.load(expected_opening_equity_usdt=Decimal("1000"))
    updated = store.save(_ledger(pnl="-3"), expected_revision=loaded.revision)
    reloaded = store.load(expected_opening_equity_usdt=Decimal("1000"))

    assert loaded == initial
    assert reloaded == updated
    assert updated.revision != initial.revision
    assert reloaded.ledger.outcomes[0].all_in_net_pnl_usdt == Decimal("-3")
    assert store.live_mainnet_order_routing_allowed is False


def test_session_risk_never_auto_initializes_and_rejects_second_initialize() -> None:
    store = PostgresBybitDemoSessionRiskLedgerStore(DSN)

    with pytest.raises(FileNotFoundError):
        store.load(expected_opening_equity_usdt=Decimal("1000"))

    store.initialize(_ledger())
    with pytest.raises(FileExistsError):
        store.initialize(_ledger())


def test_session_risk_rejects_stale_revision_and_equity_mismatch() -> None:
    store = PostgresBybitDemoSessionRiskLedgerStore(DSN)
    initial = store.initialize(_ledger())
    store.save(_ledger(pnl="-4"), expected_revision=initial.revision)

    with pytest.raises(RuntimeError, match="revision changed concurrently"):
        store.save(_ledger(pnl="-2"), expected_revision=initial.revision)
    with pytest.raises(ValueError, match="opening equity mismatch"):
        store.load(expected_opening_equity_usdt=Decimal("999"))
