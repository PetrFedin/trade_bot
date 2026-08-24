from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresCryptoProspectiveEvidenceStore:
    """Append-only store for materialized prospective exact-cell evidence reports."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("prospective evidence PostgreSQL DSN is required")
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
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    def migrate(
        self,
        path: str | Path = (
            "migrations/v118/001_bybit_prospective_evidence_materialization.sql"
        ),
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def persist(
        self,
        report: Mapping[str, Any],
        *,
        created_at: datetime | None = None,
    ) -> str:
        _validate_report(report)
        report_id = prospective_evidence_report_id(report)
        generated_at = _parse_time(str(report["report_generated_at"]))
        lineage = report["source_lineage"]
        if not isinstance(lineage, Mapping):
            raise ValueError("prospective evidence source_lineage must be an object")
        moment = datetime.now(UTC) if created_at is None else _utc(created_at)
        payload_json = _canonical_json(report)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_prospective_exact_cell_report_v118
                        (report_id, report_generated_at, report_window,
                         source_observation_count, source_seed_set_sha256,
                         earliest_signal_available_at, latest_signal_available_at,
                         source_cell_complete_count, source_cell_unavailable_count,
                         liquidation_coverage_qualified_count, report_json,
                         source_lineage_complete, research_only, trade_actionable,
                         strategy_promotion_allowed, demo_activation_allowed,
                         live_activation_allowed, bybit_live_order_routing_allowed,
                         created_at)
                        VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                         true, true, false, false, false, false, false, %s)
                        ON CONFLICT (report_id) DO NOTHING""",
                        (
                            report_id,
                            generated_at,
                            str(report["report_window"]),
                            _required_int(lineage, "source_observation_count"),
                            _required_hash(lineage, "source_seed_set_sha256"),
                            _optional_time(lineage.get("earliest_signal_available_at")),
                            _optional_time(lineage.get("latest_signal_available_at")),
                            _required_int(lineage, "source_cell_complete_count"),
                            _required_int(lineage, "source_cell_unavailable_count"),
                            _required_int(
                                lineage,
                                "liquidation_coverage_qualified_count",
                            ),
                            payload_json,
                            moment,
                        ),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """SELECT report_json
                            FROM astra_bybit_prospective_exact_cell_report_v118
                            WHERE report_id=%s""",
                            (report_id,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError(
                                "prospective evidence idempotency lookup lost report"
                            )
                        if _canonical_json(row["report_json"]) != payload_json:
                            raise ValueError(
                                "prospective evidence report id payload mismatch"
                            )
        return report_id


def prospective_evidence_report_id(report: Mapping[str, Any]) -> str:
    _validate_report(report)
    return hashlib.sha256(_canonical_json(report).encode("utf-8")).hexdigest()


def _validate_report(report: Mapping[str, Any]) -> None:
    if report.get("diagnostic") != "BYBIT_PROSPECTIVE_EXACT_EVIDENCE_CELL_MATRIX":
        raise ValueError("prospective evidence report diagnostic is invalid")
    generated = report.get("report_generated_at")
    if not isinstance(generated, str):
        raise ValueError("prospective evidence report_generated_at is missing")
    _parse_time(generated)
    window = report.get("report_window")
    if not isinstance(window, str) or not window.strip():
        raise ValueError("prospective evidence report_window is missing")
    lineage = report.get("source_lineage")
    if not isinstance(lineage, Mapping):
        raise ValueError("prospective evidence source_lineage is missing")
    observation_count = _required_int(lineage, "source_observation_count")
    complete = _required_int(lineage, "source_cell_complete_count")
    unavailable = _required_int(lineage, "source_cell_unavailable_count")
    liquidation = _required_int(lineage, "liquidation_coverage_qualified_count")
    _required_hash(lineage, "source_seed_set_sha256")
    if complete + unavailable != observation_count:
        raise ValueError("prospective evidence lineage cell counts do not reconcile")
    if liquidation > observation_count:
        raise ValueError("prospective evidence liquidation count exceeds observations")
    earliest = _optional_time(lineage.get("earliest_signal_available_at"))
    latest = _optional_time(lineage.get("latest_signal_available_at"))
    if observation_count == 0:
        if earliest is not None or latest is not None:
            raise ValueError("empty prospective evidence lineage cannot carry signal times")
    elif earliest is None or latest is None or earliest > latest:
        raise ValueError("prospective evidence signal watermarks are inconsistent")
    if lineage.get("source_lineage_complete") is not True:
        raise ValueError("prospective evidence source lineage must be complete")
    for field in (
        "trade_actionable",
        "strategy_promotion_allowed",
        "demo_activation_allowed",
        "live_activation_allowed",
        "bybit_live_order_routing_allowed",
    ):
        if report.get(field) is not False:
            raise ValueError(f"prospective evidence unsafe report flag:{field}")
        if lineage.get(field) is not False:
            raise ValueError(f"prospective evidence unsafe lineage flag:{field}")


def _required_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise ValueError(f"prospective evidence missing {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"prospective evidence invalid {field}") from exc
    if parsed < 0:
        raise ValueError(f"prospective evidence negative {field}")
    return parsed


def _required_hash(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str):
        raise ValueError(f"prospective evidence missing {field}")
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"prospective evidence invalid {field}")
    return value


def _optional_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("prospective evidence signal timestamp must be a string")
    return _parse_time(value)


def _parse_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("prospective evidence timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


__all__ = [
    "PostgresCryptoProspectiveEvidenceStore",
    "prospective_evidence_report_id",
]
