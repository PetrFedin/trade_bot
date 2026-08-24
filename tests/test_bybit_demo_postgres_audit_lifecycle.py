from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_approval_lineage import (
    build_bybit_demo_approved_entry_authorization,
)
from app.execution.bybit_demo_approval_lineage_store import (
    JsonFileBybitDemoApprovedEntryAuthorizationStore,
)
from app.execution.bybit_demo_entry_provenance import BybitDemoEntryDecisionProvenance
from app.execution.bybit_demo_entry_provenance_store import (
    JsonFileBybitDemoEntryProvenanceStore,
)
from app.execution.bybit_demo_excursion_runtime import (
    BybitDemoExcursionRuntimeResult,
    BybitDemoExcursionRuntimeStatus,
)
from app.execution.bybit_demo_excursion_tracker import start_bybit_demo_trade_excursion
from app.execution.bybit_demo_managed_trade_poll import (
    BybitDemoManagedTradePollPhase,
    BybitDemoManagedTradePollResult,
)
from app.execution.bybit_demo_operator_approval import BybitDemoOperatorApproval
from app.execution.bybit_demo_post_trade_accounting import BybitDemoProfitOutcomeStatus
from app.execution.bybit_demo_postgres_approval_lineage_store import (
    PostgresBybitDemoApprovedEntryAuthorizationStore,
)
from app.execution.bybit_demo_postgres_entry_provenance_store import (
    PostgresBybitDemoEntryProvenanceStore,
)
from app.execution.bybit_demo_postgres_excursion_store import (
    PostgresBybitDemoExcursionStore,
)
from app.execution.bybit_demo_postgres_terminal_evidence_store import (
    PostgresBybitDemoTerminalEvidenceStore,
)
from app.execution.bybit_demo_profit_preservation_evidence import (
    BybitDemoProfitPreservationEvidence,
)
from app.execution.bybit_demo_ranked_fallback import (
    BybitDemoCandidateFallbackAttempt,
    BybitDemoCandidateFallbackStage,
)
from app.execution.bybit_demo_terminal_evidence_store import (
    JsonFileBybitDemoTerminalEvidenceStore,
)
from app.execution.bybit_demo_terminal_handoff import (
    BybitDemoTerminalHandoffStatus,
    persist_and_acknowledge_bybit_demo_terminal_evidence,
)
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ASTRA_DEMO_AUDIT_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ASTRA_DEMO_AUDIT_TEST_DSN is not configured",
)

_DECISION = datetime(2026, 8, 24, 12, tzinfo=UTC)
_APPROVED = _DECISION + timedelta(minutes=6)


def _reset_schema() -> None:
    v119 = Path("migrations/v119/001_bybit_demo_durable_runtime.sql").read_text(
        encoding="utf-8"
    )
    v120 = Path("migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql").read_text(
        encoding="utf-8"
    )
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute("DROP TABLE IF EXISTS astra_bybit_demo_terminal_evidence_v120 CASCADE")
        connection.execute("DROP TABLE IF EXISTS astra_bybit_demo_entry_provenance_v120 CASCADE")
        connection.execute(
            "DROP TABLE IF EXISTS astra_bybit_demo_approved_entry_authorization_v120 CASCADE"
        )
        connection.execute(
            "DROP FUNCTION IF EXISTS astra_reject_bybit_demo_audit_mutation_v120()"
        )
        connection.execute("DROP TABLE IF EXISTS astra_bybit_demo_active_excursion_v119 CASCADE")
        connection.execute("DROP TABLE IF EXISTS astra_bybit_demo_runtime_lease_v119 CASCADE")
        connection.execute(v119)
        connection.execute(v120)


def _approval() -> BybitDemoOperatorApproval:
    return BybitDemoOperatorApproval(
        source_snapshot_id="a" * 64,
        source_evidence_rank=1,
        source_market_rank=2,
        symbol="BTCUSDT",
        side="LONG",
        decision_time=_DECISION.isoformat(),
        signal_available_at=(_DECISION + timedelta(minutes=5)).isoformat(),
        signal_quality_score=Decimal("1.5"),
        source_planned_notional_usdt=Decimal("500"),
        source_risk_budget_usdt=Decimal("10"),
        source_modeled_round_trip_cost_usdt=Decimal("1"),
        maximum_entry_quantity=Decimal("5"),
        approved_at=_APPROVED.isoformat(),
        expires_at=(_APPROVED + timedelta(minutes=2)).isoformat(),
    )


