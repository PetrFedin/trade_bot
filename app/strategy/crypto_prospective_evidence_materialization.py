from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from app.strategy.crypto_prospective_exact_cell_matrix import (
    CryptoProspectiveExactCellDataset,
)

_MATERIALIZATION_INTERVAL_TARGET_SECONDS = 600


def build_prospective_evidence_materialization_metadata(
    dataset: CryptoProspectiveExactCellDataset,
    *,
    generated_at: datetime,
) -> dict[str, Any]:
    """Build deterministic source lineage for one read-only prospective report."""

    dataset.validate()
    moment = _utc(generated_at)
    seed_ids = sorted(item.prospective.base.seed_id for item in dataset.observations)
    signal_times = sorted(
        _parse_time(item.prospective.base.signal_available_at)
        for item in dataset.observations
    )
    complete_count = sum(
        item.cell_context_state == "CELL_COMPLETE" for item in dataset.observations
    )
    unavailable_count = sum(
        item.cell_context_state == "CELL_UNAVAILABLE" for item in dataset.observations
    )
    liquidation_qualified_count = sum(
        item.prospective.context_state == "COVERAGE_QUALIFIED"
        for item in dataset.observations
    )
    if complete_count + unavailable_count != len(dataset.observations):
        raise ValueError("prospective materialization cell-state counts do not reconcile")
    return {
        "source_observation_count": len(dataset.observations),
        "source_seed_set_sha256": _seed_set_sha256(seed_ids),
        "source_seed_hash_contract": "SORTED_SHA256_IDS_NEWLINE_DELIMITED_UTF8",
        "earliest_signal_available_at": (
            None if not signal_times else signal_times[0].isoformat()
        ),
        "latest_signal_available_at": (
            None if not signal_times else signal_times[-1].isoformat()
        ),
        "source_cell_complete_count": complete_count,
        "source_cell_unavailable_count": unavailable_count,
        "liquidation_coverage_qualified_count": liquidation_qualified_count,
        "materialized_at": moment.isoformat(),
        "materialization_interval_target_seconds": (
            _MATERIALIZATION_INTERVAL_TARGET_SECONDS
        ),
        "materialization_freshness_claim": "RUN_TIMESTAMP_ONLY",
        "source_lineage_complete": True,
        "trade_actionable": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def _seed_set_sha256(seed_ids: list[str]) -> str:
    for seed_id in seed_ids:
        if len(seed_id) != 64 or any(char not in "0123456789abcdef" for char in seed_id):
            raise ValueError("prospective materialization seed id must be lowercase sha256")
    encoded = "\n".join(seed_ids).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("prospective materialization timestamp must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["build_prospective_evidence_materialization_metadata"]
