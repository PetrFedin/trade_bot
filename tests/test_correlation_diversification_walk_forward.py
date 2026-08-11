import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tools.qualify_correlation_diversification_walk_forward import qualify

BASE_CONFIG = Path("research/cross_sectional_trading_quality_shadow_v2.json")
DIVERSIFICATION_POLICY = Path("research/correlation_diversification_shadow_v1.json")
START = datetime(2026, 1, 2, tzinfo=UTC)


def _write_config(path: Path) -> None:
    config = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    config["walk_forward"] = {
        "training_bars": 8,
        "holdout_bars": 4,
        "step_bars": 4,
        "minimum_folds": 3,
        "parameter_fitting_allowed": False,
        "reset_portfolio_each_fold": True,
        "non_overlapping_holdouts": True,
    }
    path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def _write_csv(path: Path) -> None:
    aapl = [str(100 + 2 * index) for index in range(24)]
    msft = list(aapl)
    nvda: list[str] = []
    value = Decimal("100")
    for index in range(24):
        if index:
            value += Decimal("1.5") if index % 2 else Decimal("-0.5")
        nvda.append(str(value))
    closes = {"AAPL": aapl, "MSFT": msft, "NVDA": nvda}

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


def test_diversification_walk_forward_is_holdout_only_and_non_promotable(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    csv_path = tmp_path / "bars.csv"
    _write_config(config_path)
    _write_csv(csv_path)

    evidence = qualify(csv_path, config_path, DIVERSIFICATION_POLICY)
    payload = json.dumps(evidence, sort_keys=True)

    assert payload
    assert evidence["qualification"] == "PASS_DIVERSIFICATION_WALK_FORWARD_RESEARCH"
    assert evidence["shadow_only"] is True
    assert evidence["strategy_promotion_allowed"] is False
    assert evidence["external_order_routing_allowed"] is False
    assert evidence["live_trading_allowed"] is False
    assert evidence["fold_count"] == 4
    assert evidence["walk_forward_policy"]["parameter_fitting_allowed"] is False
    assert evidence["walk_forward_policy"]["reset_portfolio_each_fold"] is True
    assert evidence["walk_forward_policy"]["non_overlapping_holdouts"] is True
    assert evidence["aggregate"]["changed_decision_count"] > 0
    assert evidence["evidence_satisfied_blockers"] == [
        "NO_WALK_FORWARD_DIVERSIFICATION_EVIDENCE"
    ]
    assert "NO_WALK_FORWARD_DIVERSIFICATION_EVIDENCE" not in evidence[
        "remaining_promotion_blockers"
    ]
    assert "NO_EXTERNAL_PAPER_PORTFOLIO_EXECUTION" in evidence[
        "remaining_promotion_blockers"
    ]
    assert "NO_STRATEGY_PROFITABILITY_CLAIM" in evidence["limitations"]
