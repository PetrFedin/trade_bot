from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.alpaca_historical import (
    ALPACA_STOCK_BARS_URL,
    AlpacaHistoricalBarsClient,
    AlpacaHistoricalBarsRequest,
    HistoricalAcquisition,
)
from app.marketdata.ohlcv import MultiSymbolOhlcvDataset, OhlcvBar
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.regime_momentum import RegimeAwareMomentumConfig

_SCHEMA = "multisymbol-ohlcv-selection-research-v1"


def _object(data: dict[str, Any], field: str) -> dict[str, Any]:
    value = data.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _positive_int(data: dict[str, Any], field: str) -> int:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _decimal(data: dict[str, Any], field: str) -> Decimal:
    value = data.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a decimal string")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError(f"{field} must be finite")
    return parsed


def _datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be RFC3339 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed


def load_config(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != _SCHEMA:
        raise ValueError("multi-symbol research config schema mismatch")
    if data.get("research_only") is not True:
        raise ValueError("multi-symbol acquisition must remain research-only")
    if data.get("strategy_promotion_allowed") is not False:
        raise ValueError("research config cannot allow strategy promotion")
    if data.get("provider") != "alpaca":
        raise ValueError("research provider must be alpaca")
    symbols = data.get("symbols")
    if (
        not isinstance(symbols, list)
        or len(symbols) < 2
        or not all(isinstance(symbol, str) for symbol in symbols)
    ):
        raise ValueError("research config requires at least two symbols")
    normalized = [symbol.strip().upper() for symbol in symbols]
    if normalized != symbols or len(set(symbols)) != len(symbols):
        raise ValueError("research symbols must be unique normalized uppercase")
    start = _datetime(data.get("start"), "start")
    end = _datetime(data.get("end"), "end")
    if end <= start:
        raise ValueError("research end must be after start")
    _positive_int(data, "limit")
    _positive_int(data, "maximum_pages")
    _positive_int(data, "minimum_bars_per_symbol")
    selection = _object(data, "selection")
    _positive_int(selection, "top_k")
    expected_ranking = [
        "momentum_desc",
        "trend_strength_desc",
        "realized_volatility_asc",
        "symbol_asc",
    ]
    if selection.get("ranking") != expected_ranking:
        raise ValueError("research ranking order mismatch")
    signal = _object(selection, "signal")
    _positive_int(signal, "fast_bars")
    _positive_int(signal, "slow_bars")
    _positive_int(signal, "momentum_lookback_bars")
    _positive_int(signal, "volatility_bars")
    _decimal(signal, "minimum_momentum_return")
    _decimal(signal, "minimum_trend_strength")
    _decimal(signal, "maximum_realized_volatility")
    return data


def build_request(config: dict[str, Any]) -> AlpacaHistoricalBarsRequest:
    return AlpacaHistoricalBarsRequest(
        symbols=tuple(config["symbols"]),
        start=_datetime(config["start"], "start"),
        end=_datetime(config["end"], "end"),
        timeframe=str(config["timeframe"]),
        feed=str(config["feed"]),
        adjustment=str(config["adjustment"]),
        currency=str(config["currency"]),
        limit=_positive_int(config, "limit"),
        maximum_pages=_positive_int(config, "maximum_pages"),
    )


def _signal_config(config: dict[str, Any]) -> RegimeAwareMomentumConfig:
    signal = _object(_object(config, "selection"), "signal")
    return RegimeAwareMomentumConfig(
        fast_bars=_positive_int(signal, "fast_bars"),
        slow_bars=_positive_int(signal, "slow_bars"),
        momentum_lookback_bars=_positive_int(signal, "momentum_lookback_bars"),
        volatility_bars=_positive_int(signal, "volatility_bars"),
        minimum_momentum_return=_decimal(signal, "minimum_momentum_return"),
        minimum_trend_strength=_decimal(signal, "minimum_trend_strength"),
        maximum_realized_volatility=_decimal(signal, "maximum_realized_volatility"),
    )


def _latest_common_timestamp(dataset: MultiSymbolOhlcvDataset) -> datetime:
    timestamps_by_symbol = [
        {bar.timestamp for bar in dataset.bars_for(symbol)} for symbol in dataset.symbols
    ]
    common = set.intersection(*timestamps_by_symbol)
    if not common:
        raise ValueError("no common decision timestamp across research symbols")
    return max(common)


def _selection_bars(dataset: MultiSymbolOhlcvDataset) -> list[OhlcvBar]:
    decision_time = _latest_common_timestamp(dataset)
    return [bar for bar in dataset.bars if bar.timestamp <= decision_time]


def write_csv(dataset: MultiSymbolOhlcvDataset, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(
            [
                "symbol",
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "trade_count",
                "vwap",
            ]
        )
        for bar in dataset.bars:
            writer.writerow(
                [
                    bar.symbol,
                    bar.timestamp.isoformat(),
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    bar.volume,
                    bar.trade_count,
                    "" if bar.vwap is None else str(bar.vwap),
                ]
            )
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_report(
    *,
    config: dict[str, Any],
    acquisition: HistoricalAcquisition,
    csv_sha256: str,
) -> dict[str, object]:
    minimum_bars = _positive_int(config, "minimum_bars_per_symbol")
    acquisition.validate(minimum_bars_per_symbol=minimum_bars)
    selection_config = _object(config, "selection")
    selector = CrossSectionalSelector(
        top_k=_positive_int(selection_config, "top_k"),
        signal_config=_signal_config(config),
    )
    selection = selector.select(_selection_bars(acquisition.dataset))
    return {
        "schema_version": _SCHEMA,
        "qualification": "PASS_RESEARCH_DATA",
        "provider": "alpaca",
        "endpoint": ALPACA_STOCK_BARS_URL,
        "research_only": True,
        "strategy_promotion_allowed": False,
        "paper_order_writes_enabled": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
        "feed": acquisition.dataset.feed,
        "timeframe": acquisition.dataset.timeframe,
        "adjustment": acquisition.dataset.adjustment,
        "symbols": list(acquisition.dataset.symbols),
        "bars_by_symbol": acquisition.dataset.counts_by_symbol(),
        "total_bars": len(acquisition.dataset.bars),
        "page_count": acquisition.page_count,
        "request_ids": list(acquisition.dataset.request_ids),
        "csv_sha256": csv_sha256,
        "query_start": str(config["start"]),
        "query_end": str(config["end"]),
        "selection": {
            "decision_time": selection.decision_time.isoformat(),
            "selected_symbols": list(selection.selected_symbols),
            "candidates": [
                {
                    **asdict(candidate),
                    "momentum_return": str(candidate.momentum_return),
                    "trend_strength": str(candidate.trend_strength),
                    "realized_volatility": str(candidate.realized_volatility),
                    "quality_score": str(candidate.quality_score),
                    "reference_price": str(candidate.reference_price),
                    "rejection_reasons": list(candidate.rejection_reasons),
                }
                for candidate in selection.candidates
            ],
        },
        "limitations": list(config["limitations"]),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Alpaca multi-symbol OHLCV research acquisition"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--csv-output", type=Path)
    parser.add_argument("--report-output", type=Path)
    parser.add_argument("--validate-config-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args.config)
    if args.validate_config_only:
        print(json.dumps({"schema_version": _SCHEMA, "config_valid": True}, sort_keys=True))
        return 0
    if args.csv_output is None or args.report_output is None:
        raise ValueError("csv-output and report-output are required for acquisition")
    key_id = os.environ.get("ASTRA_ALPACA_PAPER_KEY_ID", "")
    secret_key = os.environ.get("ASTRA_ALPACA_PAPER_SECRET_KEY", "")
    if not key_id or not secret_key:
        raise ValueError("ALPACA_MARKET_DATA_CREDENTIALS_MISSING")
    client = AlpacaHistoricalBarsClient(key_id=key_id, secret_key=secret_key)
    acquisition = client.fetch(build_request(config))
    csv_sha256 = write_csv(acquisition.dataset, args.csv_output)
    report = build_report(
        config=config,
        acquisition=acquisition,
        csv_sha256=csv_sha256,
    )
    args.report_output.parent.mkdir(parents=True, exist_ok=True)
    args.report_output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
