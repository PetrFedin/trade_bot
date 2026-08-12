import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tools.qualify_entry_quality_shadow import qualify as qualify_shadow
from tools.qualify_entry_quality_walk_forward import qualify as qualify_walk_forward

BASE_CONFIG = Path("research/cross_sectional_trading_quality_shadow_v2.json")
ENTRY_POLICY = Path("research/entry_quality_filter_shadow_v1.json")
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


def _series() -> dict[str, list[Decimal]]:
    aapl: list[Decimal] = []
    aapl_value = Decimal("100")
    for index in range(24):
        if index:
            aapl_value *= Decimal("1.06") if index in {8, 12, 16, 20} else Decimal("1.004")
        aapl.append(aapl_value)
    msft = [Decimal("100") + Decimal("0.60") * index for index in range(24)]
    nvda = [Decimal("100") + Decimal("0.45") * index for index in range(24)]
    return {"AAPL": aapl, "MSFT": msft, "NVDA": nvda}


def _write_csv(path: Path) -> None:
    closes = _series()
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
            for index, price in enumerate(closes[symbol]):
                writer.writerow(
                    [
                        symbol,
                        (START + timedelta(days=index)).isoformat(),
                        str(price),
                        str(price + Decimal("0.25")),
                        str(price - Decimal("0.25")),
                        str(price),
                        2_000_000 + index,
                        10_000 + index,
                        str(price),
                    ]
                )


def test_entry_quality_same_sample_is_marginal_and_non_promotable(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    csv_path = tmp_path / "bars.csv"
    _write_config(config_path)
    _write_csv(csv_path)

    evidence = qualify_shadow(csv_path, config_path, ENTRY_POLICY)
    json.dumps(evidence, sort_keys=True)

    assert evidence["qualification"] == "PASS_ENTRY_QUALITY_SHADOW_RESEARCH"
    assert evidence["shadow_only"] is True
    assert evidence["strategy_promotion_allowed"] is False
    assert evidence["external_order_routing_allowed"] is False
    assert evidence["live_trading_allowed"] is False
    assert evidence["selection_changes"]["changed_decision_count"] > 0
    assert evidence["evidence_satisfied_blockers"] == [
        "NO_ENTRY_QUALITY_SAME_SAMPLE_EVIDENCE"
    ]
    assert "NO_ENTRY_QUALITY_WALK_FORWARD_EVIDENCE" in evidence[
        "remaining_promotion_blockers"
    ]
    assert "NO_REAL_PAPER_ENTRY_QUALITY_EVIDENCE" in evidence[
        "remaining_promotion_blockers"
    ]


def test_entry_quality_walk_forward_is_non_fitting_and_non_promotable(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    csv_path = tmp_path / "bars.csv"
    _write_config(config_path)
    _write_csv(csv_path)

    evidence = qualify_walk_forward(csv_path, config_path, ENTRY_POLICY)
    json.dumps(evidence, sort_keys=True)

    assert evidence["qualification"] == "PASS_ENTRY_QUALITY_WALK_FORWARD_RESEARCH"
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
        "NO_ENTRY_QUALITY_WALK_FORWARD_EVIDENCE"
    ]
    assert "NO_ENTRY_QUALITY_WALK_FORWARD_EVIDENCE" not in evidence[
        "remaining_promotion_blockers"
    ]
    assert "NO_REAL_PAPER_ENTRY_QUALITY_EVIDENCE" in evidence[
        "remaining_promotion_blockers"
    ]
    assert "NO_STRATEGY_PROFITABILITY_CLAIM" in evidence["limitations"]
