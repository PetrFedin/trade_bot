from __future__ import annotations

import ast
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.execution.bybit_demo_v120_persistence_records import (
    BybitDemoApprovedEntryAuthorizationV120,
    BybitDemoEntryDecisionProvenanceV120,
    BybitDemoFallbackAttemptV120,
    BybitDemoTerminalEvidenceFactsV120,
    BybitDemoTerminalEvidenceV120,
    canonical_sha256,
    decode_approved_entry_authorization_v120,
    decode_entry_provenance_v120,
    decode_terminal_evidence_v120,
    encode_approved_entry_authorization_v120,
    encode_entry_provenance_v120,
    encode_terminal_evidence_v120,
)

ROOT = Path(__file__).resolve().parents[1]
RECORD_MODULE = ROOT / "app/execution/bybit_demo_v120_persistence_records.py"
POSTGRES_MODULE = ROOT / "app/execution/bybit_demo_postgres_v120_persistence.py"


def _hex64(char: str) -> str:
    return char * 64


def _approval() -> BybitDemoApprovedEntryAuthorizationV120:
    return BybitDemoApprovedEntryAuthorizationV120(
        approval_id=_hex64("a"),
        source_snapshot_id=_hex64("b"),
        source_evidence_rank=3,
        source_market_rank=7,
        symbol="BTCUSDT",
        side="LONG",
        decision_time="2026-09-03T12:00:00+00:00",
        signal_available_at="2026-09-03T12:00:01+00:00",
        approved_at="2026-09-03T12:00:10+00:00",
        expires_at="2026-09-03T12:02:10+00:00",
        expected_entry_order_link_id="ASTRA-DEMO-ENTRY-C2A3",
        expected_close_order_link_id="ASTRA-DEMO-CLOSE-C2A3",
        authorized_at="2026-09-03T12:00:10+00:00",
    )


def _entry() -> BybitDemoEntryDecisionProvenanceV120:
    return BybitDemoEntryDecisionProvenanceV120(
        entry_order_link_id="ASTRA-DEMO-ENTRY-C2A3",
        symbol="BTCUSDT",
        side="LONG",
        decision_time="2026-09-03T12:00:00+00:00",
        selected_signal_rank=2,
        executable_candidate_count=3,
        candidate_audit_count=5,
        economic_shadow_selected_symbol="ETHUSDT",
        economic_shadow_selected_side="SHORT",
        economic_shadow_differs_from_current=True,
        selected_after_fallback=True,
        fallback_attempts=(
            BybitDemoFallbackAttemptV120(
                symbol="ETHUSDT",
                side="SHORT",
                stage="PRE_ENTRY_QUOTE",
                reasons=("STALE_QUOTE",),
                quote_price=Decimal("3200.5"),
                modeled_entry_price=Decimal("3201.1"),
            ),
        ),
        expected_net_edge_usd=Decimal("2.50"),
        risk_budget_usdt=Decimal("10"),
        quality_score=Decimal("0.81"),
        target_net_profit_usd=Decimal("3.50"),
        planned_reference_price=Decimal("60000"),
        planned_reference_quantity=Decimal("0.001"),
        planned_notional_usdt=Decimal("60"),
        modeled_round_trip_cost_usdt=Decimal("0.12"),
        pre_entry_quote_price=Decimal("60001"),
        pre_entry_modeled_entry_price=Decimal("60002"),
        pre_entry_original_quantity=Decimal("0.001"),
        pre_entry_adjusted_quantity=Decimal("0.0009"),
        pre_entry_quote_resized=True,
        pre_entry_quantity_retention_fraction=Decimal("0.9"),
        actual_average_entry_price=Decimal("60003"),
        actual_filled_quantity=Decimal("0.0009"),
        actual_fill_notional_usdt=Decimal("54.0027"),
        actual_fill_adverse_slippage_bps_vs_modeled_entry=Decimal("0.1666611113"),
        account_taker_fee_rate=Decimal("0.00055"),
        exit_mode="OPEN_ENDED_RUNNER",
        runner_admission_reasons=("ADMITTED",),
        liquidation_safety_reason=None,
        stop_to_liquidation_r=Decimal("8.5"),
        effective_account_equity_usdt=Decimal("1000"),
        effective_peak_equity_usdt=Decimal("1020"),
        margin_mode="ISOLATED",
    )


