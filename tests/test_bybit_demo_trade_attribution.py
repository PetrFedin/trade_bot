from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from app.execution.bybit_demo_entry_provenance import BybitDemoEntryDecisionProvenance
from app.execution.bybit_demo_post_trade_accounting import BybitDemoProfitOutcomeStatus
from app.execution.bybit_demo_profit_preservation_evidence import (
    BybitDemoProfitPreservationEvidence,
)
from app.execution.bybit_demo_ranked_fallback import (
    BybitDemoCandidateFallbackAttempt,
    BybitDemoCandidateFallbackStage,
)
from app.execution.bybit_demo_terminal_evidence_store import (
    BybitDemoTerminalEvidenceReceipt,
)
from app.execution.bybit_demo_trade_attribution import (
    BybitDemoTradeAttributionPolicy,
    build_bybit_demo_trade_attribution,
    summarize_bybit_demo_trade_attribution,
)
from app.strategy.crypto_perp import CryptoSide

_ENTRY = "ASTRA-DEMO-E-ATTRIBUTION"


def _provenance(
    *,
    side: CryptoSide = CryptoSide.LONG,
    rank: int = 1,
    fallback: bool = False,
    resized: bool = False,
    shadow_differs: bool = False,
    exit_mode: str = "FIXED_20_TARGET",
    expected_edge: str = "25",
    risk_budget: str = "10",
    slippage_bps: str = "4",
) -> BybitDemoEntryDecisionProvenance:
    attempts = ()
    if fallback:
        attempts = (
            BybitDemoCandidateFallbackAttempt(
                symbol="ETHUSDT",
                side="LONG",
                stage=BybitDemoCandidateFallbackStage.PRE_ENTRY_QUOTE,
                reasons=("NEXT_OPEN_EXPECTED_NET_EDGE_BELOW_TARGET",),
                quote_price=Decimal("2000"),
                modeled_entry_price=Decimal("2001"),
            ),
        )
    retention = Decimal("0.95") if resized else Decimal("1")
    return BybitDemoEntryDecisionProvenance(
        entry_order_link_id=_ENTRY,
        symbol="BTCUSDT",
        side=side,
        decision_time="2026-08-19T10:00:00+00:00",
        selected_signal_rank=rank,
        executable_candidate_count=2,
        candidate_audit_count=8,
        economic_shadow_selected_symbol="ETHUSDT" if shadow_differs else "BTCUSDT",
        economic_shadow_selected_side=side.value,
        economic_shadow_differs_from_current=shadow_differs,
        selected_after_fallback=fallback,
        fallback_attempts=attempts,
        expected_net_edge_usd=Decimal(expected_edge),
        risk_budget_usdt=Decimal(risk_budget),
        quality_score=Decimal("2.5"),
        target_net_profit_usd=Decimal("20"),
        planned_reference_price=Decimal("100"),
        planned_reference_quantity=Decimal("2"),
        planned_notional_usdt=Decimal("200"),
        modeled_round_trip_cost_usdt=Decimal("0.4"),
        pre_entry_quote_price=Decimal("100"),
        pre_entry_modeled_entry_price=Decimal("100"),
        pre_entry_original_quantity=Decimal("2"),
        pre_entry_adjusted_quantity=Decimal("1.9") if resized else Decimal("2"),
        pre_entry_quote_resized=resized,
        pre_entry_quantity_retention_fraction=retention,
        actual_average_entry_price=Decimal("100.04"),
        actual_filled_quantity=Decimal("1.9") if resized else Decimal("2"),
        actual_fill_notional_usdt=Decimal("190.076") if resized else Decimal("200.08"),
        actual_fill_adverse_slippage_bps_vs_modeled_entry=Decimal(slippage_bps),
        account_taker_fee_rate=Decimal("0.00055"),
        exit_mode=exit_mode,
        runner_admission_reasons=(),
        liquidation_safety_reason="SAFE",
        stop_to_liquidation_r=Decimal("2.5"),
        effective_account_equity_usdt=Decimal("1000"),
        effective_peak_equity_usdt=Decimal("1050"),
        margin_mode="REGULAR_MARGIN",
    )


