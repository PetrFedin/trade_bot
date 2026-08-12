import csv
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tools.qualify_external_strategy_candidates import qualify_suite

BASE_CONFIG = Path("research/cross_sectional_trading_quality_shadow_v2.json")
ENTRY_POLICY = Path("research/entry_quality_filter_shadow_v1.json")
EXIT_POLICY = Path("research/selection_exit_confirmation_shadow_v1.json")
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


def _write_csv(path: Path) -> None:
    series = {
        "AAPL": [Decimal("100") + Decimal(index) for index in range(24)],
        "MSFT": [Decimal("100") + Decimal("0.7") * index for index in range(24)],
        "NVDA": [
            Decimal("104") - Decimal("0.3") * index
            if index < 12
            else Decimal("100.4") + Decimal("0.8") * (index - 12)
            for index in range(24)
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
        for symbol in sorted(series):
            for index, price in enumerate(series[symbol]):
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


def test_candidate_suite_reuses_one_csv_and_never_promotes(tmp_path: Path) -> None:
    csv_path = tmp_path / "bars.csv"
    config_path = tmp_path / "config.json"
    output_dir = tmp_path / "candidate-suite"
    _write_csv(csv_path)
    _write_config(config_path)

    manifest = qualify_suite(
        csv_path=csv_path,
        trading_quality_config_path=config_path,
        entry_quality_policy_path=ENTRY_POLICY,
        selection_exit_policy_path=EXIT_POLICY,
        profit_runner_policy_path=RUNNER_POLICY,
        output_dir=output_dir,
    )
    source_sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()

    assert manifest["qualification"] == "PASS_EXTERNAL_STRATEGY_CANDIDATE_SUITE"
    assert manifest["source_csv_sha256"] == source_sha
    assert manifest["candidate_count"] == 6
    assert manifest["shadow_only"] is True
    assert manifest["strategy_promotion_allowed"] is False
    assert manifest["external_order_routing_allowed"] is False
    assert manifest["live_trading_allowed"] is False
    assert set(manifest["reports"]) == {
        "entry_quality_same_sample",
        "entry_quality_walk_forward",
        "selection_exit_same_sample",
        "selection_exit_walk_forward",
        "profit_runner_same_sample",
        "profit_runner_walk_forward",
    }
    assert set(manifest["remaining_real_paper_blockers"]) == {
        "NO_REAL_PAPER_ENTRY_QUALITY_EVIDENCE",
        "NO_REAL_PAPER_PROFIT_RUNNER_EVIDENCE",
        "NO_REAL_PAPER_SELECTION_EXIT_EVIDENCE",
    }

    for file_name in manifest["reports"].values():
        report = json.loads((output_dir / file_name).read_text(encoding="utf-8"))
        assert report["source_csv_sha256"] == source_sha
        assert report["shadow_only"] is True
        assert report["strategy_promotion_allowed"] is False
        assert report["external_order_routing_allowed"] is False
        assert report["live_trading_allowed"] is False

    stored_manifest = json.loads(
        (output_dir / "strategy-candidate-suite-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored_manifest == manifest
