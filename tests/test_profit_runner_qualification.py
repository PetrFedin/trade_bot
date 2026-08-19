import csv
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tools.qualify_profit_runner_shadow import qualify as qualify_shadow
from tools.qualify_profit_runner_walk_forward import qualify as qualify_walk_forward

BASE_CONFIG = Path("research/cross_sectional_trading_quality_shadow_v2.json")
RUNNER_POLICY = Path("research/profit_runner_shadow_v1.json")
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
            "110", "112", "111", "113", "115", "117", "116", "118",
            "120", "122", "121", "123", "125", "127", "126", "128",
        )
    ]
    msft = [
        Decimal(value)
        for value in (
            "100", "100.5", "101", "101.5", "102", "102.5", "103", "104",
            "105", "106", "107", "108", "109", "110", "111", "112",
            "113", "114", "115", "116", "117", "118", "119", "120",
        )
    ]
    nvda = [
        Decimal(value)
        for value in (
            "105", "104.5", "104", "103.5", "103", "102.5", "102", "101.5",
            "101", "100.5", "100", "99.5", "99", "98.5", "98", "97.5",
            "98", "99", "100", "101", "102", "103", "104", "105",
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
                        str(price + Decimal("0.75")),
                        str(price - Decimal("0.50")),
                        str(price),
                        2_000_000 + index,
                        10_000 + index,
                        str(price),
                    ]
                )


def test_profit_runner_same_sample_evidence_remains_non_promotable(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    csv_path = tmp_path / "bars.csv"
    _write_config(config_path)
    _write_csv(csv_path)

    evidence = qualify_shadow(csv_path, config_path, RUNNER_POLICY)
    json.dumps(evidence, sort_keys=True)

    assert evidence["qualification"] == "PASS_PROFIT_RUNNER_SHADOW_RESEARCH"
    assert evidence["shadow_only"] is True
    assert evidence["strategy_promotion_allowed"] is False
    assert evidence["external_order_routing_allowed"] is False
    assert evidence["live_trading_allowed"] is False
    assert "take_profit_exit_count" in evidence["control_fixed_take_profit"]
    assert "average_mfe_giveback_fraction" in evidence["candidate_profit_runner"]
    assert "average_mfe_capture_ratio_delta" in evidence["comparison"]
    assert evidence["evidence_satisfied_blockers"] == [
        "NO_PROFIT_RUNNER_SAME_SAMPLE_EVIDENCE"
    ]
    assert "NO_PROFIT_RUNNER_WALK_FORWARD_EVIDENCE" in evidence[
        "remaining_promotion_blockers"
    ]
    assert "NO_REAL_PAPER_PROFIT_RUNNER_EVIDENCE" in evidence[
        "remaining_promotion_blockers"
    ]


def test_profit_runner_walk_forward_is_fixed_parameter_and_non_promotable(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.json"
    csv_path = tmp_path / "bars.csv"
    _write_config(config_path)
    _write_csv(csv_path)

    evidence = qualify_walk_forward(csv_path, config_path, RUNNER_POLICY)
    json.dumps(evidence, sort_keys=True)

    assert evidence["qualification"] == "PASS_PROFIT_RUNNER_WALK_FORWARD_RESEARCH"
    assert evidence["shadow_only"] is True
    assert evidence["strategy_promotion_allowed"] is False
    assert evidence["external_order_routing_allowed"] is False
    assert evidence["live_trading_allowed"] is False
    assert evidence["fold_count"] == 4
    assert evidence["walk_forward_policy"]["parameter_fitting_allowed"] is False
    assert evidence["walk_forward_policy"]["reset_portfolio_each_fold"] is True
    assert evidence["walk_forward_policy"]["non_overlapping_holdouts"] is True
    assert "mean_average_mfe_capture_ratio_delta" in evidence["aggregate"]
    assert "mean_average_mfe_giveback_fraction_delta" in evidence["aggregate"]
    assert "mean_hard_stop_count_delta" in evidence["aggregate"]
    assert evidence["evidence_satisfied_blockers"] == [
        "NO_PROFIT_RUNNER_WALK_FORWARD_EVIDENCE"
    ]
    assert "NO_REAL_PAPER_PROFIT_RUNNER_EVIDENCE" in evidence[
        "remaining_promotion_blockers"
    ]
    assert "NO_STRATEGY_PROFITABILITY_CLAIM" in evidence["limitations"]