def _evidence(
    *,
    side: CryptoSide = CryptoSide.LONG,
    all_in: str = "10",
    funding: str = "-0.5",
    peak_r: str = "2",
    mae_r: str = "0.4",
    capture: str | None = "0.55",
    giveback_r: str = "0.9",
) -> BybitDemoProfitPreservationEvidence:
    all_in_value = Decimal(all_in)
    outcome = (
        BybitDemoProfitOutcomeStatus.FULLY_RECONCILED_PROFIT
        if all_in_value > 0
        else BybitDemoProfitOutcomeStatus.FULLY_RECONCILED_LOSS
    )
    return BybitDemoProfitPreservationEvidence(
        symbol="BTCUSDT",
        side=side,
        observation_count=12,
        observed_peak_favorable_r=Decimal(peak_r),
        observed_max_adverse_r=Decimal(mae_r),
        realized_gross_exit_r=Decimal("1.2") if all_in_value > 0 else Decimal("-0.8"),
        observed_peak_capture_fraction=None if capture is None else Decimal(capture),
        giveback_from_observed_peak_to_exit_r=Decimal(giveback_r),
        exit_exceeded_observed_peak=False,
        partial_close_seen=False,
        realized_gross_pnl_usdt=Decimal("12") if all_in_value > 0 else Decimal("-8"),
        realized_net_after_execution_fees_usdt=(
            Decimal("11") if all_in_value > 0 else Decimal("-9")
        ),
        execution_fees_usdt=Decimal("1"),
        account_closed_pnl_usdt=all_in_value - Decimal(funding),
        funding_net_usdt=Decimal(funding),
        all_in_net_pnl_usdt=all_in_value,
        profit_outcome_status=outcome,
        positive_peak_nonpositive_gross_exit=False,
        gross_positive_fill_nonpositive=False,
        fill_positive_account_nonpositive=False,
        account_positive_all_in_nonpositive=False,
        positive_peak_nonpositive_all_in=all_in_value <= 0,
        fully_reconciled_all_in=True,
    )


def _receipt(entry_order_link_id: str = _ENTRY) -> BybitDemoTerminalEvidenceReceipt:
    return BybitDemoTerminalEvidenceReceipt(
        entry_order_link_id=entry_order_link_id,
        checkpoint_revision="a" * 64,
        record_sha256="b" * 64,
        idempotent_existing_record=False,
    )


def test_trade_attribution_joins_selection_execution_and_all_in_outcome() -> None:
    row = build_bybit_demo_trade_attribution(
        _provenance(),
        _evidence(),
        terminal_receipt=_receipt(),
    )

    assert row.entry_order_link_id == _ENTRY
    assert row.expected_net_edge_usd == Decimal("25")
    assert row.all_in_net_pnl_usdt == Decimal("10")
    assert row.all_in_edge_realization_fraction == Decimal("0.4")
    assert row.all_in_r_multiple == Decimal("1")
    assert row.actual_fill_adverse_slippage_bps_vs_modeled_entry == Decimal("4")
    assert row.observed_peak_favorable_r == Decimal("2")
    assert row.observed_max_adverse_r == Decimal("0.4")
    assert row.observed_peak_capture_fraction == Decimal("0.55")
    assert row.giveback_from_observed_peak_to_exit_r == Decimal("0.9")
    assert row.realized_pnl_used_for_online_selection is False
    assert row.automatic_selector_retuning_allowed is False
    assert row.automatic_exit_retuning_allowed is False
    assert row.strategy_promotion_allowed is False
    assert row.live_mainnet_order_routing_allowed is False


