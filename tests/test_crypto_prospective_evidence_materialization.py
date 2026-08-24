from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from app.strategy.crypto_prospective_evidence_materialization import (
    build_prospective_evidence_materialization_metadata,
)


@dataclass(frozen=True)
class _Base:
    seed_id: str
    signal_available_at: str


@dataclass(frozen=True)
class _Prospective:
    base: _Base
    context_state: str


@dataclass(frozen=True)
class _Observation:
    prospective: _Prospective
    cell_context_state: str


@dataclass(frozen=True)
class _Dataset:
    observations: tuple[_Observation, ...]

    def validate(self) -> None:
        return None


def _row(
    seed_number: int,
    signal_time: str,
    *,
    cell_state: str = "CELL_COMPLETE",
    liquidation_state: str = "NOT_MATERIALIZED",
) -> _Observation:
    return _Observation(
        prospective=_Prospective(
            base=_Base(seed_id=f"{seed_number:064x}", signal_available_at=signal_time),
            context_state=liquidation_state,
        ),
        cell_context_state=cell_state,
    )


def test_materialization_lineage_hash_is_order_independent_and_has_watermarks() -> None:
    first = _row(1, "2026-08-24T10:00:00+00:00")
    second = _row(
        2,
        "2026-08-24T11:00:00+00:00",
        cell_state="CELL_UNAVAILABLE",
        liquidation_state="COVERAGE_QUALIFIED",
    )
    generated = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)

    left = build_prospective_evidence_materialization_metadata(
        _Dataset(observations=(second, first)),  # type: ignore[arg-type]
        generated_at=generated,
    )
    right = build_prospective_evidence_materialization_metadata(
        _Dataset(observations=(first, second)),  # type: ignore[arg-type]
        generated_at=generated,
    )

    expected_seed_payload = f"{1:064x}\n{2:064x}".encode()
    assert left["source_seed_set_sha256"] == hashlib.sha256(
        expected_seed_payload
    ).hexdigest()
    assert left["source_seed_set_sha256"] == right["source_seed_set_sha256"]
    assert left["source_observation_count"] == 2
    assert left["source_cell_complete_count"] == 1
    assert left["source_cell_unavailable_count"] == 1
    assert left["liquidation_coverage_qualified_count"] == 1
    assert left["earliest_signal_available_at"] == "2026-08-24T10:00:00+00:00"
    assert left["latest_signal_available_at"] == "2026-08-24T11:00:00+00:00"
    assert left["materialization_interval_target_seconds"] == 600
    assert left["source_lineage_complete"] is True
    assert left["trade_actionable"] is False
    assert left["bybit_live_order_routing_allowed"] is False


def test_empty_materialization_has_explicit_empty_lineage() -> None:
    metadata = build_prospective_evidence_materialization_metadata(
        _Dataset(observations=()),  # type: ignore[arg-type]
        generated_at=datetime(2026, 8, 24, 12, 0, tzinfo=UTC),
    )

    assert metadata["source_observation_count"] == 0
    assert metadata["source_seed_set_sha256"] == hashlib.sha256(b"").hexdigest()
    assert metadata["earliest_signal_available_at"] is None
    assert metadata["latest_signal_available_at"] is None
