from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.strategy.crypto_signal_oos_confirmation import (
    CryptoHistoricalPerfectEvidenceCell,
    CryptoHistoricalPerfectEvidenceSnapshot,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresCryptoHistoricalPerfectEvidenceReader:
    """Read one immutable v111 historical evidence snapshot without mutation."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("signal OOS PostgreSQL DSN is required")
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

    def load_snapshot(self, evidence_snapshot_id: str) -> CryptoHistoricalPerfectEvidenceSnapshot:
        if len(evidence_snapshot_id) != 64 or any(
            char not in "0123456789abcdef" for char in evidence_snapshot_id
        ):
            raise ValueError("signal OOS evidence snapshot id must be lowercase sha256")
        sql = """SELECT
                evidence_snapshot_id,
                observed_at,
                minimum_cell_trades,
                report_json
            FROM astra_bybit_strategy_evidence_snapshot_v111
            WHERE evidence_snapshot_id = %s"""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, (evidence_snapshot_id,))
                rows = cursor.fetchall()
        if len(rows) != 1:
            raise ValueError("signal OOS requires exactly one frozen v111 evidence snapshot")
        row = rows[0]
        report = row.get("report_json")
        if not isinstance(report, Mapping):
            raise ValueError("signal OOS v111 report_json must be an object")
        _validate_report_boundary(report)
        minimum_cell_trades = int(row["minimum_cell_trades"])
        reported_minimum = _required_int(report, "minimum_cell_trades")
        if minimum_cell_trades != reported_minimum:
            raise ValueError("signal OOS v111 minimum cell trade count drifted")
        observed_at = _utc(row["observed_at"]).isoformat()
        candidates = _perfect_candidates(
            report,
            evidence_snapshot_id=evidence_snapshot_id,
            evidence_snapshot_observed_at=observed_at,
        )
        snapshot = CryptoHistoricalPerfectEvidenceSnapshot(
            evidence_snapshot_id=evidence_snapshot_id,
            observed_at=observed_at,
            minimum_cell_trades=minimum_cell_trades,
            candidates=candidates,
        )
        snapshot.validate()
        return snapshot


def _perfect_candidates(
    report: Mapping[str, Any],
    *,
    evidence_snapshot_id: str,
    evidence_snapshot_observed_at: str,
) -> tuple[CryptoHistoricalPerfectEvidenceCell, ...]:
    raw_matrix = report.get("matrix")
    if not isinstance(raw_matrix, list):
        raise ValueError("signal OOS v111 evidence matrix is missing")
    candidates: list[CryptoHistoricalPerfectEvidenceCell] = []
    for raw in raw_matrix:
        if not isinstance(raw, Mapping):
            raise ValueError("signal OOS v111 evidence matrix row must be an object")
        trade_count = _required_int(raw, "trade_count")
        win_count = _required_int(raw, "win_count")
        loss_count = _required_int(raw, "loss_count")
        sample_sufficient = raw.get("sample_sufficient")
        win_rate = _required_decimal(raw, "win_rate")
        total_pnl = _required_decimal(raw, "total_net_pnl_usdt")
        average_pnl = _required_decimal(raw, "average_net_pnl_usdt")
        if sample_sufficient is not True:
            continue
        if trade_count <= 0 or win_count != trade_count or loss_count != 0:
            continue
        if win_rate != Decimal("1") or total_pnl <= 0 or average_pnl <= 0:
            continue
        candidate = CryptoHistoricalPerfectEvidenceCell(
            evidence_snapshot_id=evidence_snapshot_id,
            evidence_snapshot_observed_at=evidence_snapshot_observed_at,
            cell_key=_required_text(raw, "cell_key"),
            symbol=_required_text(raw, "symbol"),
            side=_required_text(raw, "side"),
            market_regime=_required_text(raw, "market_regime"),
            open_interest_regime=_required_text(raw, "open_interest_regime"),
            crowding_regime=_required_text(raw, "crowding_regime"),
            prior_funding_regime=_required_text(raw, "prior_funding_regime"),
            stress_regime=_required_text(raw, "stress_regime"),
            historical_trade_count=trade_count,
            historical_win_rate=win_rate,
            historical_total_net_pnl_usdt=total_pnl,
            historical_average_net_pnl_usdt=average_pnl,
            historical_profit_factor=_optional_decimal(raw.get("profit_factor")),
            historical_average_mfe_r=_optional_decimal(raw.get("average_mfe_r")),
            historical_average_mae_r=_optional_decimal(raw.get("average_mae_r")),
        )
        candidate.validate()
        candidates.append(candidate)
    return tuple(sorted(candidates, key=lambda item: item.semantic_key))


def _validate_report_boundary(report: Mapping[str, Any]) -> None:
    if report.get("diagnostic") != "BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX":
        raise ValueError("signal OOS v111 report diagnostic is unsupported")
    for field in (
        "parameter_retuning_performed",
        "strategy_selection_allowed",
        "strategy_promotion_allowed",
        "demo_activation_allowed",
        "live_activation_allowed",
        "bybit_live_order_routing_allowed",
        "predictive_guarantee_allowed",
    ):
        if report.get(field) is not False:
            raise ValueError(f"signal OOS rejected unsafe or incomplete v111 field:{field}")


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"signal OOS v111 row has invalid {field}")
    return value


def _required_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"signal OOS v111 row has invalid {field}")
    return value


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = _optional_decimal(row.get(field))
    if value is None:
        raise ValueError(f"signal OOS v111 row is missing {field}")
    return value


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("signal OOS decimal cannot be boolean")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("signal OOS decimal is invalid") from exc
    if not parsed.is_finite():
        raise ValueError("signal OOS decimal must be finite")
    return parsed


def _utc(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("signal OOS v111 observed_at must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["PostgresCryptoHistoricalPerfectEvidenceReader"]
