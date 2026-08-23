from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.strategy.crypto_shadow_outcomes import (
    CryptoShadowOutcome,
    CryptoShadowSeed,
    CryptoShadowSourceCandidate,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_TRACKABLE_STATES = (
    "QUALIFIED_POSITIVE_EVIDENCE",
    "QUALIFIED_MIXED_EVIDENCE",
    "NO_SAMPLE_SUFFICIENT_EXACT_CELL",
    "DERIVATIVES_CONTEXT_INCOMPLETE",
)


class PostgresCryptoShadowOutcomeStore:
    """Append-only store for prospective fixed-strategy shadow observations."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("crypto shadow PostgreSQL DSN is required")
        self._dsn = dsn

    @property
    def live_mainnet_order_routing_allowed(self) -> bool:
        return False

    @property
    def order_writes_supported(self) -> bool:
        return False

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    def migrate(
        self,
        path: str | Path = "migrations/v112/001_bybit_prospective_shadow_outcomes.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def unseeded_sources(self, *, limit: int = 200) -> tuple[CryptoShadowSourceCandidate, ...]:
        if not 1 <= limit <= 5000:
            raise ValueError("crypto shadow source limit must be within [1, 5000]")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT
                        candidate.snapshot_id,
                        candidate.evidence_rank,
                        candidate.market_rank,
                        candidate.qualification_state,
                        candidate.symbol,
                        candidate.signal_side,
                        candidate.decision_time,
                        candidate.signal_quality_score,
                        candidate.planned_notional_usdt,
                        candidate.risk_budget_usdt,
                        candidate.estimated_round_trip_cost_usdt
                    FROM astra_bybit_live_opportunity_candidate_v111 AS candidate
                    WHERE candidate.qualification_state = ANY(%s)
                      AND candidate.signal_side IS NOT NULL
                      AND candidate.decision_time IS NOT NULL
                      AND candidate.signal_quality_score IS NOT NULL
                      AND candidate.planned_notional_usdt IS NOT NULL
                      AND candidate.risk_budget_usdt IS NOT NULL
                      AND candidate.estimated_round_trip_cost_usdt IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM astra_bybit_shadow_seed_v112 AS seed
                          WHERE seed.source_snapshot_id = candidate.snapshot_id
                            AND seed.symbol = candidate.symbol
                      )
                    ORDER BY candidate.snapshot_id, candidate.evidence_rank
                    LIMIT %s""",
                    (list(_TRACKABLE_STATES), limit),
                )
                rows = cursor.fetchall()
        result = tuple(_source_from_row(row) for row in rows)
        for source in result:
            source.validate()
        return result

    def persist_seed(
        self,
        seed: CryptoShadowSeed,
        *,
        created_at: datetime | None = None,
    ) -> str:
        seed.validate()
        moment = datetime.now(UTC) if created_at is None else _utc(created_at)
        payload_json = _canonical_json(seed.to_payload())
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_shadow_seed_v112
                        (seed_id, source_snapshot_id, source_evidence_rank,
                         source_market_rank, source_qualification_state, symbol, side,
                         decision_bar_start_at, signal_available_at, entry_price,
                         stop_price, target_price, planned_notional_usdt, risk_budget_usdt,
                         estimated_round_trip_cost_usdt, target_net_profit_usd,
                         signal_quality_score, seed_json, prospective,
                         operator_review_required, trade_actionable, demo_activation_allowed,
                         live_activation_allowed, bybit_live_order_routing_allowed, created_at)
                        VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         %s, %s, %s, %s::jsonb, true, true, false, false, false, false, %s)
                        ON CONFLICT (source_snapshot_id, symbol) DO NOTHING""",
                        (
                            seed.seed_id,
                            seed.source_snapshot_id,
                            seed.source_evidence_rank,
                            seed.source_market_rank,
                            seed.source_qualification_state,
                            seed.symbol,
                            seed.side,
                            _parse_time(seed.decision_bar_start_at),
                            _parse_time(seed.signal_available_at),
                            seed.entry_price,
                            seed.stop_price,
                            seed.target_price,
                            seed.planned_notional_usdt,
                            seed.risk_budget_usdt,
                            seed.estimated_round_trip_cost_usdt,
                            seed.target_net_profit_usd,
                            seed.signal_quality_score,
                            payload_json,
                            moment,
                        ),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """SELECT seed_id, seed_json
                            FROM astra_bybit_shadow_seed_v112
                            WHERE source_snapshot_id=%s AND symbol=%s""",
                            (seed.source_snapshot_id, seed.symbol),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("crypto shadow seed idempotency lookup lost row")
                        if row["seed_id"] != seed.seed_id:
                            raise ValueError(
                                "crypto shadow source identity produced divergent seed"
                            )
                        if _canonical_json(row["seed_json"]) != payload_json:
                            raise ValueError("crypto shadow seed payload mismatch")
        return seed.seed_id

    def active_seeds(self, *, limit: int = 500) -> tuple[CryptoShadowSeed, ...]:
        if not 1 <= limit <= 5000:
            raise ValueError("crypto shadow active seed limit must be within [1, 5000]")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT seed.seed_json
                    FROM astra_bybit_shadow_seed_v112 AS seed
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM astra_bybit_shadow_outcome_v112 AS outcome
                        WHERE outcome.seed_id = seed.seed_id
                          AND outcome.final = true
                    )
                    ORDER BY seed.signal_available_at, seed.seed_id
                    LIMIT %s""",
                    (limit,),
                )
                rows = cursor.fetchall()
        seeds = tuple(_seed_from_payload(row["seed_json"]) for row in rows)
        for seed in seeds:
            seed.validate()
        return seeds

    def persist_outcome(
        self,
        outcome: CryptoShadowOutcome,
        *,
        created_at: datetime | None = None,
    ) -> str:
        outcome.validate()
        moment = datetime.now(UTC) if created_at is None else _utc(created_at)
        payload_json = _canonical_json(outcome.to_payload())
        horizons = {item.horizon_minutes: item for item in outcome.horizons}
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_shadow_outcome_v112
                        (evaluation_id, seed_id, source_snapshot_id,
                         source_qualification_state, symbol, side, signal_available_at,
                         observed_through, first_touch_state, target_hit_at, stop_hit_at,
                         first_touch_modeled_net_pnl_usdt, mfe_r, mae_r,
                         completed_bar_count, horizon_15_complete, horizon_60_complete,
                         horizon_240_complete, final, outcome_json, prospective,
                         operator_review_required, trade_actionable, demo_activation_allowed,
                         live_activation_allowed, bybit_live_order_routing_allowed, created_at)
                        VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         %s, %s, %s, %s, %s, %s::jsonb, true, true, false, false, false,
                         false, %s)
                        ON CONFLICT (seed_id, observed_through) DO NOTHING""",
                        (
                            outcome.evaluation_id,
                            outcome.seed_id,
                            outcome.source_snapshot_id,
                            outcome.source_qualification_state,
                            outcome.symbol,
                            outcome.side,
                            _parse_time(outcome.signal_available_at),
                            _parse_time(outcome.observed_through),
                            outcome.first_touch_state,
                            _optional_time(outcome.target_hit_at),
                            _optional_time(outcome.stop_hit_at),
                            outcome.first_touch_modeled_net_pnl_usdt,
                            outcome.mfe_r,
                            outcome.mae_r,
                            outcome.completed_bar_count,
                            horizons[15].complete,
                            horizons[60].complete,
                            horizons[240].complete,
                            outcome.final,
                            payload_json,
                            moment,
                        ),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """SELECT evaluation_id, outcome_json
                            FROM astra_bybit_shadow_outcome_v112
                            WHERE seed_id=%s AND observed_through=%s""",
                            (outcome.seed_id, _parse_time(outcome.observed_through)),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("crypto shadow outcome idempotency lookup lost row")
                        if row["evaluation_id"] != outcome.evaluation_id:
                            raise ValueError("crypto shadow evaluation identity is divergent")
                        if _canonical_json(row["outcome_json"]) != payload_json:
                            raise ValueError("crypto shadow outcome payload mismatch")
        return outcome.evaluation_id


def _source_from_row(row: Mapping[str, Any]) -> CryptoShadowSourceCandidate:
    return CryptoShadowSourceCandidate(
        source_snapshot_id=str(row["snapshot_id"]),
        evidence_rank=int(row["evidence_rank"]),
        market_rank=int(row["market_rank"]),
        qualification_state=str(row["qualification_state"]),
        symbol=str(row["symbol"]),
        side=str(row["signal_side"]),
        decision_time=_utc(row["decision_time"]).isoformat(),
        signal_quality_score=_decimal(row["signal_quality_score"]),
        planned_notional_usdt=_decimal(row["planned_notional_usdt"]),
        risk_budget_usdt=_decimal(row["risk_budget_usdt"]),
        estimated_round_trip_cost_usdt=_decimal(
            row["estimated_round_trip_cost_usdt"]
        ),
    )


def _seed_from_payload(payload: Any) -> CryptoShadowSeed:
    if not isinstance(payload, Mapping):
        raise ValueError("stored crypto shadow seed JSON is not an object")
    seed = CryptoShadowSeed(
        source_snapshot_id=str(payload.get("source_snapshot_id", "")),
        source_evidence_rank=int(payload.get("source_evidence_rank", 0)),
        source_market_rank=int(payload.get("source_market_rank", 0)),
        source_qualification_state=str(payload.get("source_qualification_state", "")),
        symbol=str(payload.get("symbol", "")),
        side=str(payload.get("side", "")),
        decision_bar_start_at=str(payload.get("decision_bar_start_at", "")),
        signal_available_at=str(payload.get("signal_available_at", "")),
        entry_price=_decimal(payload.get("entry_price")),
        stop_price=_decimal(payload.get("stop_price")),
        target_price=_decimal(payload.get("target_price")),
        planned_notional_usdt=_decimal(payload.get("planned_notional_usdt")),
        risk_budget_usdt=_decimal(payload.get("risk_budget_usdt")),
        estimated_round_trip_cost_usdt=_decimal(
            payload.get("estimated_round_trip_cost_usdt")
        ),
        target_net_profit_usd=_decimal(payload.get("target_net_profit_usd")),
        signal_quality_score=_decimal(payload.get("signal_quality_score")),
        prospective=payload.get("prospective") is True,
        operator_review_required=payload.get("operator_review_required") is True,
        trade_actionable=payload.get("trade_actionable") is True,
        demo_activation_allowed=payload.get("demo_activation_allowed") is True,
        live_activation_allowed=payload.get("live_activation_allowed") is True,
        bybit_live_order_routing_allowed=(
            payload.get("bybit_live_order_routing_allowed") is True
        ),
    )
    expected_id = payload.get("seed_id")
    seed.validate()
    if expected_id is not None and expected_id != seed.seed_id:
        raise ValueError("stored crypto shadow seed id does not match canonical payload")
    return seed


def _decimal(value: Any) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("crypto shadow numeric value is missing")
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("crypto shadow numeric value must be finite")
    return parsed


def _parse_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _optional_time(value: str | None) -> datetime | None:
    return None if value is None else _parse_time(value)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("crypto shadow timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