def _terminal() -> BybitDemoTerminalEvidenceV120:
    return BybitDemoTerminalEvidenceV120(
        entry_order_link_id="ASTRA-DEMO-ENTRY-C2A3",
        checkpoint_revision=_hex64("c"),
        evidence=BybitDemoTerminalEvidenceFactsV120(
            symbol="BTCUSDT",
            side="LONG",
            observation_count=12,
            observed_peak_favorable_r=Decimal("1.4"),
            observed_max_adverse_r=Decimal("-0.3"),
            realized_gross_exit_r=Decimal("0.8"),
            observed_peak_capture_fraction=Decimal("0.5714285714"),
            giveback_from_observed_peak_to_exit_r=Decimal("0.6"),
            exit_exceeded_observed_peak=False,
            partial_close_seen=False,
            realized_gross_pnl_usdt=Decimal("8.0"),
            realized_net_after_execution_fees_usdt=Decimal("7.4"),
            execution_fees_usdt=Decimal("0.6"),
            account_closed_pnl_usdt=Decimal("7.4"),
            funding_net_usdt=Decimal("-0.1"),
            all_in_net_pnl_usdt=Decimal("7.3"),
            profit_outcome_status="FULLY_RECONCILED_PROFIT",
            positive_peak_nonpositive_gross_exit=False,
            gross_positive_fill_nonpositive=False,
            fill_positive_account_nonpositive=False,
            account_positive_all_in_nonpositive=False,
            positive_peak_nonpositive_all_in=False,
            fully_reconciled_all_in=True,
        ),
    )


def test_approval_round_trip_preserves_historical_canonical_shape() -> None:
    approval = _approval()
    canonical, digest = encode_approved_entry_authorization_v120(approval)
    payload = json.loads(canonical)

    assert payload == {
        "approval_id": _hex64("a"),
        "approved_at": "2026-09-03T12:00:10+00:00",
        "authorized_at": "2026-09-03T12:00:10+00:00",
        "automatic_selector_retuning_allowed": False,
        "decision_time": "2026-09-03T12:00:00+00:00",
        "diagnostics_only": True,
        "environment": "BYBIT_DEMO",
        "expected_close_order_link_id": "ASTRA-DEMO-CLOSE-C2A3",
        "expected_entry_order_link_id": "ASTRA-DEMO-ENTRY-C2A3",
        "expires_at": "2026-09-03T12:02:10+00:00",
        "live_mainnet_order_routing_allowed": False,
        "operator_confirmed": True,
        "outcome_free": True,
        "side": "LONG",
        "signal_available_at": "2026-09-03T12:00:01+00:00",
        "single_use_entry_required": True,
        "source_evidence_rank": 3,
        "source_market_rank": 7,
        "source_snapshot_id": _hex64("b"),
        "strategy_promotion_allowed": False,
        "symbol": "BTCUSDT",
        "trade_actionable": False,
    }
    assert digest == canonical_sha256(canonical)
    assert decode_approved_entry_authorization_v120(canonical) == approval


def test_entry_round_trip_preserves_historical_decimal_and_list_encoding() -> None:
    entry = _entry()
    canonical, digest = encode_entry_provenance_v120(entry)
    payload = json.loads(canonical)

    assert payload["side"] == "LONG"
    assert payload["expected_net_edge_usd"] == "2.50"
    assert payload["pre_entry_adjusted_quantity"] == "0.0009"
    assert payload["runner_admission_reasons"] == ["ADMITTED"]
    assert payload["fallback_attempts"] == [
        {
            "modeled_entry_price": "3201.1",
            "quote_price": "3200.5",
            "reasons": ["STALE_QUOTE"],
            "side": "SHORT",
            "stage": "PRE_ENTRY_QUOTE",
            "symbol": "ETHUSDT",
        }
    ]
    assert "realized_pnl" not in canonical.lower()
    assert digest == canonical_sha256(canonical)
    assert decode_entry_provenance_v120(canonical) == entry