def _review_row() -> dict[str, object]:
    approval = _approval()
    return {
        "snapshot_id": approval.source_snapshot_id,
        "evidence_rank": approval.source_evidence_rank,
        "market_rank": approval.source_market_rank,
        "symbol": approval.symbol,
        "qualification_state": "QUALIFIED_POSITIVE_EVIDENCE",
        "signal_side": approval.side,
        "decision_time": approval.decision_time,
        "signal_quality_score": approval.signal_quality_score,
        "expected_net_edge_usd": Decimal("25"),
        "planned_notional_usdt": approval.source_planned_notional_usdt,
        "risk_budget_usdt": approval.source_risk_budget_usdt,
        "estimated_round_trip_cost_usdt": approval.source_modeled_round_trip_cost_usdt,
        "evidence_sample_sufficient": True,
        "positive_historical_evidence": True,
        "operator_review_required": True,
        "trade_actionable": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def _provenance(entry_order_link_id: str) -> BybitDemoEntryDecisionProvenance:
    return BybitDemoEntryDecisionProvenance(
        entry_order_link_id=entry_order_link_id,
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-24T12:00:00+00:00",
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


def _trade_plan() -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-24T12:00:00+00:00",
        reference_price=Decimal("100"),
        notional_usdt=Decimal("200"),
        reference_quantity=Decimal("2"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.05"),
        estimated_round_trip_cost_usdt=Decimal("1"),
        estimated_stop_loss_after_cost_usdt=Decimal("11"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.105"),
        expected_move_fraction=Decimal("0.15"),
        expected_net_edge_usd=Decimal("29"),
        quality_score=Decimal("2"),
    )


def _position() -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal("2"),
        average_price=Decimal("100"),
        unrealised_pnl=Decimal("0"),
        liquidation_price=Decimal("50"),
    )


def _terminal_poll(checkpoint) -> BybitDemoManagedTradePollResult:
    excursion = BybitDemoExcursionRuntimeResult(
        status=BybitDemoExcursionRuntimeStatus.TERMINAL_EVIDENCE_READY,
        reasons=(),
        checkpoint=checkpoint,
        trade=object(),
        final=object(),
        checkpoint_clear_allowed=True,
    )
    return BybitDemoManagedTradePollResult(
        phase=BybitDemoManagedTradePollPhase.TERMINAL_EVIDENCE_READY,
        reasons=(),
        excursion=excursion,
        management=None,
        max_hold_close=None,
        accounting=SimpleNamespace(
            lifecycle=SimpleNamespace(next_entry_allowed=True),
            live_mainnet_order_routing_allowed=False,
        ),
        profit_evidence=_evidence(),
        terminal_evidence_ack_required=True,
        fully_reconciled_all_in=True,
    )


def test_postgres_approval_store_matches_file_identity_and_round_trips(tmp_path) -> None:
    _reset_schema()
    authorization = build_bybit_demo_approved_entry_authorization(
        _approval(),
        _review_row(),
        now=_APPROVED,
    )
    file_store = JsonFileBybitDemoApprovedEntryAuthorizationStore(tmp_path / "approval")
    postgres_store = PostgresBybitDemoApprovedEntryAuthorizationStore(_DSN)

    file_receipt = file_store.persist(authorization)
    first = postgres_store.persist(authorization)
    second = postgres_store.persist(authorization)
    loaded = postgres_store.load(
        entry_order_link_id=authorization.expected_entry_order_link_id
    )

    assert first.record_sha256 == file_receipt.record_sha256
    assert second.record_sha256 == first.record_sha256
    assert first.idempotent_existing_record is False
    assert second.idempotent_existing_record is True
    assert loaded.authorization == authorization
    assert loaded.record_sha256 == first.record_sha256
    assert postgres_store.order_writes_supported is False
    assert postgres_store.order_submission_supported is False
    assert postgres_store.outcome_storage_allowed is False
    assert postgres_store.realized_pnl_storage_allowed is False
    assert postgres_store.live_mainnet_order_routing_allowed is False

    conflicting = replace(
        authorization,
        approval_id="b" * 64,
        source_snapshot_id="c" * 64,
    )
    with pytest.raises(RuntimeError, match="conflict"):
        postgres_store.persist(conflicting)


def test_postgres_provenance_matches_file_identity_and_round_trips(tmp_path) -> None:
    _reset_schema()
    provenance = _provenance("ASTRA-DEMO-E-PG-PROVENANCE")
    file_store = JsonFileBybitDemoEntryProvenanceStore(tmp_path / "provenance")
    postgres_store = PostgresBybitDemoEntryProvenanceStore(_DSN)

    file_receipt = file_store.persist(provenance)
    first = postgres_store.persist(provenance)
    second = postgres_store.persist(provenance)
    loaded = postgres_store.load(entry_order_link_id=provenance.entry_order_link_id)

    assert first.record_sha256 == file_receipt.record_sha256
    assert second.record_sha256 == first.record_sha256
    assert first.idempotent_existing_record is False
    assert second.idempotent_existing_record is True
    assert loaded.provenance == provenance
    assert loaded.record_sha256 == first.record_sha256
    assert postgres_store.immutable_records is True
    assert postgres_store.realized_pnl_storage_allowed is False
    assert postgres_store.order_writes_supported is False
    assert postgres_store.live_mainnet_order_routing_allowed is False

    with pytest.raises(RuntimeError, match="entry provenance conflict"):
        postgres_store.persist(replace(provenance, selected_signal_rank=2))


def test_postgres_terminal_matches_file_identity_and_is_append_only(tmp_path) -> None:
    _reset_schema()
    entry_order_link_id = "ASTRA-DEMO-E-PG-TERMINAL"
    revision = "d" * 64
    evidence = _evidence()
    file_store = JsonFileBybitDemoTerminalEvidenceStore(tmp_path / "terminal")
    postgres_store = PostgresBybitDemoTerminalEvidenceStore(_DSN)

    file_receipt = file_store.persist(
        entry_order_link_id=entry_order_link_id,
        checkpoint_revision=revision,
        evidence=evidence,
    )
    first = postgres_store.persist(
        entry_order_link_id=entry_order_link_id,
        checkpoint_revision=revision,
        evidence=evidence,
    )
    second = postgres_store.persist(
        entry_order_link_id=entry_order_link_id,
        checkpoint_revision=revision,
        evidence=evidence,
    )

    assert first.record_sha256 == file_receipt.record_sha256
    assert second.record_sha256 == first.record_sha256
    assert first.idempotent_existing_record is False
    assert second.idempotent_existing_record is True
    assert postgres_store.immutable_records is True
    assert postgres_store.order_writes_supported is False
    assert postgres_store.live_mainnet_order_routing_allowed is False

    with pytest.raises(RuntimeError, match="terminal evidence conflict"):
        postgres_store.persist(
            entry_order_link_id=entry_order_link_id,
            checkpoint_revision="e" * 64,
            evidence=evidence,
        )

    with psycopg.connect(_DSN, autocommit=True) as connection:
        with pytest.raises(psycopg.Error, match="append-only"):
            connection.execute(
                """UPDATE astra_bybit_demo_terminal_evidence_v120
                   SET canonical_record='{}'
                   WHERE entry_order_link_id=%s""",
                (entry_order_link_id,),
            )


def test_postgres_terminal_handoff_persists_before_checkpoint_clear() -> None:
    _reset_schema()
    excursion_store = PostgresBybitDemoExcursionStore(_DSN)
    terminal_store = PostgresBybitDemoTerminalEvidenceStore(_DSN)
    entry_order_link_id = "ASTRA-DEMO-E-PG-HANDOFF"
    checkpoint = excursion_store.initialize(
        entry_order_link_id=entry_order_link_id,
        state=start_bybit_demo_trade_excursion(_trade_plan(), position=_position()),
    )

    result = persist_and_acknowledge_bybit_demo_terminal_evidence(
        _terminal_poll(checkpoint),
        evidence_store=terminal_store,
        excursion_store=excursion_store,
    )

    assert result.status is BybitDemoTerminalHandoffStatus.COMPLETE
    assert result.receipt is not None
    assert result.receipt.entry_order_link_id == entry_order_link_id
    assert result.receipt.checkpoint_revision == checkpoint.revision
    assert result.evidence_durable is True
    assert result.checkpoint_cleared is True
    assert result.next_entry_allowed is True
    assert result.live_mainnet_order_routing_allowed is False
    with pytest.raises(FileNotFoundError):
        excursion_store.load()