def test_trade_attribution_requires_exact_entry_id_symbol_and_side() -> None:
    with pytest.raises(ValueError, match="orderLinkId mismatch"):
        build_bybit_demo_trade_attribution(
            _provenance(),
            _evidence(),
            terminal_receipt=_receipt("ASTRA-DEMO-E-OTHER"),
        )
    with pytest.raises(ValueError, match="symbol/side mismatch"):
        build_bybit_demo_trade_attribution(
            _provenance(),
            _evidence(side=CryptoSide.SHORT),
            terminal_receipt=_receipt(),
        )


def test_pending_terminal_evidence_cannot_be_attributed_as_final_outcome() -> None:
    pending = replace(
        _evidence(),
        all_in_net_pnl_usdt=None,
        fully_reconciled_all_in=False,
        profit_outcome_status=BybitDemoProfitOutcomeStatus.ALL_IN_ACCOUNTING_PENDING,
    )

    with pytest.raises(ValueError, match="fully reconciled all-in evidence"):
        build_bybit_demo_trade_attribution(
            _provenance(),
            pending,
            terminal_receipt=_receipt(),
        )


def test_summary_attributes_profit_by_rank_fallback_resize_shadow_and_exit_mode() -> None:
    direct = build_bybit_demo_trade_attribution(
        _provenance(),
        _evidence(all_in="10"),
        terminal_receipt=_receipt(),
    )
    fallback = build_bybit_demo_trade_attribution(
        replace(
            _provenance(
                side=CryptoSide.SHORT,
                rank=2,
                fallback=True,
                resized=True,
                shadow_differs=True,
                exit_mode="OPEN_ENDED_RUNNER",
                expected_edge="20",
                risk_budget="10",
                slippage_bps="8",
            ),
            entry_order_link_id="ASTRA-DEMO-E-ATTRIBUTION-2",
        ),
        replace(_evidence(side=CryptoSide.SHORT, all_in="-5"), symbol="BTCUSDT"),
        terminal_receipt=_receipt("ASTRA-DEMO-E-ATTRIBUTION-2"),
    )

    report = summarize_bybit_demo_trade_attribution(
        (direct, fallback),
        policy=BybitDemoTradeAttributionPolicy(minimum_bucket_sample_size=2),
    )

    assert report["trade_count"] == 2
    assert report["overall"]["total_all_in_net_pnl_usdt"] == 5.0
    assert report["overall"]["profit_count"] == 1
    assert report["overall"]["loss_count"] == 1
    assert report["overall"]["sample_sufficient_for_hypothesis"] is True
    assert report["by_side"]["LONG"]["total_all_in_net_pnl_usdt"] == 10.0
    assert report["by_side"]["SHORT"]["total_all_in_net_pnl_usdt"] == -5.0
    assert report["by_signal_rank"]["1"]["trade_count"] == 1
    assert report["by_signal_rank"]["2"]["trade_count"] == 1
    assert report["by_fallback"]["DIRECT"]["trade_count"] == 1
    assert report["by_fallback"]["FALLBACK"]["trade_count"] == 1
    assert report["by_pre_entry_resize"]["RESIZED"]["trade_count"] == 1
    assert report["by_economic_shadow_agreement"]["DISAGREES"]["trade_count"] == 1
    assert report["by_exit_mode"]["OPEN_ENDED_RUNNER"]["trade_count"] == 1
    assert report["automatic_selector_retuning_allowed"] is False
    assert report["automatic_exit_retuning_allowed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["live_mainnet_order_routing_allowed"] is False


def test_small_bucket_is_descriptive_only_and_cannot_trigger_retuning() -> None:
    row = build_bybit_demo_trade_attribution(
        _provenance(),
        _evidence(),
        terminal_receipt=_receipt(),
    )

    report = summarize_bybit_demo_trade_attribution((row,))

    assert report["overall"]["sample_sufficient_for_hypothesis"] is False
    assert report["overall"]["automatic_retuning_allowed"] is False
    assert report["small_sample_buckets_are_descriptive_only"] is True
    assert report["automatic_selector_retuning_allowed"] is False
    assert report["automatic_exit_retuning_allowed"] is False
