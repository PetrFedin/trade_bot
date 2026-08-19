import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.marketdata.alpaca_historical import HistoricalAcquisition
from app.marketdata.ohlcv import MultiSymbolOhlcvDataset, OhlcvBar
from tools.external_multisymbol_historical import build_report, load_config

CONFIG = Path("research/multisymbol_ohlcv_selection_v1.json")
START = datetime(2026, 1, 2, tzinfo=UTC)


def _bars(symbol: str, closes: list[str]) -> list[OhlcvBar]:
    return [
        OhlcvBar(
            symbol=symbol,
            timestamp=START + timedelta(days=index),
            open=Decimal(close),
            high=Decimal(close) + Decimal("0.2"),
            low=Decimal(close) - Decimal("0.2"),
            close=Decimal(close),
            volume=1000 + index,
            trade_count=100 + index,
            vwap=Decimal(close),
        )
        for index, close in enumerate(closes)
    ]


def test_external_report_serializes_selection_quality_score() -> None:
    config = load_config(CONFIG)
    config["minimum_bars_per_symbol"] = 8
    config["selection"]["top_k"] = 1
    dataset = MultiSymbolOhlcvDataset(
        provider="alpaca",
        feed="iex",
        timeframe="1Day",
        adjustment="all",
        bars=tuple(
            [
                *_bars(
                    "AAPL",
                    ["100", "101", "102", "103", "104", "105", "106", "108"],
                ),
                *_bars(
                    "MSFT",
                    ["100", "100.4", "100.8", "101.2", "101.6", "102", "102.5", "103"],
                ),
            ]
        ),
        request_ids=("req-test",),
    )
    acquisition = HistoricalAcquisition(
        dataset=dataset,
        page_count=1,
        missing_symbols=(),
    )

    report = build_report(
        config=config,
        acquisition=acquisition,
        csv_sha256="0" * 64,
    )
    payload = json.dumps(report, sort_keys=True)

    assert payload
    assert report["selection"]["selected_symbols"] == ["AAPL"]
    candidates = report["selection"]["candidates"]
    assert all(isinstance(candidate["quality_score"], str) for candidate in candidates)
