from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.strategy.crypto_prospective_ranking_calibration import (
    CryptoProspectiveCalibrationDataset,
    CryptoProspectiveCalibrationObservation,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresCryptoProspectiveCalibrationReader:
    """Read-only loader for deduplicated final prospective shadow outcomes."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("prospective calibration PostgreSQL DSN is required")
        self._dsn = dsn

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
    ) -> CryptoProspectiveCalibrationDataset:
        if isinstance(maximum_final_seeds, bool) or not 1 <= maximum_final_seeds <= 1_000_000:
            raise ValueError("prospective calibration maximum seed count is invalid")
        start = None if signal_available_at_or_after is None else _utc(
            signal_available_at_or_after
        )
        count_sql = """SELECT count(*) AS final_seed_count
            FROM astra_bybit_shadow_seed_v112 AS seed
            WHERE (%s::timestamptz IS NULL OR seed.signal_available_at >= %s)
              AND EXISTS (
                  SELECT 1
                  FROM astra_bybit_shadow_outcome_v112 AS outcome
                  WHERE outcome.seed_id = seed.seed_id AND outcome.final = true
              )"""
        rows_sql = """WITH first_final AS (
                SELECT DISTINCT ON (outcome.seed_id)
                    outcome.seed_id,
                    outcome.outcome_json,
                    outcome.observed_through,
                    outcome.evaluation_id
                FROM astra_bybit_shadow_outcome_v112 AS outcome
                WHERE outcome.final = true
                ORDER BY outcome.seed_id, outcome.observed_through, outcome.evaluation_id
            )
            SELECT
                seed.seed_id,
                seed.source_evidence_rank,
                seed.source_market_rank,
                seed.source_qualification_state,
                seed.symbol,
                seed.side,
                seed.signal_available_at,
                seed.signal_quality_score,
                seed.created_at AS seed_created_at,
                first_final.outcome_json
            FROM astra_bybit_shadow_seed_v112 AS seed
            JOIN first_final ON first_final.seed_id = seed.seed_id
            WHERE (%s::timestamptz IS NULL OR seed.signal_available_at >= %s)
            ORDER BY seed.signal_available_at, seed.symbol, seed.side,
                     seed.created_at, seed.seed_id"""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(count_sql, (start, start))
                count_row = cursor.fetchone()
                if count_row is None:
                    raise RuntimeError("prospective calibration count query returned no row")
                raw_count = int(count_row["final_seed_count"])
                if raw_count > maximum_final_seeds:
                    raise RuntimeError(
                        "prospective calibration refused truncated dataset:"
                        f"final_seeds={raw_count}:limit={maximum_final_seeds}"
                    )
                cursor.execute(rows_sql, (start, start))
                rows = cursor.fetchall()
        if len(rows) != raw_count:
            raise RuntimeError("prospective calibration row count changed during read")

        observations: list[CryptoProspectiveCalibrationObservation] = []
        seen_signals: set[tuple[str, str, str]] = set()
        for row in rows:
            signal_at = _utc(row["signal_available_at"]).isoformat()
            identity = (str(row["symbol"]), str(row["side"]), signal_at)
            if identity in seen_signals:
                continue
            seen_signals.add(identity)
            observations.append(_observation_from_row(row, signal_available_at=signal_at))
        dataset = CryptoProspectiveCalibrationDataset(
            raw_final_seed_count=raw_count,
            observations=tuple(observations),
        )
        dataset.validate()
        return dataset


def _observation_from_row(
    row: Mapping[str, Any],
    *,
    signal_available_at: str,
) -> CryptoProspectiveCalibrationObservation:
    payload = row.get("outcome_json")
    if not isinstance(payload, Mapping):
        raise ValueError("stored prospective calibration outcome JSON is not an object")
    if payload.get("final") is not True or payload.get("prospective") is not True:
        raise ValueError("prospective calibration requires a final prospective outcome")
    if payload.get("trade_actionable") is not False:
        raise ValueError("prospective calibration outcome unexpectedly became actionable")
    if payload.get("bybit_live_order_routing_allowed") is not False:
        raise ValueError("prospective calibration outcome unexpectedly enabled live routing")
    if payload.get("seed_id") != row.get("seed_id"):
        raise ValueError("prospective calibration outcome seed identity mismatch")
    if payload.get("symbol") != row.get("symbol") or payload.get("side") != row.get("side"):
        raise ValueError("prospective calibration outcome market identity mismatch")
    if payload.get("source_qualification_state") != row.get("source_qualification_state"):
        raise ValueError("prospective calibration outcome qualification state mismatch")
    if str(payload.get("signal_available_at", "")) != signal_available_at:
        raise ValueError("prospective calibration signal timestamp mismatch")
    first_touch_state = str(payload.get("first_touch_state", ""))
    if first_touch_state == "INCOMPLETE":
        raise ValueError("final prospective calibration outcome cannot be incomplete")
    horizons = payload.get("horizons")
    if not isinstance(horizons, list):
        raise ValueError("prospective calibration outcome horizons are missing")
    by_horizon: dict[int, Mapping[str, Any]] = {}
    for item in horizons:
        if not isinstance(item, Mapping):
            raise ValueError("prospective calibration horizon must be an object")
        horizon = _integer(item.get("horizon_minutes"), "horizon_minutes")
        if horizon in by_horizon:
            raise ValueError("prospective calibration contains duplicate horizon")
        if item.get("complete") is not True:
            raise ValueError("prospective calibration requires complete horizons")
        by_horizon[horizon] = item
    if tuple(sorted(by_horizon)) != (15, 60, 240):
        raise ValueError("prospective calibration requires 15m/60m/240m horizons")

    observation = CryptoProspectiveCalibrationObservation(
        seed_id=str(row["seed_id"]),
        evidence_rank=_integer(row["source_evidence_rank"], "source_evidence_rank"),
        market_rank=_integer(row["source_market_rank"], "source_market_rank"),
        qualification_state=str(row["source_qualification_state"]),
        symbol=str(row["symbol"]),
        side=str(row["side"]),
        signal_available_at=signal_available_at,
        signal_quality_score=_decimal(row["signal_quality_score"], "signal_quality_score"),
        first_touch_state=first_touch_state,
        first_touch_modeled_net_pnl_usdt=_optional_decimal(
            payload.get("first_touch_modeled_net_pnl_usdt"),
            "first_touch_modeled_net_pnl_usdt",
        ),
        mfe_r=_decimal(payload.get("mfe_r"), "mfe_r"),
        mae_r=_decimal(payload.get("mae_r"), "mae_r"),
        horizon_15_directional_return_fraction=_horizon_decimal(
            by_horizon[15],
            "directional_return_fraction",
        ),
        horizon_15_modeled_net_pnl_usdt=_horizon_decimal(
            by_horizon[15],
            "modeled_net_pnl_usdt",
        ),
        horizon_60_directional_return_fraction=_horizon_decimal(
            by_horizon[60],
            "directional_return_fraction",
        ),
        horizon_60_modeled_net_pnl_usdt=_horizon_decimal(
            by_horizon[60],
            "modeled_net_pnl_usdt",
        ),
        horizon_240_directional_return_fraction=_horizon_decimal(
            by_horizon[240],
            "directional_return_fraction",
        ),
        horizon_240_modeled_net_pnl_usdt=_horizon_decimal(
            by_horizon[240],
            "modeled_net_pnl_usdt",
        ),
    )
    observation.validate()
    return observation


def _horizon_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    return _decimal(row.get(field), field)


def _optional_decimal(value: Any, field: str) -> Decimal | None:
    return None if value is None else _decimal(value, field)


def _decimal(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"prospective calibration {field} is missing")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"prospective calibration {field} is invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"prospective calibration {field} must be finite")
    return parsed


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"prospective calibration {field} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"prospective calibration {field} is invalid") from exc
    return parsed


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("prospective calibration timestamp must be timezone-aware")
    return value.astimezone(UTC)
