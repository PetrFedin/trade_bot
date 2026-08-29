from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.strategy.crypto_signal_pattern_holdout import (
    CryptoSignalPatternHoldoutPolicy,
    validate_crypto_signal_pattern_holdout,
)

_DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "ADAUSDT",
)
_EXPECTED_SOURCE = "BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M"
_EXPECTED_STRATEGY_MODE = "CONDITIONAL_1_5X_OPEN_ENDED_RUNNER"


def validate_holdout_directory(
    root: Path,
    *,
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS,
    policy: CryptoSignalPatternHoldoutPolicy | None = None,
) -> dict[str, Any]:
    normalized = tuple(symbol.strip().upper() for symbol in symbols)
    if not normalized or len(set(normalized)) != len(normalized):
        raise ValueError("signal holdout symbols must be unique and non-empty")

    discovery_rows: list[Mapping[str, Any]] = []
    holdout_rows: list[Mapping[str, Any]] = []
    source_windows: dict[str, dict[str, Any]] = {}
    discovery_dates: set[str] = set()
    holdout_dates: set[str] = set()

    for symbol in normalized:
        discovery = _read_report(root / symbol / "discovery" / "report.json")
        holdout = _read_report(root / symbol / "holdout" / "report.json")
        _validate_source_report(discovery, symbol=symbol)
        _validate_source_report(holdout, symbol=symbol)

        discovery_archive_dates = _archive_dates(discovery)
        holdout_archive_dates = _archive_dates(holdout)
        if set(discovery_archive_dates) & set(holdout_archive_dates):
            raise ValueError("signal holdout discovery and holdout dates overlap")
        if max(discovery_archive_dates) >= min(holdout_archive_dates):
            raise ValueError("signal holdout windows are not strictly chronological")
        discovery_dates.update(discovery_archive_dates)
        holdout_dates.update(holdout_archive_dates)

        discovery_trade_rows = _trade_rows(discovery)
        holdout_trade_rows = _trade_rows(holdout)
        discovery_rows.extend(discovery_trade_rows)
        holdout_rows.extend(holdout_trade_rows)
        source_windows[symbol] = {
            "discovery_archive_dates": discovery_archive_dates,
            "holdout_archive_dates": holdout_archive_dates,
            "discovery_trade_count": len(discovery_trade_rows),
            "holdout_trade_count": len(holdout_trade_rows),
            "discovery_signal_count": _signal_count(discovery),
            "holdout_signal_count": _signal_count(holdout),
        }

    if len(discovery_dates) != 7 or len(holdout_dates) != 7:
        raise ValueError("signal holdout expects one common seven-day window per phase")

    validation = validate_crypto_signal_pattern_holdout(
        discovery_rows,
        holdout_rows,
        policy=policy,
    )
    validation.update(
        source=_EXPECTED_SOURCE,
        strategy_mode=_EXPECTED_STRATEGY_MODE,
        symbols=list(normalized),
        discovery_archive_dates=sorted(discovery_dates),
        holdout_archive_dates=sorted(holdout_dates),
        source_windows=source_windows,
        source_data_recomputed=False,
        parameter_retuning_performed=False,
        strategy_promotion_allowed=False,
        trade_actionable=False,
        demo_activation_allowed=False,
        live_activation_allowed=False,
        bybit_live_order_routing_allowed=False,
    )
    return validation


def _read_report(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        raise ValueError(f"signal holdout source report missing:{path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("signal holdout source report must be an object")
    return raw


def _validate_source_report(report: Mapping[str, Any], *, symbol: str) -> None:
    if report.get("audit") != "BYBIT_CRYPTO_SINGLE_SYMBOL_CURRENT_QUALIFIED_AUDIT_V1":
        raise ValueError("signal holdout source audit kind is invalid")
    if report.get("symbol") != symbol:
        raise ValueError("signal holdout source symbol is invalid")
    if report.get("source") != _EXPECTED_SOURCE:
        raise ValueError("signal holdout source market-data contract is invalid")
    if report.get("strategy_mode") != _EXPECTED_STRATEGY_MODE:
        raise ValueError("signal holdout source strategy mode is invalid")
    for key in (
        "strategy_promotion_allowed",
        "trade_actionable",
        "demo_activation_allowed",
        "live_activation_allowed",
        "bybit_live_order_routing_allowed",
        "predictive_guarantee_allowed",
    ):
        if report.get(key) is not False:
            raise ValueError(f"signal holdout source unsafe marker:{key}")


def _archive_dates(report: Mapping[str, Any]) -> list[str]:
    values = report.get("archive_dates")
    if not isinstance(values, list) or len(values) != 7:
        raise ValueError("signal holdout source requires seven archive dates")
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("signal holdout source archive date is invalid")
        result.append(value)
    if result != sorted(result) or len(set(result)) != len(result):
        raise ValueError("signal holdout source archive dates must be unique and chronological")
    return result


def _trade_rows(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    trade_outcomes = report.get("trade_outcomes")
    if not isinstance(trade_outcomes, Mapping):
        raise ValueError("signal holdout source trade outcomes are missing")
    rows = trade_outcomes.get("trade_rows")
    if not isinstance(rows, list):
        raise ValueError("signal holdout source trade rows are missing")
    result: list[Mapping[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("signal holdout source trade row is invalid")
        result.append(row)
    return result


def _signal_count(report: Mapping[str, Any]) -> int:
    all_signals = report.get("all_eligible_signal_events")
    if not isinstance(all_signals, Mapping):
        raise ValueError("signal holdout source all-signal evidence is missing")
    value = all_signals.get("signal_event_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("signal holdout source signal count is invalid")
    return value


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate frozen crypto patterns across discovery and holdout archives"
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--minimum-discovery-trades", type=int, default=5)
    parser.add_argument("--minimum-holdout-trades", type=int, default=5)
    parser.add_argument("--minimum-discovery-symbols", type=int, default=2)
    parser.add_argument("--minimum-holdout-symbols", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    symbols = tuple(
        symbol.strip().upper()
        for symbol in args.symbols.split(",")
        if symbol.strip()
    )
    policy = CryptoSignalPatternHoldoutPolicy(
        minimum_discovery_trades=args.minimum_discovery_trades,
        minimum_holdout_trades=args.minimum_holdout_trades,
        minimum_discovery_symbols=args.minimum_discovery_symbols,
        minimum_holdout_symbols=args.minimum_holdout_symbols,
    )
    report = validate_holdout_directory(
        args.root,
        symbols=symbols,
        policy=policy,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "BYBIT_SIGNAL_PATTERN_HOLDOUT="
        + json.dumps(
            {
                "discovery_archive_dates": report["discovery_archive_dates"],
                "holdout_archive_dates": report["holdout_archive_dates"],
                "discovery_trade_count": report["discovery_trade_count"],
                "holdout_trade_count": report["holdout_trade_count"],
                "candidate_count": report["candidate_count"],
                "observed_holdout_perfect_count": report[
                    "observed_holdout_perfect_count"
                ],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
