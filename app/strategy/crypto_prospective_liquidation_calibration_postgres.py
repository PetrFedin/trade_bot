from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.strategy.crypto_prospective_calibration_postgres import (
    PostgresCryptoProspectiveCalibrationReader,
)
from app.strategy.crypto_prospective_liquidation_calibration import (
    CryptoLiquidationCalibrationWindow,
    CryptoProspectiveLiquidationCalibrationDataset,
    CryptoProspectiveLiquidationCalibrationObservation,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresCryptoProspectiveLiquidationCalibrationReader:
    """Read-only loader retaining every final prospective base observation."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("liquidation calibration PostgreSQL DSN is required")
        self._dsn = dsn
        self._base_reader = PostgresCryptoProspectiveCalibrationReader(dsn)

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
    ) -> CryptoProspectiveLiquidationCalibrationDataset:
        base = self._base_reader.load_dataset(
            signal_available_at_or_after=signal_available_at_or_after,
            maximum_final_seeds=maximum_final_seeds,
        )
        if not base.observations:
            dataset = CryptoProspectiveLiquidationCalibrationDataset(
                base_dataset=base,
                observations=(),
            )
            dataset.validate()
            return dataset
        seed_ids = [item.seed_id for item in base.observations]
        headers, windows = self._load_context_rows(seed_ids)
        observations: list[CryptoProspectiveLiquidationCalibrationObservation] = []
        for item in base.observations:
            header = headers.get(item.seed_id)
            if header is None:
                observations.append(
                    CryptoProspectiveLiquidationCalibrationObservation(
                        base=item,
                        context_state="NOT_MATERIALIZED",
                        coverage_reason_codes=(),
                        windows=(),
                    )
                )
                continue
            _validate_header_identity(header, item)
            qualified = bool(header["coverage_qualified"])
            reasons = _reason_codes(header.get("coverage_reason_codes"))
            if not qualified:
                observations.append(
                    CryptoProspectiveLiquidationCalibrationObservation(
                        base=item,
                        context_state="COVERAGE_UNQUALIFIED",
                        coverage_reason_codes=reasons,
                        windows=(),
                    )
                )
                continue
            seed_windows = tuple(
                _window_from_row(row)
                for row in sorted(
                    windows.get(item.seed_id, ()),
                    key=lambda row: int(row["window_minutes"]),
                )
            )
            observations.append(
                CryptoProspectiveLiquidationCalibrationObservation(
                    base=item,
                    context_state="COVERAGE_QUALIFIED",
                    coverage_reason_codes=(),
                    windows=seed_windows,
                )
            )
        dataset = CryptoProspectiveLiquidationCalibrationDataset(
            base_dataset=base,
            observations=tuple(observations),
        )
        dataset.validate()
        return dataset

    def _load_context_rows(
        self,
        seed_ids: list[str],
    ) -> tuple[dict[str, Mapping[str, Any]], dict[str, tuple[Mapping[str, Any], ...]]]:
        headers_sql = """SELECT seed_id, symbol, side, signal_available_at,
                   coverage_qualified, coverage_reason_codes
            FROM astra_bybit_shadow_liquidation_context_v117
            WHERE seed_id = ANY(%s::text[])"""
        windows_sql = """SELECT context.seed_id, liq_window.window_minutes,
                   liq_window.event_count, liq_window.long_liquidation_count,
                   liq_window.short_liquidation_count,
                   liq_window.long_estimated_notional_usdt,
                   liq_window.short_estimated_notional_usdt,
                   liq_window.total_estimated_notional_usdt,
                   liq_window.long_minus_short_estimated_notional_usdt,
                   liq_window.normalized_long_minus_short_imbalance,
                   liq_window.largest_event_estimated_notional_usdt,
                   liq_window.known_zero
            FROM astra_bybit_shadow_liquidation_context_v117 AS context
            JOIN astra_bybit_shadow_liquidation_window_v117 AS liq_window
              ON liq_window.context_id = context.context_id
            WHERE context.seed_id = ANY(%s::text[])
            ORDER BY context.seed_id, liq_window.window_minutes"""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(headers_sql, (seed_ids,))
                header_rows = cursor.fetchall()
                cursor.execute(windows_sql, (seed_ids,))
                window_rows = cursor.fetchall()
        headers: dict[str, Mapping[str, Any]] = {}
        for row in header_rows:
            seed_id = str(row["seed_id"])
            if seed_id in headers:
                raise RuntimeError("liquidation calibration found duplicate context header")
            headers[seed_id] = row
        grouped_windows: dict[str, list[Mapping[str, Any]]] = {}
        for row in window_rows:
            grouped_windows.setdefault(str(row["seed_id"]), []).append(row)
        return headers, {
            seed_id: tuple(rows) for seed_id, rows in grouped_windows.items()
        }


def _validate_header_identity(header: Mapping[str, Any], base: Any) -> None:
    if str(header["symbol"]) != base.symbol or str(header["side"]) != base.side:
        raise ValueError("liquidation calibration context market identity mismatch")
    context_signal = _utc(header["signal_available_at"])
    base_signal = _parse_iso_time(base.signal_available_at)
    if context_signal != base_signal:
        raise ValueError("liquidation calibration signal timestamp mismatch")


def _window_from_row(row: Mapping[str, Any]) -> CryptoLiquidationCalibrationWindow:
    required = (
        "event_count",
        "long_liquidation_count",
        "short_liquidation_count",
        "long_estimated_notional_usdt",
        "short_estimated_notional_usdt",
        "total_estimated_notional_usdt",
        "long_minus_short_estimated_notional_usdt",
        "normalized_long_minus_short_imbalance",
        "largest_event_estimated_notional_usdt",
    )
    if any(row.get(field) is None for field in required):
        raise ValueError("qualified liquidation calibration context has NULL metrics")
    window = CryptoLiquidationCalibrationWindow(
        window_minutes=_integer(row["window_minutes"], "window_minutes"),
        event_count=_integer(row["event_count"], "event_count"),
        long_liquidation_count=_integer(
            row["long_liquidation_count"], "long_liquidation_count"
        ),
        short_liquidation_count=_integer(
            row["short_liquidation_count"], "short_liquidation_count"
        ),
        long_estimated_notional_usdt=_decimal(
            row["long_estimated_notional_usdt"], "long_estimated_notional_usdt"
        ),
        short_estimated_notional_usdt=_decimal(
            row["short_estimated_notional_usdt"], "short_estimated_notional_usdt"
        ),
        total_estimated_notional_usdt=_decimal(
            row["total_estimated_notional_usdt"], "total_estimated_notional_usdt"
        ),
        signed_long_minus_short_notional_usdt=_decimal(
            row["long_minus_short_estimated_notional_usdt"],
            "long_minus_short_estimated_notional_usdt",
        ),
        normalized_long_minus_short_imbalance=_decimal(
            row["normalized_long_minus_short_imbalance"],
            "normalized_long_minus_short_imbalance",
        ),
        largest_event_estimated_notional_usdt=_decimal(
            row["largest_event_estimated_notional_usdt"],
            "largest_event_estimated_notional_usdt",
        ),
        known_zero=bool(row["known_zero"]),
    )
    window.validate()
    return window


def _reason_codes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("liquidation calibration coverage reasons must be a JSON string list")
    return tuple(value)


def _decimal(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"liquidation calibration {field} is missing")
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError(f"liquidation calibration {field} must be finite")
    return parsed


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"liquidation calibration {field} is invalid")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"liquidation calibration {field} is invalid") from exc


def _parse_iso_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("liquidation calibration base signal timestamp is invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("liquidation calibration base signal timestamp is invalid") from exc
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("liquidation calibration timestamp must be timezone-aware")
    return value.astimezone(UTC)


__all__ = ["PostgresCryptoProspectiveLiquidationCalibrationReader"]
