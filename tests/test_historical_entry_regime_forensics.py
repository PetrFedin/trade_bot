from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from tests.test_historical_portfolio_evidence_tool import _write_symbol_csvs
from tools.historical_entry_regime_forensics import _bucket, run_entry_forensics


def test_predeclared_volatility_buckets() -> None:
    assert _bucket(Decimal("0.01"), Decimal("0.025"), Decimal("0.055")) == "LOW"
    assert _bucket(Decimal("0.03"), Decimal("0.025"), Decimal("0.055")) == "MID"
    assert _bucket(Decimal("0.06"), Decimal("0.025"), Decimal("0.055")) == "HIGH"


def test_frozen_replay_and_entry_regimes(tmp_path: Path) -> None:
    _write_symbol_csvs(tmp_path)
    payload = run_entry_forensics(
        bars_dir=tmp_path, symbols=("AAPL", "MSFT", "NVDA"),
    )
    assert payload["research_only"] is True
    assert payload["strategy_promotion_allowed"] is False
    assert payload["strategy_changed"] is False
    assert payload["summary"]["trades"] == len(payload["trades"])
    assert (payload["summary"]["same_bar_exits"] +
            payload["summary"]["next_bar_exits"] <=
            payload["summary"]["early_exits"])
    assert sum(x["closed_trades"] for x in payload["summary"]["by_prior_regime"].values()) == len(payload["trades"])
    for row in payload["trades"]:
        assert "prior_20_bar_mean_range_fraction" in row
        assert "entry_bar_range_fraction_post_entry" in row


def test_research_runner_makes_no_network_calls(tmp_path: Path) -> None:
    _write_symbol_csvs(tmp_path)
    with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
        run_entry_forensics(bars_dir=tmp_path, symbols=("AAPL", "MSFT", "NVDA"))
