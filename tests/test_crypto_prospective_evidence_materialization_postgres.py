from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.strategy.crypto_prospective_evidence_postgres import (
    PostgresCryptoProspectiveEvidenceStore,
    prospective_evidence_report_id,
)

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ASTRA_EXACT_CELL_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ASTRA_EXACT_CELL_TEST_DSN is not configured",
)


def _report() -> dict[str, object]:
    return {
        "diagnostic": "BYBIT_PROSPECTIVE_EXACT_EVIDENCE_CELL_MATRIX",
        "report_generated_at": "2026-08-24T12:00:00+00:00",
        "report_window": "ALL_AVAILABLE_PROSPECTIVE_HISTORY",
        "observation_count": 1,
        "source_lineage": {
            "source_observation_count": 1,
            "source_seed_set_sha256": "a" * 64,
            "source_seed_hash_contract": "SORTED_SHA256_IDS_NEWLINE_DELIMITED_UTF8",
            "earliest_signal_available_at": "2026-08-24T10:00:00+00:00",
            "latest_signal_available_at": "2026-08-24T10:00:00+00:00",
            "source_cell_complete_count": 1,
            "source_cell_unavailable_count": 0,
            "liquidation_coverage_qualified_count": 1,
            "materialized_at": "2026-08-24T12:00:00+00:00",
            "materialization_interval_target_seconds": 600,
            "materialization_freshness_claim": "RUN_TIMESTAMP_ONLY",
            "source_lineage_complete": True,
            "trade_actionable": False,
            "strategy_promotion_allowed": False,
            "demo_activation_allowed": False,
            "live_activation_allowed": False,
            "bybit_live_order_routing_allowed": False,
        },
        "trade_actionable": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def _migrate() -> None:
    sql = Path(
        "migrations/v118/001_bybit_prospective_evidence_materialization.sql"
    ).read_text(encoding="utf-8")
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(sql)


def test_store_persists_lineage_and_rejects_history_mutation() -> None:
    _migrate()
    report = _report()
    store = PostgresCryptoProspectiveEvidenceStore(_DSN)
    report_id = store.persist(
        report,
        created_at=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
    )

    assert report_id == prospective_evidence_report_id(report)
    assert store.persist(report) == report_id
    assert store.order_writes_supported is False
    assert store.live_mainnet_order_routing_allowed is False

    with psycopg.connect(_DSN, autocommit=True) as connection:
        row = connection.execute(
            """SELECT source_observation_count, source_seed_set_sha256,
                      source_lineage_complete, trade_actionable,
                      bybit_live_order_routing_allowed
               FROM astra_bybit_prospective_exact_cell_report_v118
               WHERE report_id=%s""",
            (report_id,),
        ).fetchone()
        assert row == (1, "a" * 64, True, False, False)
        with pytest.raises(psycopg.Error, match="append-only"):
            connection.execute(
                """UPDATE astra_bybit_prospective_exact_cell_report_v118
                   SET source_observation_count=2
                   WHERE report_id=%s""",
                (report_id,),
            )
