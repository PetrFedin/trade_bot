from __future__ import annotations

from collections.abc import Mapping
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresCryptoLiveOpportunityReader:
    """Read the latest ranked review queue without exposing any trading mutation surface."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("crypto live opportunity PostgreSQL DSN is required")
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
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=True)

    def latest_snapshot_payload(self) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT snapshot_json
                    FROM astra_bybit_live_opportunity_snapshot_v111
                    ORDER BY observed_at DESC, snapshot_id DESC
                    LIMIT 1"""
                )
                row = cursor.fetchone()
        if row is None:
            return None
        payload = row["snapshot_json"]
        if not isinstance(payload, Mapping):
            raise ValueError("stored live opportunity snapshot_json is not an object")
        _validate_snapshot_payload(payload)
        return dict(payload)

    def latest_review_queue(
        self,
        *,
        limit: int = 10,
        include_mixed: bool = False,
    ) -> tuple[Mapping[str, Any], ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ValueError("live opportunity review queue limit must be within [1, 50]")
        states = (
            ("QUALIFIED_POSITIVE_EVIDENCE", "QUALIFIED_MIXED_EVIDENCE")
            if include_mixed
            else ("QUALIFIED_POSITIVE_EVIDENCE",)
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """WITH latest AS (
                        SELECT snapshot_id
                        FROM astra_bybit_live_opportunity_snapshot_v111
                        ORDER BY observed_at DESC, snapshot_id DESC
                        LIMIT 1
                    )
                    SELECT
                        c.snapshot_id,
                        c.evidence_rank,
                        c.market_rank,
                        c.symbol,
                        c.qualification_state,
                        c.qualification_reasons,
                        c.signal_side,
                        c.decision_time,
                        c.market_universe_score,
                        c.signal_quality_score,
                        c.current_market_regime,
                        c.current_open_interest_regime,
                        c.current_crowding_regime,
                        c.current_prior_funding_regime,
                        c.current_stress_regime,
                        c.current_stress_score,
                        c.expected_net_edge_usd,
                        c.planned_notional_usdt,
                        c.risk_budget_usdt,
                        c.estimated_round_trip_cost_usdt,
                        c.evidence_cell_key,
                        c.evidence_trade_count,
                        c.evidence_sample_sufficient,
                        c.evidence_profit_factor,
                        c.evidence_win_rate,
                        c.evidence_total_net_pnl_usdt,
                        c.evidence_average_net_pnl_usdt,
                        c.evidence_average_mfe_r,
                        c.evidence_average_mae_r,
                        c.evidence_drawdown_usdt,
                        c.positive_historical_evidence,
                        c.operator_review_required,
                        c.trade_actionable,
                        c.strategy_promotion_allowed,
                        c.demo_activation_allowed,
                        c.live_activation_allowed,
                        c.bybit_live_order_routing_allowed
                    FROM astra_bybit_live_opportunity_candidate_v111 c
                    JOIN latest l ON l.snapshot_id = c.snapshot_id
                    WHERE c.qualification_state = ANY(%s)
                    ORDER BY c.evidence_rank ASC
                    LIMIT %s""",
                    (list(states), limit),
                )
                rows = cursor.fetchall()
        result: list[Mapping[str, Any]] = []
        for row in rows:
            _validate_review_row(row)
            result.append(dict(row))
        return tuple(result)


def _validate_snapshot_payload(payload: Mapping[str, Any]) -> None:
    for field, expected in (
        ("operator_review_required", True),
        ("trade_actionable", False),
        ("strategy_parameters_changed", False),
        ("strategy_promotion_allowed", False),
        ("demo_activation_allowed", False),
        ("live_activation_allowed", False),
        ("bybit_live_order_routing_allowed", False),
        ("causal_claim_allowed", False),
        ("predictive_guarantee_allowed", False),
    ):
        if payload.get(field) is not expected:
            raise ValueError(f"stored live opportunity snapshot violates safety flag:{field}")


def _validate_review_row(row: Mapping[str, Any]) -> None:
    if row.get("qualification_state") not in {
        "QUALIFIED_POSITIVE_EVIDENCE",
        "QUALIFIED_MIXED_EVIDENCE",
    }:
        raise ValueError("live opportunity review queue contains unqualified state")
    if row.get("operator_review_required") is not True:
        raise ValueError("live opportunity review queue lost operator-review boundary")
    for field in (
        "trade_actionable",
        "strategy_promotion_allowed",
        "demo_activation_allowed",
        "live_activation_allowed",
        "bybit_live_order_routing_allowed",
    ):
        if row.get(field) is not False:
            raise ValueError(f"live opportunity review queue violates safety flag:{field}")
