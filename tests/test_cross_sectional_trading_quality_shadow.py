from datetime import UTC, datetime, timedelta
from decimal import Decimal
import csv
import json
from pathlib import Path

from tools.qualify_cross_sectional_trading_quality_shadow import (
    load_config,
    qualify,
)

CONFIG = Path("research/cross_sectional_trading_quality_shadow_v2.json")
START = datetime(2026, 1, 2, tzinfo=UTC)


def _write_csv(path: Path) -> None:
    closes = {
        "AAPL": [
            "100",
            "101",
            "102",
            "103",
            "104",
            "105",
            "106",
            "108",
            "110",
            "109.5",
            "111",
            "112",
        ],
        "MSFT": [
            "100",
            "100.5",
            "101",
            "101.5",
            "102",
            "102.5",
            "103",
            "104",
            "104.5",
            "105",
            "105.5",
            "106",
        ],
        "NVDA": [
            "107",
            "106",
            "105",
            "104",
            "103",
            "102",
            "101",
            "100",
            "99",
            "98",
            "97",
            "96",
        ],
    }
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
        for symbol in sorted(closes):
            for index, close in enumerate(closes[symbol]):
                price = Decimal(close)
                writer.writerow(
                    [
                        symbol,
                        (START + timedelta(days=index)).isoformat(),
                        str(price),
                        str(price + Decimal("0.3")),
                        str(price - Decimal("0.3")),
                        str(price),
                        1000 + index,
                        100 + index,
                        str(price),
                    ]
                )


def test_combined_trading_quality_config_is_fail_closed() -> None:
    config = load_config(CONFIG)
    assert config["shadow_only"] is True
    assert config["strategy_promotion_allowed"] is False
    assert "NO_STRATEGY_PROFITABILITY_CLAIM" in config["limitations"]
    assert "NO_OUT_OF_SAMPLE_ABLATION_EVIDENCE" in config["promotion_blockers"]
    assert "CONFIRMED_REENTRY" in config["shared_controls"]
    assert config["candidate_components"] == [
        "RISK_ADJUSTED_SELECTION_SCORE",
        "STOP_RISK_INVERSE_VOLATILITY_SIZING",
        "BREAK_EVEN_AND_PROFIT_PROTECTION",
    ]


def test_combined_candidate_compares_end_to_end_against_legacy(tmp_path: Path) -> None:
    csv_path = tmp_path / "bars.csv"
    _write_csv(csv_path)

    evidence = qualify(csv_path, CONFIG)
    payload = json.dumps(evidence, sort_keys=True)

    assert payload
    assert evidence["qualification"] == "PASS_COMPARATIVE_RESEARCH"
    assert evidence["shadow_only"] is True
    assert evidence["strategy_promotion_allowed"] is False
    assert evidence["external_order_routing_allowed"] is False
    assert evidence["live_trading_allowed"] is False
    assert evidence["symbols"] == ["AAPL", "MSFT", "NVDA"]
    assert evidence["synchronized_timestamp_count"] == 12
    assert evidence["ablation_evidence_available"] is True
    assert evidence["component_attribution_complete"] is False
    assert evidence["candidate_components"] == [
        "RISK_ADJUSTED_SELECTION_SCORE",
        "STOP_RISK_INVERSE_VOLATILITY_SIZING",
        "BREAK_EVEN_AND_PROFIT_PROTECTION",
    ]
    assert "CONFIRMED_REENTRY" in evidence["shared_controls"]
    assert set(evidence["ablations"]) == {
        "SELECTION_ONLY",
        "SIZING_ONLY",
        "PROTECTION_ONLY",
    }
    assert set(evidence["ablation_deltas_vs_control"]) == set(evidence["ablations"])

    for side in ("control", "candidate"):
        metrics = evidence[side]
        assert "total_return" in metrics
        assert "max_drawdown_fraction" in metrics
        assert "win_rate" in metrics
        assert "profit_factor" in metrics
        assert "profit_preservation_rate" in metrics
        assert "average_mfe_capture_ratio" in metrics
        assert Decimal(metrics["maximum_gross_exposure_fraction_observed"]) >= 0
    for metrics in evidence["ablations"].values():
        assert "total_return" in metrics
        assert "max_drawdown_fraction" in metrics
        assert "profit_preservation_rate" in metrics

    assert set(evidence["comparison"]) == {
        "total_return_delta",
        "max_drawdown_fraction_delta",
        "win_rate_delta",
        "profit_factor_delta",
        "profit_preservation_rate_delta",
        "average_mfe_capture_ratio_delta",
    }
