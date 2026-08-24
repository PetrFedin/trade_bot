from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.strategy.crypto_prospective_exact_cell_matrix import (
    CryptoProspectiveExactCellDataset,
    CryptoProspectiveExactCellObservation,
    CryptoProspectiveSourceEvidenceCell,
)
from app.strategy.crypto_prospective_liquidation_calibration_postgres import (
    PostgresCryptoProspectiveLiquidationCalibrationReader,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_REQUIRED_CELL_FIELDS = (
    "evidence_cell_key",
    "current_market_regime",
    "current_open_interest_regime",
    "current_crowding_regime",
    "current_prior_funding_regime",
    "current_stress_regime",
    "current_stress_score",
    "evidence_trade_count",
)


class PostgresCryptoProspectiveExactCellReader:
    """Read source-time v111 cell facts and final prospective outcomes without mutation."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("prospective exact-cell PostgreSQL DSN is required")
        self._dsn = dsn
        self._prospective_reader = PostgresCryptoProspectiveLiquidationCalibrationReader(dsn)

    @property
    def order_writes_supported(self) -> bool:
        return False

    @property
    def live_mainnet_order_routing_allowed(self) -> bool:
        return False

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=True)

    def load_dataset(
        self,
        *,
        signal_available_at_or_after: datetime | None = None,
        maximum_final_seeds: int = 100_000,
    ) -> CryptoProspectiveExactCellDataset:
        prospective = self._prospective_reader.load_dataset(
            signal_available_at_or_after=signal_available_at_or_after,
            maximum_final_seeds=maximum_final_seeds,
        )
        if not prospective.observations:
            dataset = CryptoProspectiveExactCellDataset(observations=())
            dataset.validate()
            return dataset
        seed_ids = [item.base.seed_id for item in prospective.observations]
        sources = self._load_sources(seed_ids)
        observations: list[CryptoProspectiveExactCellObservation] = []
        for item in prospective.observations:
            row = sources.get(item.base.seed_id)
            if row is None:
                observations.append(
                    CryptoProspectiveExactCellObservation(
                        prospective=item,
                        cell_context_state="CELL_UNAVAILABLE",
                        cell_unavailable_reason="SOURCE_V111_CANDIDATE_MISSING",
                        source_cell=None,
                    )
                )
                continue
            _validate_source_identity(row, item.base)
            missing = tuple(field for field in _REQUIRED_CELL_FIELDS if row.get(field) is None)
            if missing:
                observations.append(
                    CryptoProspectiveExactCellObservation(
                        prospective=item,
                        cell_context_state="CELL_UNAVAILABLE",
                        cell_unavailable_reason=(
                            "SOURCE_EXACT_CELL_INCOMPLETE:" + ",".join(missing)
                        ),
                        source_cell=None,
                    )
                )
                continue
            source = _source_cell(row)
            observations.append(
                CryptoProspectiveExactCellObservation(
                    prospective=item,
                    cell_context_state="CELL_COMPLETE",
                    cell_unavailable_reason=None,
                    source_cell=source,
                )
            )
        dataset = CryptoProspectiveExactCellDataset(observations=tuple(observations))
        dataset.validate()
        return dataset

    def _load_sources(self, seed_ids: list[str]) -> dict[str, Mapping[str, Any]]:
        sql = """SELECT
                seed.seed_id,
                seed.source_snapshot_id,
                seed.source_evidence_rank,
                seed.source_market_rank,
                seed.source_qualification_state,
                seed.symbol AS seed_symbol,
                seed.side AS seed_side,
                seed.decision_bar_start_at,
                candidate.evidence_rank,
                candidate.market_rank,
                candidate.symbol,
                candidate.qualification_state,
                candidate.signal_side,
                candidate.decision_time,
                candidate.signal_quality_score,
                candidate.current_market_regime,
                candidate.current_open_interest_regime,
                candidate.current_crowding_regime,
                candidate.current_prior_funding_regime,
                candidate.current_stress_regime,
                candidate.current_stress_score,
                candidate.evidence_cell_key,
                candidate.evidence_trade_count,
                candidate.evidence_sample_sufficient,
                candidate.evidence_profit_factor,
                candidate.evidence_win_rate,
                candidate.evidence_total_net_pnl_usdt,
                candidate.evidence_average_net_pnl_usdt,
                candidate.evidence_average_mfe_r,
                candidate.evidence_average_mae_r,
                candidate.evidence_drawdown_usdt,
                candidate.positive_historical_evidence
            FROM astra_bybit_shadow_seed_v112 AS seed
            LEFT JOIN astra_bybit_live_opportunity_candidate_v111 AS candidate
              ON candidate.snapshot_id = seed.source_snapshot_id
             AND candidate.symbol = seed.symbol
            WHERE seed.seed_id = ANY(%s::text[])
            ORDER BY seed.seed_id"""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, (seed_ids,))
                rows = cursor.fetchall()
        result: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            seed_id = str(row["seed_id"])
            if seed_id in result:
                raise RuntimeError("prospective exact-cell reader found duplicate source seed")
            if row.get("evidence_rank") is not None:
                result[seed_id] = row
        return result


def _validate_source_identity(row: Mapping[str, Any], base: Any) -> None:
    if str(row["seed_symbol"]) != base.symbol or str(row["symbol"]) != base.symbol:
        raise ValueError("prospective exact-cell source symbol mismatch")
    if str(row["seed_side"]) != base.side or str(row["signal_side"]) != base.side:
        raise ValueError("prospective exact-cell source side mismatch")
    if int(row["source_evidence_rank"]) != base.evidence_rank:
        raise ValueError("prospective exact-cell seed evidence rank mismatch")
    if int(row["evidence_rank"]) != base.evidence_rank:
        raise ValueError("prospective exact-cell candidate evidence rank mismatch")
    if int(row["source_market_rank"]) != base.market_rank:
        raise ValueError("prospective exact-cell seed market rank mismatch")
    if int(row["market_rank"]) != base.market_rank:
        raise ValueError("prospective exact-cell candidate market rank mismatch")
    if str(row["source_qualification_state"]) != base.qualification_state:
        raise ValueError("prospective exact-cell seed qualification mismatch")
    if str(row["qualification_state"]) != base.qualification_state:
        raise ValueError("prospective exact-cell candidate qualification mismatch")
    decision = _utc(row["decision_bar_start_at"])
    candidate_decision = _utc(row["decision_time"])
    if decision != candidate_decision:
        raise ValueError("prospective exact-cell source decision timestamp mismatch")
    candidate_quality = _optional_decimal(row.get("signal_quality_score"))
    if candidate_quality is None or candidate_quality != base.signal_quality_score:
        raise ValueError("prospective exact-cell source signal quality mismatch")


def _source_cell(row: Mapping[str, Any]) -> CryptoProspectiveSourceEvidenceCell:
    source = CryptoProspectiveSourceEvidenceCell(
        evidence_cell_key=str(row["evidence_cell_key"]),
        market_regime=str(row["current_market_regime"]),
        open_interest_regime=str(row["current_open_interest_regime"]),
        crowding_regime=str(row["current_crowding_regime"]),
        prior_funding_regime=str(row["current_prior_funding_regime"]),
        stress_regime=str(row["current_stress_regime"]),
        stress_score=int(row["current_stress_score"]),
        historical_trade_count=int(row["evidence_trade_count"]),
        historical_sample_sufficient=bool(row["evidence_sample_sufficient"]),
        historical_profit_factor=_optional_decimal(row.get("evidence_profit_factor")),
        historical_win_rate=_optional_decimal(row.get("evidence_win_rate")),
        historical_total_net_pnl_usdt=_optional_decimal(
            row.get("evidence_total_net_pnl_usdt")
        ),
        historical_average_net_pnl_usdt=_optional_decimal(
            row.get("evidence_average_net_pnl_usdt")
        ),
        historical_average_mfe_r=_optional_decimal(row.get("evidence_average_mfe_r")),
        historical_average_mae_r=_optional_decimal(row.get("evidence_average_mae_r")),
        historical_drawdown_usdt=_optional_decimal(row.get("evidence_drawdown_usdt")),
        positive_historical_evidence=bool(row["positive_historical_evidence"]),
    )
    source.validate()
    return source


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("prospective exact-cell decimal cannot be boolean")
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("prospective exact-cell decimal must be finite")
    return parsed


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("prospective exact-cell timestamp must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["PostgresCryptoProspectiveExactCellReader"]
