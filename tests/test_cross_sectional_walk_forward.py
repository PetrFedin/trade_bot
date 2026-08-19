import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tools.qualify_cross_sectional_walk_forward import qualify

BASE_CONFIG = Path("research/cross_sectional_trading_quality_shadow_v2.json")
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
    closes = {
        "AAPL": [str(100 + index) for index in range(24)],
        "MSFT": [str(100 + Decimal(index) * Decimal("0.6")) for index in range(24)],
        "NVDA": [str(124 - index) for index in range(24)],
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


def test_walk_forward_uses_non_overlapping_holdouts_without_parameter_fitting(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    csv_path = tmp_path / "bars.csv"
    _write_config(config_path)
    _write_csv(csv_path)

    evidence = qualify(csv_path, config_path)
    payload = json.dumps(evidence, sort_keys=True)

    assert payload
    assert evidence["qualification"] == "PASS_WALK_FORWARD_RESEARCH"
    assert evidence["shadow_only"] is True
    assert evidence["strategy_promotion_allowed"] is False
    assert evidence["external_order_routing_allowed"] is False
    assert evidence["live_trading_allowed"] is False
    assert evidence["fold_count"] == 4
    assert evidence["walk_forward_policy"] == {
        "training_bars": 8,
        "holdout_bars": 4,
        "step_bars": 4,
        "minimum_folds": 3,
        "parameter_fitting_allowed": False,
        "reset_portfolio_each_fold": True,
        "non_overlapping_holdouts": True,
    }

    previous_holdout_end = None
    for fold in evidence["folds"]:
        assert fold["training_bars"] == 8
        assert fold["holdout_bars"] == 4
        assert set(fold["ablations"]) == {
            "SELECTION_ONLY",
            "SIZING_ONLY",
            "PROTECTION_ONLY",
        }
        assert set(fold["ablation_deltas_vs_control"]) == set(fold["ablations"])
        holdout_start = datetime.fromisoformat(fold["holdout_start"])
        holdout_end = datetime.fromisoformat(fold["holdout_end"])
        if previous_holdout_end is not None:
            assert holdout_start > previous_holdout_end
        previous_holdout_end = holdout_end

    assert set(evidence["evidence_satisfied_blockers"]) == {
        "NO_WALK_FORWARD_HOLDOUT_EVIDENCE",
        "NO_OUT_OF_SAMPLE_ABLATION_EVIDENCE",
    }
    assert "NO_WALK_FORWARD_HOLDOUT_EVIDENCE" not in evidence[
        "remaining_promotion_blockers"
    ]
    assert "NO_OUT_OF_SAMPLE_ABLATION_EVIDENCE" not in evidence[
        "remaining_promotion_blockers"
    ]
    assert "NO_EXTERNAL_PAPER_PORTFOLIO_EXECUTION" in evidence[
        "remaining_promotion_blockers"
    ]
    assert "DEGRADATION_THRESHOLDS_UNCALIBRATED" in evidence[
        "remaining_promotion_blockers"
    ]
    assert "NO_STRATEGY_PROFITABILITY_CLAIM" in evidence["limitations"]
