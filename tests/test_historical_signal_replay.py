import csv
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tools.replay_historical_signals import replay

ROOT = Path(__file__).resolve().parents[1]
TRADING_QUALITY = ROOT / "research/cross_sectional_trading_quality_shadow_v2.json"
ENTRY_QUALITY = ROOT / "research/entry_quality_filter_shadow_v1.json"
SELECTION_EXIT = ROOT / "research/selection_exit_confirmation_shadow_v1.json"
PROFIT_RUNNER = ROOT / "research/profit_runner_shadow_v1.json"


def _write_market(path: Path, *, days: int = 31) -> None:
    rows = []
    start = datetime(2026, 1, 1, tzinfo=UTC)
    slopes = {
        "AAPL": Decimal("1.00"),
        "MSFT": Decimal("0.70"),
        "NVDA": Decimal("0.25"),
    }
    for symbol, slope in slopes.items():
        for index in range(days):
            close = Decimal("100") + slope * Decimal(index)
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": (start + timedelta(days=index)).isoformat(),
                    "open": str(close * Decimal("0.999")),
                    "high": str(close * Decimal("1.01")),
                    "low": str(close * Decimal("0.99")),
                    "close": str(close),
                    "volume": "1000000",
                    "trade_count": "10000",
                    "vwap": str(close),
                }
            )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "symbol",
                "timestamp",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "trade_count",
                "vwap",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def test_replay_emits_trade_and_decision_evidence_without_lookahead(
    tmp_path: Path,
) -> None:
    csv_path = tmp_path / "bars.csv"
    _write_market(csv_path)

    report = replay(
        csv_path=csv_path,
        trading_quality_config_path=TRADING_QUALITY,
        entry_quality_policy_path=ENTRY_QUALITY,
        selection_exit_policy_path=SELECTION_EXIT,
        profit_runner_policy_path=PROFIT_RUNNER,
        completed_through=date(2026, 1, 30),
        source_label="TEST_SYNTHETIC",
    )

    assert report["qualification"] == "PASS_HISTORICAL_REPLAY"
    assert report["evidence_scope"] == "OBSERVED_MARKET_HISTORY_REPLAY"
    assert report["live_trading_allowed"] is False
    assert report["real_paper_fills"] is False
    assert report["bars_excluded_by_cutoff"] == 3
    assert report["last_timestamp"].startswith("2026-01-30")
    assert report["symbols"] == ["AAPL", "MSFT", "NVDA"]

    terminal = report["terminal_completed_bar_signal"]
    assert terminal["decision_time"] == report["last_timestamp"]
    assert terminal["execution_pending"] is True
    assert terminal["stateful_entry_gates_evaluated"] is False
    assert terminal["current_selected_symbols"]
    assert terminal["candidates"]
    assert all("quality_score" in candidate for candidate in terminal["candidates"])

    variants = report["variants"]
    assert set(variants) == {
        "CURRENT_COMBINED_SHADOW",
        "ENTRY_QUALITY_CANDIDATE",
        "SELECTION_EXIT_CONFIRMATION_CANDIDATE",
        "ENTRY_QUALITY_PLUS_SELECTION_EXIT_INTERACTION",
        "PROFIT_RUNNER_CANDIDATE",
    }
    assert report["declared_interaction_checks"] == [
        "ENTRY_QUALITY_PLUS_SELECTION_EXIT_INTERACTION"
    ]
    for payload in variants.values():
        assert payload["no_lookahead_verified"] is True
        assert payload["trade_timestamps_verified"] is True
        assert payload["decision_trace"]
        for decision in payload["decision_trace"]:
            assert decision["decision_time"] < decision["execution_time"]

    baseline = variants["CURRENT_COMBINED_SHADOW"]
    assert baseline["closed_trades"]
    assert baseline["metrics"]["closed_trade_count"] >= 1


def test_replay_candidates_and_interaction_remain_unpromoted(tmp_path: Path) -> None:
    csv_path = tmp_path / "bars.csv"
    _write_market(csv_path)

    report = replay(
        csv_path=csv_path,
        trading_quality_config_path=TRADING_QUALITY,
        entry_quality_policy_path=ENTRY_QUALITY,
        selection_exit_policy_path=SELECTION_EXIT,
        profit_runner_policy_path=PROFIT_RUNNER,
        completed_through=date(2026, 1, 31),
    )

    assert report["counterfactual_candidates_promoted"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["paper_order_writes_enabled"] is False
    assert report["external_order_routing_allowed"] is False
    assert set(report["comparisons_vs_current_combined_shadow"]) == {
        "ENTRY_QUALITY_CANDIDATE",
        "SELECTION_EXIT_CONFIRMATION_CANDIDATE",
        "ENTRY_QUALITY_PLUS_SELECTION_EXIT_INTERACTION",
        "PROFIT_RUNNER_CANDIDATE",
    }
