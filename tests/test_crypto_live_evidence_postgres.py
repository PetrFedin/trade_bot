from __future__ import annotations

from pathlib import Path

from app.strategy.crypto_live_evidence_postgres import (
    PostgresCryptoLiveEvidenceStore,
    evidence_report_id,
    extract_evidence_report,
)


def _report() -> dict[str, object]:
    return {
        "diagnostic": "BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX",
        "trade_count": 6,
        "cell_count": 1,
        "minimum_cell_trades": 5,
        "turnover_reference_usdt": "1000000",
        "matrix": [],
        "parameter_retuning_performed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def test_evidence_report_id_is_deterministic_and_extracts_nested_research() -> None:
    report = _report()
    first = evidence_report_id(report)
    second = evidence_report_id(dict(reversed(list(report.items()))))
    assert first == second
    assert len(first) == 64

    nested, observed_at = extract_evidence_report(
        {
            "observed_at": "2026-08-23T12:00:00+00:00",
            "strategy_evidence_matrix": report,
        }
    )
    assert nested == report
    assert observed_at is not None
    assert observed_at.isoformat() == "2026-08-23T12:00:00+00:00"


def test_postgres_store_has_no_order_surface_and_optional_dependency_is_lazy() -> None:
    store = PostgresCryptoLiveEvidenceStore("postgresql://example.invalid/astra")
    assert store.live_mainnet_order_routing_allowed is False
    assert store.order_writes_supported is False
    assert not hasattr(store, "place_order")
    assert not hasattr(store, "cancel_order")
    assert not hasattr(store, "amend_order")


def test_v111_schema_is_append_only_and_database_forces_non_trading_flags() -> None:
    sql = Path("migrations/v111/001_bybit_live_evidence_registry.sql").read_text(
        encoding="utf-8"
    )
    assert "append-only" in sql
    assert "operator_review_required = true" in sql
    assert "trade_actionable = false" in sql
    assert "strategy_promotion_allowed = false" in sql
    assert "demo_activation_allowed = false" in sql
    assert "live_activation_allowed = false" in sql
    assert "bybit_live_order_routing_allowed = false" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "astra_bybit_strategy_evidence_snapshot_v111" in sql
    assert "astra_bybit_live_opportunity_snapshot_v111" in sql
    assert "astra_bybit_live_opportunity_candidate_v111" in sql
