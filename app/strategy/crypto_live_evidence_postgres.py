from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.strategy.crypto_live_evidence_ranking import CryptoLiveOpportunitySnapshot

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresCryptoLiveEvidenceStore:
    """Append-only PostgreSQL store for evidence matrices and ranked opportunities."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("crypto live evidence PostgreSQL DSN is required")
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
        path: str | Path = "migrations/v111/001_bybit_live_evidence_registry.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def persist_evidence_report(
        self,
        report: Mapping[str, Any],
        *,
        observed_at: datetime,
        created_at: datetime | None = None,
    ) -> str:
        _validate_evidence_report(report)
        evidence_id = evidence_report_id(report)
        moment = datetime.now(UTC) if created_at is None else _utc(created_at)
        evidence_time = _utc(observed_at)
        report_json = _canonical_json(report)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_strategy_evidence_snapshot_v111
                        (evidence_snapshot_id, observed_at, trade_count, cell_count,
                         minimum_cell_trades, turnover_reference_usdt, report_json,
                         parameter_retuning_performed, strategy_selection_allowed,
                         strategy_promotion_allowed, demo_activation_allowed,
                         live_activation_allowed, bybit_live_order_routing_allowed, created_at)
                        VALUES
                        (%s, %s, %s, %s, %s, %s, %s::jsonb,
                         false, false, false, false, false, false, %s)
                        ON CONFLICT (evidence_snapshot_id) DO NOTHING""",
                        (
                            evidence_id,
                            evidence_time,
                            _required_int(report, "trade_count"),
                            _required_int(report, "cell_count"),
                            _required_int(report, "minimum_cell_trades"),
                            _required_decimal(report, "turnover_reference_usdt"),
                            report_json,
                            moment,
                        ),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """SELECT report_json
                            FROM astra_bybit_strategy_evidence_snapshot_v111
                            WHERE evidence_snapshot_id=%s""",
                            (evidence_id,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("crypto evidence idempotency lookup lost report")
                        if _canonical_json(row["report_json"]) != report_json:
                            raise ValueError("crypto evidence snapshot id payload mismatch")
        return evidence_id

    def latest_evidence_report(self) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT report_json
                    FROM astra_bybit_strategy_evidence_snapshot_v111
                    ORDER BY observed_at DESC, evidence_snapshot_id DESC
                    LIMIT 1"""
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                report = row["report_json"]
                if not isinstance(report, Mapping):
                    raise ValueError("stored crypto evidence report_json is not an object")
                _validate_evidence_report(report)
                return dict(report)

    def persist_opportunity_snapshot(
        self,
        snapshot: CryptoLiveOpportunitySnapshot,
        *,
        created_at: datetime | None = None,
    ) -> str:
        snapshot.validate()
        moment = datetime.now(UTC) if created_at is None else _utc(created_at)
        observed_at = datetime.fromtimestamp(snapshot.observed_at_ms / 1000, tz=UTC)
        payload = snapshot.to_payload()
        payload_json = _canonical_json(payload)
        snapshot_id = snapshot.snapshot_id
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_live_opportunity_snapshot_v111
                        (snapshot_id, observed_at, observed_at_ms, market_snapshot_id,
                         evidence_snapshot_id, equity_usdt, equity_source,
                         qualified_positive_count, qualified_mixed_count, snapshot_json,
                         operator_review_required, trade_actionable,
                         strategy_parameters_changed, strategy_promotion_allowed,
                         demo_activation_allowed, live_activation_allowed,
                         bybit_live_order_routing_allowed, created_at)
                        VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                         true, false, false, false, false, false, false, %s)
                        ON CONFLICT (snapshot_id) DO NOTHING""",
                        (
                            snapshot_id,
                            observed_at,
                            snapshot.observed_at_ms,
                            snapshot.market_snapshot_id,
                            snapshot.evidence_snapshot_id,
                            snapshot.equity_usdt,
                            snapshot.equity_source,
                            snapshot.qualified_positive_count,
                            snapshot.qualified_mixed_count,
                            payload_json,
                            moment,
                        ),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """SELECT snapshot_json
                            FROM astra_bybit_live_opportunity_snapshot_v111
                            WHERE snapshot_id=%s""",
                            (snapshot_id,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError("live opportunity idempotency lookup lost snapshot")
                        if _canonical_json(row["snapshot_json"]) != payload_json:
                            raise ValueError("live opportunity snapshot id payload mismatch")
                        return snapshot_id
                    for item in snapshot.opportunities:
                        cursor.execute(
                            """INSERT INTO astra_bybit_live_opportunity_candidate_v111
                            (snapshot_id, evidence_rank, market_rank, symbol,
                             qualification_state, qualification_reasons, signal_side,
                             decision_time, market_universe_score, signal_quality_score,
                             current_market_regime, current_open_interest_regime,
                             current_crowding_regime, current_prior_funding_regime,
                             current_stress_regime, current_stress_score,
                             expected_net_edge_usd, planned_notional_usdt, risk_budget_usdt,
                             estimated_round_trip_cost_usdt, evidence_cell_key,
                             evidence_trade_count, evidence_sample_sufficient,
                             evidence_profit_factor, evidence_win_rate,
                             evidence_total_net_pnl_usdt, evidence_average_net_pnl_usdt,
                             evidence_average_mfe_r, evidence_average_mae_r,
                             evidence_drawdown_usdt, positive_historical_evidence,
                             operator_review_required, trade_actionable,
                             strategy_promotion_allowed, demo_activation_allowed,
                             live_activation_allowed, bybit_live_order_routing_allowed)
                            VALUES
                            (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s,
                             %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                             %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                             true, false, false, false, false, false)""",
                            (
                                snapshot_id,
                                item.evidence_rank,
                                item.market_rank,
                                item.symbol,
                                item.qualification_state,
                                _canonical_json(list(item.qualification_reasons)),
                                item.signal_side,
                                (
                                    None
                                    if item.decision_time is None
                                    else datetime.fromisoformat(item.decision_time)
                                ),
                                item.market_universe_score,
                                item.signal_quality_score,
                                item.current_market_regime,
                                item.current_open_interest_regime,
                                item.current_crowding_regime,
                                item.current_prior_funding_regime,
                                item.current_stress_regime,
                                item.current_stress_score,
                                item.expected_net_edge_usd,
                                item.planned_notional_usdt,
                                item.risk_budget_usdt,
                                item.estimated_round_trip_cost_usdt,
                                item.evidence_cell_key,
                                item.evidence_trade_count,
                                item.evidence_sample_sufficient,
                                item.evidence_profit_factor,
                                item.evidence_win_rate,
                                item.evidence_total_net_pnl_usdt,
                                item.evidence_average_net_pnl_usdt,
                                item.evidence_average_mfe_r,
                                item.evidence_average_mae_r,
                                item.evidence_drawdown_usdt,
                                item.positive_historical_evidence,
                            ),
                        )
        return snapshot_id


def evidence_report_id(report: Mapping[str, Any]) -> str:
    _validate_evidence_report(report)
    canonical = _canonical_json(report).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def extract_evidence_report(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], datetime | None]:
    """Accept either a matrix payload or the canonical dynamic Top-10 research artifact."""

    if payload.get("diagnostic") == "BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX":
        _validate_evidence_report(payload)
        return payload, None
    nested = payload.get("strategy_evidence_matrix")
    if not isinstance(nested, Mapping):
        raise ValueError("crypto evidence payload does not contain strategy_evidence_matrix")
    _validate_evidence_report(nested)
    observed_raw = payload.get("observed_at")
    if not isinstance(observed_raw, str):
        raise ValueError("dynamic Top-10 research artifact observed_at is missing")
    observed_at = datetime.fromisoformat(observed_raw)
    return nested, _utc(observed_at)


def _validate_evidence_report(report: Mapping[str, Any]) -> None:
    if report.get("diagnostic") != "BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX":
        raise ValueError("crypto evidence report diagnostic is invalid")
    matrix = report.get("matrix")
    if not isinstance(matrix, list):
        raise ValueError("crypto evidence report matrix is missing")
    _required_int(report, "trade_count")
    _required_int(report, "cell_count")
    minimum = _required_int(report, "minimum_cell_trades")
    if minimum <= 0:
        raise ValueError("crypto evidence minimum cell trades must be positive")
    turnover = _required_decimal(report, "turnover_reference_usdt")
    if turnover < 0:
        raise ValueError("crypto evidence turnover reference cannot be negative")
    for field in (
        "parameter_retuning_performed",
        "strategy_selection_allowed",
        "strategy_promotion_allowed",
        "demo_activation_allowed",
        "live_activation_allowed",
        "bybit_live_order_routing_allowed",
        "causal_claim_allowed",
        "predictive_guarantee_allowed",
    ):
        if report.get(field) is not False:
            raise ValueError(f"crypto evidence unsafe report flag:{field}")


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise ValueError(f"crypto evidence missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"crypto evidence invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"crypto evidence non-finite {field}")
    return parsed


def _required_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise ValueError(f"crypto evidence missing {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"crypto evidence invalid {field}") from exc
    if parsed < 0:
        raise ValueError(f"crypto evidence negative {field}")
    return parsed


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("crypto evidence timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