def test_terminal_round_trip_preserves_nested_historical_shape() -> None:
    terminal = _terminal()
    canonical, digest = encode_terminal_evidence_v120(terminal)
    payload = json.loads(canonical)

    assert set(payload) == {"entry_order_link_id", "checkpoint_revision", "evidence"}
    assert payload["evidence"]["side"] == "LONG"
    assert payload["evidence"]["all_in_net_pnl_usdt"] == "7.3"
    assert payload["evidence"]["funding_net_usdt"] == "-0.1"
    assert payload["evidence"]["fully_reconciled_all_in"] is True
    assert payload["evidence"]["exit_threshold_retuning_allowed"] is False
    assert digest == canonical_sha256(canonical)
    assert decode_terminal_evidence_v120(canonical) == terminal


def test_decoders_reject_hidden_or_future_outcome_fields() -> None:
    approval_canonical, _ = encode_approved_entry_authorization_v120(_approval())
    approval_payload = json.loads(approval_canonical)
    approval_payload["realized_pnl_usdt"] = "999"
    with pytest.raises(ValueError, match="key set mismatch"):
        decode_approved_entry_authorization_v120(json.dumps(approval_payload))

    entry_canonical, _ = encode_entry_provenance_v120(_entry())
    entry_payload = json.loads(entry_canonical)
    entry_payload["future_mfe_r"] = "10"
    with pytest.raises(ValueError, match="key set mismatch"):
        decode_entry_provenance_v120(json.dumps(entry_payload))

    terminal_canonical, _ = encode_terminal_evidence_v120(_terminal())
    terminal_payload = json.loads(terminal_canonical)
    terminal_payload["evidence"]["authorize_next_entry"] = True
    with pytest.raises(ValueError, match="key set mismatch"):
        decode_terminal_evidence_v120(json.dumps(terminal_payload))


def test_fail_closed_flags_cannot_be_relaxed() -> None:
    with pytest.raises(ValueError, match="cannot itself make a trade actionable"):
        encode_approved_entry_authorization_v120(replace(_approval(), trade_actionable=True))
    with pytest.raises(ValueError, match="cannot retune selection"):
        encode_approved_entry_authorization_v120(
            replace(_approval(), automatic_selector_retuning_allowed=True)
        )
    with pytest.raises(ValueError, match="cannot permit live routing"):
        encode_entry_provenance_v120(
            replace(_entry(), live_mainnet_order_routing_allowed=True)
        )
    with pytest.raises(ValueError, match="cannot use realized PnL"):
        encode_entry_provenance_v120(replace(_entry(), realized_pnl_used_for_selection=True))
    with pytest.raises(ValueError, match="fully reconciled"):
        encode_terminal_evidence_v120(
            replace(
                _terminal(),
                evidence=replace(
                    _terminal().evidence,
                    fully_reconciled_all_in=False,
                    all_in_net_pnl_usdt=None,
                ),
            )
        )
    with pytest.raises(ValueError, match="diagnostics-only"):
        encode_terminal_evidence_v120(
            replace(
                _terminal(),
                evidence=replace(_terminal().evidence, exit_threshold_retuning_allowed=True),
            )
        )


def test_persistence_modules_have_no_strategy_broker_marketdata_or_builder_imports() -> None:
    forbidden_prefixes = (
        "app.strategy",
        "app.marketdata",
        "app.execution.bybit_demo_operator_approval",
        "app.execution.bybit_demo_strategy_selector",
        "app.execution.bybit_demo_ranked_fallback",
        "app.execution.bybit_demo_cycle",
        "app.execution.bybit_demo_post_trade_accounting",
        "app.execution.bybit_demo_excursion",
    )
    for path in (RECORD_MODULE, POSTGRES_MODULE):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not [
            name
            for name in imported
            if any(name.startswith(prefix) for prefix in forbidden_prefixes)
        ]


def test_postgres_store_source_has_no_runtime_migration_or_order_mutation_methods() -> None:
    source = POSTGRES_MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    method_names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert "migrate" not in method_names
    assert "submit_order" not in method_names
    assert "cancel_order" not in method_names
    assert "replace_order" not in method_names
    assert "execute_order" not in method_names
    assert "UPDATE " not in source
    assert "DELETE FROM" not in source
    assert "TRUNCATE" not in source
