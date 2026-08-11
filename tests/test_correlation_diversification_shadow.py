import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tools.qualify_correlation_diversification_shadow import qualify

TRADING_QUALITY_CONFIG = Path("research/cross_sectional_trading_quality_shadow_v2.json")
DIVERSIFICATION_POLICY = Path("research/correlation_diversification_shadow_v1.json")
START = datetime(2026, 1, 2, tzinfo=UTC)


def _write_csv(path: Path) -> None:
    strong_trend = [
        "100",
        "102",
        "104",
        "106",
        "108",
        "110",
        "112",
        "114",
        "116",
        "118",
        "120",
        "122",
    ]
    closes = {
        "AAPL": strong_trend,
        "MSFT": strong_trend,
        "NVDA": [
            "100",
            "101.5",
            "101",
            "102.5",
            "102",
            "103.5",
            "103",
            "104.5",
            "104",
            "105.5",
            "105",
            "106.5",
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


def test_diversification_shadow_is_marginal_and_non_promotable(tmp_path: Path) -> None:
    csv_path = tmp_path / "bars.csv"
    _write_csv(csv_path)

    evidence = qualify(
        csv_path,
        TRADING_QUALITY_CONFIG,
        DIVERSIFICATION_POLICY,
    )
    payload = json.dumps(evidence, sort_keys=True)

    assert payload
    assert evidence["qualification"] == "PASS_DIVERSIFICATION_RESEARCH"
    assert evidence["shadow_only"] is True
    assert evidence["strategy_promotion_allowed"] is False
    assert evidence["external_order_routing_allowed"] is False
    assert evidence["live_trading_allowed"] is False
    assert evidence["symbols"] == ["AAPL", "MSFT", "NVDA"]
    assert evidence["correlation_policy"] == {
        "lookback_bars": 7,
        "minimum_return_observations": 7,
        "maximum_pairwise_correlation": "0.85",
    }
    assert evidence["selection_changes"]["changed_decision_count"] > 0
    assert evidence["selection_changes"]["dropped_symbol_counts"]["MSFT"] > 0
    assert evidence["selection_changes"]["added_symbol_counts"]["NVDA"] > 0
    assert "total_return_delta" in evidence["comparison"]
    assert "NO_WALK_FORWARD_DIVERSIFICATION_EVIDENCE" in evidence[
        "promotion_blockers"
    ]
    assert "NO_STRATEGY_PROFITABILITY_CLAIM" in evidence["limitations"]
