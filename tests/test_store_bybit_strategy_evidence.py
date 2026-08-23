from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from tools.store_bybit_strategy_evidence import (
    prepare_evidence_import,
    store_evidence_payload,
)


def _report() -> dict[str, object]:
    return {
        "diagnostic": "BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX",
        "trade_count": 0,
        "cell_count": 0,
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


class _Store:
    def __init__(self) -> None:
        self.migrated = False
        self.calls: list[tuple[dict[str, Any], datetime]] = []

    def migrate(self) -> None:
        self.migrated = True

    def persist_evidence_report(self, report, *, observed_at: datetime) -> str:
        from app.strategy.crypto_live_evidence_postgres import evidence_report_id

        self.calls.append((dict(report), observed_at))
        return evidence_report_id(report)


def test_full_research_artifact_uses_embedded_observed_time_and_canonical_id() -> None:
    payload = {
        "observed_at": "2026-08-23T12:00:00+00:00",
        "strategy_evidence_matrix": _report(),
    }
    report, observed_at, evidence_id = prepare_evidence_import(payload)
    assert report == _report()
    assert observed_at == datetime(2026, 8, 23, 12, tzinfo=UTC)
    assert len(evidence_id) == 64

    store = _Store()
    persisted = store_evidence_payload(payload, store=store, migrate=True)
    assert persisted == evidence_id
    assert store.migrated is True
    assert store.calls == [(report, observed_at)]


def test_direct_matrix_requires_explicit_timezone_aware_observed_time() -> None:
    with pytest.raises(ValueError, match="requires explicit timezone-aware observed_at"):
        prepare_evidence_import(_report())
    with pytest.raises(ValueError, match="must be timezone-aware"):
        prepare_evidence_import(
            _report(),
            observed_at=datetime(2026, 8, 23, 12),
        )
