from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.strategy.crypto_prospective_calibration_postgres import (
    PostgresCryptoProspectiveCalibrationReader,
)

_START = datetime(2026, 8, 1, tzinfo=UTC)


def _outcome_payload(
    *,
    seed_id: str,
    symbol: str,
    side: str,
    signal_available_at: datetime,
    state: str,
    pnl: Decimal,
) -> dict[str, Any]:
    return {
        "schema": "BYBIT_PROSPECTIVE_SHADOW_OUTCOME_V112",
        "seed_id": seed_id,
        "source_snapshot_id": "a" * 64,
        "source_qualification_state": state,
        "symbol": symbol,
        "side": side,
        "signal_available_at": signal_available_at.isoformat(),
        "observed_through": (signal_available_at + timedelta(minutes=240)).isoformat(),
        "first_touch_state": "TARGET_FIRST" if pnl > 0 else "STOP_FIRST",
        "target_hit_at": (
            (signal_available_at + timedelta(minutes=15)).isoformat() if pnl > 0 else None
        ),
        "stop_hit_at": (
            (signal_available_at + timedelta(minutes=15)).isoformat() if pnl < 0 else None
        ),
        "first_touch_modeled_net_pnl_usdt": str(pnl),
        "mfe_r": "1.2",
        "mae_r": "-0.6",
        "completed_bar_count": 48,
        "horizons": [
            {
                "horizon_minutes": horizon,
                "complete": True,
                "close_time": (signal_available_at + timedelta(minutes=horizon)).isoformat(),
                "close_price": "101",
                "directional_return_fraction": "0.01" if pnl > 0 else "-0.01",
                "gross_pnl_usdt": str(pnl + Decimal("0.5")),
                "modeled_net_pnl_usdt": str(pnl),
            }
            for horizon in (15, 60, 240)
        ],
        "final": True,
        "prospective": True,
        "operator_review_required": True,
        "trade_actionable": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
        "evaluation_id": "b" * 64,
    }


def _row(
    index: int,
    *,
    symbol: str,
    signal_available_at: datetime,
    state: str,
    pnl: Decimal,
    created_offset_minutes: int,
) -> dict[str, Any]:
    seed_id = f"{index + 1:064x}"
    side = "LONG"
    return {
        "seed_id": seed_id,
        "source_evidence_rank": index + 1,
        "source_market_rank": index + 1,
        "source_qualification_state": state,
        "symbol": symbol,
        "side": side,
        "signal_available_at": signal_available_at,
        "signal_quality_score": Decimal("1.5"),
        "seed_created_at": signal_available_at + timedelta(minutes=created_offset_minutes),
        "outcome_json": _outcome_payload(
            seed_id=seed_id,
            symbol=symbol,
            side=side,
            signal_available_at=signal_available_at,
            state=state,
            pnl=pnl,
        ),
    }


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.mode = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, query: str, _params: tuple[Any, ...]) -> None:
        self.mode = "count" if "count(*)" in query else "rows"

    def fetchone(self):
        if self.mode != "count":
            raise AssertionError("unexpected fetchone mode")
        return {"final_seed_count": len(self.rows)}

    def fetchall(self):
        if self.mode != "rows":
            raise AssertionError("unexpected fetchall mode")
        return list(self.rows)


class _Connection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return _Cursor(self.rows)


class _Reader(PostgresCryptoProspectiveCalibrationReader):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__("postgresql://placeholder")
        self.rows = rows

    def _connect(self):
        return _Connection(self.rows)


def test_reader_deduplicates_repeated_snapshot_of_same_signal_identity() -> None:
    first_signal = _START
    second_signal = _START + timedelta(hours=1)
    rows = [
        _row(
            0,
            symbol="BTCUSDT",
            signal_available_at=first_signal,
            state="QUALIFIED_POSITIVE_EVIDENCE",
            pnl=Decimal("10"),
            created_offset_minutes=0,
        ),
        _row(
            1,
            symbol="BTCUSDT",
            signal_available_at=first_signal,
            state="QUALIFIED_MIXED_EVIDENCE",
            pnl=Decimal("10"),
            created_offset_minutes=10,
        ),
        _row(
            2,
            symbol="ETHUSDT",
            signal_available_at=second_signal,
            state="QUALIFIED_MIXED_EVIDENCE",
            pnl=Decimal("-5"),
            created_offset_minutes=0,
        ),
    ]
    dataset = _Reader(rows).load_dataset(maximum_final_seeds=10)

    assert dataset.raw_final_seed_count == 3
    assert dataset.deduplicated_observation_count == 2
    assert dataset.duplicate_signal_observation_count == 1
    assert dataset.observations[0].seed_id == f"{1:064x}"
    assert dataset.observations[0].qualification_state == "QUALIFIED_POSITIVE_EVIDENCE"
    assert dataset.observations[1].symbol == "ETHUSDT"


def test_reader_has_no_order_write_surface() -> None:
    reader = PostgresCryptoProspectiveCalibrationReader("postgresql://placeholder")
    assert reader.order_writes_supported is False
    assert reader.live_mainnet_order_routing_allowed is False
    for method in ("create_order", "amend_order", "cancel_order", "place_order"):
        assert not hasattr(reader, method)
