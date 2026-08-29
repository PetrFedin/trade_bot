from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.validate_bybit_signal_pattern_holdout import validate_holdout_directory

_PATTERN = "LONG|VOL_LOW_NORMAL|TREND_MODERATE|BREAKOUT_PULLBACK|TURNOVER_LOW"
_SOURCE = "BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M"
_STRATEGY = "CONDITIONAL_1_5X_OPEN_ENDED_RUNNER"


def _trade_row(symbol: str, *, index: int, date_prefix: str) -> dict[str, object]:
    return {
        "pattern": _PATTERN,
        "symbol": symbol,
        "side": "LONG",
        "decision_time": f"{date_prefix}T{index:02d}:00:00+00:00",
        "positive_close": True,
        "planned_profit_exit": True,
        "net_pnl_usdt": 20.0,
        "exit_reason": "NET_TARGET",
    }


def _report(
    symbol: str,
    *,
    archive_dates: list[str],
    rows: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "audit": "BYBIT_CRYPTO_SINGLE_SYMBOL_CURRENT_QUALIFIED_AUDIT_V1",
        "symbol": symbol,
        "source": _SOURCE,
        "strategy_mode": _STRATEGY,
        "archive_dates": archive_dates,
        "all_eligible_signal_events": {"signal_event_count": 10},
        "trade_outcomes": {"trade_rows": rows},
        "strategy_promotion_allowed": False,
        "trade_actionable": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _write(
    root: Path,
    symbol: str,
    phase: str,
    payload: dict[str, object],
) -> None:
    path = root / symbol / phase / "report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _source_tree(root: Path, *, holdout_loss: bool = False) -> None:
    discovery_dates = [f"2026-08-{day:02d}" for day in range(15, 22)]
    holdout_dates = [f"2026-08-{day:02d}" for day in range(22, 29)]
    discovery_rows = {
        "BTCUSDT": [
            _trade_row("BTCUSDT", index=index, date_prefix="2026-08-20")
            for index in range(3)
        ],
        "ETHUSDT": [
            _trade_row("ETHUSDT", index=index, date_prefix="2026-08-21")
            for index in range(2)
        ],
    }
    holdout_rows = {
        "BTCUSDT": [
            _trade_row("BTCUSDT", index=index, date_prefix="2026-08-27")
            for index in range(3)
        ],
        "ETHUSDT": [
            _trade_row("ETHUSDT", index=index, date_prefix="2026-08-28")
            for index in range(2)
        ],
    }
    if holdout_loss:
        holdout_rows["ETHUSDT"][-1].update(
            positive_close=False,
            planned_profit_exit=False,
            net_pnl_usdt=-10.0,
            exit_reason="HARD_STOP",
        )
    for symbol in ("BTCUSDT", "ETHUSDT"):
        _write(
            root,
            symbol,
            "discovery",
            _report(
                symbol,
                archive_dates=discovery_dates,
                rows=discovery_rows[symbol],
            ),
        )
        _write(
            root,
            symbol,
            "holdout",
            _report(
                symbol,
                archive_dates=holdout_dates,
                rows=holdout_rows[symbol],
            ),
        )


def test_aggregator_validates_strict_non_overlapping_holdout(tmp_path: Path) -> None:
    _source_tree(tmp_path)
    report = validate_holdout_directory(
        tmp_path,
        symbols=("BTCUSDT", "ETHUSDT"),
    )

    assert report["discovery_archive_dates"] == [
        f"2026-08-{day:02d}" for day in range(15, 22)
    ]
    assert report["holdout_archive_dates"] == [
        f"2026-08-{day:02d}" for day in range(22, 29)
    ]
    assert report["candidate_count"] == 1
    assert report["observed_holdout_perfect_count"] == 1
    assert report["strategy_promotion_allowed"] is False


def test_aggregator_surfaces_holdout_break(tmp_path: Path) -> None:
    _source_tree(tmp_path, holdout_loss=True)
    report = validate_holdout_directory(
        tmp_path,
        symbols=("BTCUSDT", "ETHUSDT"),
    )

    assert report["observed_holdout_perfect_count"] == 0
    assert report["candidates"][0]["status"] == "HOLDOUT_BROKE_PERFECT_HISTORY"


def test_aggregator_rejects_overlapping_source_windows(tmp_path: Path) -> None:
    _source_tree(tmp_path)
    path = tmp_path / "BTCUSDT" / "holdout" / "report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["archive_dates"] = [f"2026-08-{day:02d}" for day in range(21, 28)]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="overlap"):
        validate_holdout_directory(
            tmp_path,
            symbols=("BTCUSDT", "ETHUSDT"),
        )
