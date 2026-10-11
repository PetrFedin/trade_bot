from pathlib import Path

from tests.test_historical_portfolio_evidence_tool import _write_symbol_csvs
from tools.historical_entry_regime_forensics import run_entry_forensics


def test_uncertainty_conservation(tmp_path: Path) -> None:
    _write_symbol_csvs(tmp_path)
    result = run_entry_forensics(
        bars_dir=tmp_path, symbols=("AAPL", "MSFT", "NVDA")
    )
    rows = result["trades"]
    summary = result["summary"]
    assert summary["intrabar_ambiguous_exits"] == sum(
        x["ambiguous_intrabar_exit"] for x in rows
    )
    assert summary["gap_through_protective_stops"] == sum(
        x["gap_through_stop"] for x in rows
    )
    assert summary["same_bar_ambiguous_exits"] == sum(
        x["same_bar_exit"] and x["ambiguous_intrabar_exit"] for x in rows
    )
    assert sum(
        x["ambiguous_intrabar_exits"]
        for x in summary["by_prior_regime"].values()
    ) == summary["intrabar_ambiguous_exits"]
