import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tools.qualify_selection_exit_confirmation_shadow import qualify as qualify_shadow
from tools.qualify_selection_exit_confirmation_walk_forward import (
    qualify as qualify_walk_forward,
)

BASE_CONFIG = Path("research/cross_sectional_trading_quality_shadow_v2.json")
EXIT_POLICY = Path("research/selection_exit_confirmation_shadow_v1.json")
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
    aapl = [
        Decimal(value)
        for value in (
            "100", "101", "102", "103", "104", "105", "106", "108",
            "106.5", "107", "106", "105.5", "105", "104.5", "104", "103.5",
            "104", "105", "106", "107", "108", "107", "106", "105",
        )
    ]
    msft = [
        Decimal(value)
        for value in (
            "100", "100.4", "100.8", "101.2", "101.6", "102", "102.4", "103",
            "104.5", "105.5", "106", "106.5", "107", "107.5", "108", "108.5",
            "108", "107.5", "107", "106.5", "106", "106.5", "107", "107.5",
        )
    ]
    nvda = [
        Decimal(value)
        for value in (
            "103", "102.8", "102.6", "102.4", "102.2", "102", "101.8", "101.6",
            "101.4", "101.2", "101", "100.8", "101", "102", "103", "104",
            "105", "106", "107", "108", "109", "110", "111", "112",
        )
    ]
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


def test_selection_exit_same_sample_evidence_remains_non_promotable(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    csv_path = tmp_path / "bars.csv"
    _write_config(config_path)
    _write_csv(csv_path)

    evidence = qualify_shadow(csv_path, config_path, EXIT_POLICY)
    json.dumps(evidence, sort_keys=True)

    assert evidence["qualification"] == (
        "PASS_SELECTION_EXIT_CONFIRMATION_SHADOW_RESEARCH"
    )
    assert evidence["shadow_only"] is True
    assert evidence["strategy_promotion_allowed"] is False
    assert evidence["external_order_routing_allowed"] is False
    assert evidence["live_trading_allowed"] is False
    assert "selection_exit_confirmation_pending_count" in evidence[
        "candidate_confirmed_exit"
    ]
    assert evidence["evidence_satisfied_blockers"] == [
        "NO_SELECTION_EXIT_SAME_SAMPLE_EVIDENCE"
    ]
    assert "NO_SELECTION_EXIT_WALK_FORWARD_EVIDENCE" in evidence[
        "remaining_promotion_blockers"
    ]
    assert "NO_REAL_PAPER_SELECTION_EXIT_EVIDENCE" in evidence[
        "remaining_promotion_blockers"
    ]


def test_selection_exit_walk_forward_is_fixed_parameter_and_non_promotable(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    csv_path = tmp_path / "bars.csv"
    _write_config(config_path)
    _write_csv(csv_path)

    evidence = qualify_walk_forward(csv_path, config_path, EXIT_POLICY)
    json.dumps(evidence, sort_keys=True)

    assert evidence["qualification"] == (
        "PASS_SELECTION_EXIT_CONFIRMATION_WALK_FORWARD_RESEARCH"
    )
    assert evidence["shadow_only"] is True
    assert evidence["strategy_promotion_allowed"] is False
    assert evidence["fold_count"] == 4
    assert evidence["walk_forward_policy"]["parameter_fitting_allowed"] is False
    assert evidence["walk_forward_policy"]["reset_portfolio_each_fold"] is True
    assert evidence["walk_forward_policy"]["non_overlapping_holdouts"] is True
    assert "pending_confirmation_count" in evidence["aggregate"]
    assert "mean_hard_stop_count_delta" in evidence["aggregate"]
    assert evidence["evidence_satisfied_blockers"] == [
        "NO_SELECTION_EXIT_WALK_FORWARD_EVIDENCE"
    ]
    assert "NO_REAL_PAPER_SELECTION_EXIT_EVIDENCE" in evidence[
        "remaining_promotion_blockers"
    ]
    assert "NO_STRATEGY_PROFITABILITY_CLAIM" in evidence["limitations"]
