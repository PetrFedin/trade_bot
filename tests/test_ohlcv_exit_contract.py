from decimal import Decimal
from pathlib import Path

from tools.qualify_ohlcv_exit_contract import qualify

CONTRACT = Path("research/ohlcv_exit_contract_v1.json")


def test_ohlcv_exit_contract_is_shadow_only_and_non_promotable() -> None:
    evidence = qualify(CONTRACT)
    assert evidence["qualification"] == "PASS_SYNTHETIC_CONTRACT"
    assert evidence["shadow_only"] is True
    assert evidence["strategy_promotion_allowed"] is False
    assert evidence["real_ohlcv_evidence"] is False
    assert evidence["multisymbol_portfolio_evidence"] is False
    assert set(evidence["promotion_blockers"]) == {
        "SYNTHETIC_CONTRACT_ONLY",
        "NO_REAL_OHLCV_EVIDENCE",
        "NO_MULTISYMBOL_PORTFOLIO_BACKTEST",
        "NO_EXTERNAL_PAPER_STRATEGY_EVIDENCE",
    }


def test_ohlcv_contract_locks_profit_and_protective_exit_semantics() -> None:
    scenarios = qualify(CONTRACT)["scenarios"]
    take = scenarios["INTRABAR_TAKE_PROFIT"]
    assert take["exit_reason"] == "INTRABAR_TAKE_PROFIT"
    assert Decimal(take["net_pnl"]) > 0

    ambiguous = scenarios["AMBIGUOUS_STOP_AND_TAKE_PROTECTIVE_FIRST"]
    assert ambiguous["exit_reason"] == "INTRABAR_HARD_STOP"
    assert ambiguous["ambiguous_intrabar_exit"] is True
    assert Decimal(ambiguous["net_pnl"]) < 0

    gap = scenarios["GAP_THROUGH_HARD_STOP"]
    assert gap["gap_through_stop"] is True
    assert gap["exit_execution_price"] == "100"

    trailing = scenarios["TRAILING_FROM_COMPLETED_PRIOR_PEAK"]
    assert trailing["exit_reason"] == "INTRABAR_TRAILING_STOP"
    assert Decimal(trailing["net_pnl"]) > 0


def test_current_bar_close_cannot_retroactively_change_entry() -> None:
    scenario = qualify(CONTRACT)["scenarios"][
        "CURRENT_BAR_CLOSE_CANNOT_CHANGE_ENTRY_BEFORE_OPEN"
    ]
    assert scenario["entry_execution_price"] == "108"
    assert scenario["fill_count"] == 2
    assert scenario["exit_reason"] == "INTRABAR_HARD_STOP"
