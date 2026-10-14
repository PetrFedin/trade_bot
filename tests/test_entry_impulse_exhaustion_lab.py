"""Impulsive-entry veto research: previous-bar-only and safety regression."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
    PortfolioEntryBlockReason,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy
from tools.entry_impulse_exhaustion_lab import (
    ATR_LOOKBACK,
    HYPOTHESES,
    SIX,
    completed_relative_atr,
    make_prior_impulse_gate,
)

D = Decimal
ZERO = D("0")
START = datetime(2025, 1, 1, tzinfo=UTC)


def _bars(symbol, *, drift=ZERO, size=25):
    result = []
    for index in range(size):
        close = D("100") + drift * index
        result.append(OhlcvBar(
            symbol=symbol,
            timestamp=START + timedelta(days=index),
            open=close,
            high=close + D("1"),
            low=close - D("1"),
            close=close,
            volume=1000,
            trade_count=100,
            vwap=close,
        ))
    return result


def _portfolio(gate=None):
    return CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(top_k=2),
        portfolio_policy=CrossSectionalPortfolioPolicy(
            opening_cash=D("10000"),
            fee_per_fill=D("0"),
            fee_bps_per_fill=D("8"),
            slippage_bps=D("5"),
            maximum_gross_exposure_fraction=D("0.60"),
            new_position_target_equity_fraction=D("0.29"),
        ),
        position_policy=PositionManagementPolicy(),
        reentry_policy=ReentryConfirmationPolicy(
            minimum_consecutive_eligible_bars=2,
        ),
        research_prior_entry_veto=gate,
    )


def _market():
    return [
        *_bars("BTCUSDT", drift=D("1.2")),
        *_bars("ETHUSDT", drift=D("0.6")),
        *_bars("XRPUSDT", drift=D("-0.4")),
    ]


def test_absent_and_permissive_entry_gate_are_exact_original() -> None:
    vanilla = _portfolio().run(_market())
    unchanged = _portfolio(lambda *_: True).run(_market())
    assert vanilla == unchanged
    assert vanilla.closed_trade_count >= 0
    assert "RESEARCH_PRIOR_ENTRY_VETO" not in vanilla.entry_block_counts


def test_all_new_entries_vetoed_without_affecting_selector_or_cash() -> None:
    original = _portfolio().run(_market())
    blocked = _portfolio(lambda *_: False).run(_market())
    assert original.decision_trace[0].selected_symbols == (
        blocked.decision_trace[0].selected_symbols
    )
    assert blocked.decision_trace[0].entered_symbols == ()
    assert blocked.closed_trade_count == 0
    assert blocked.ending_equity == D("10000")
    assert blocked.entry_block_counts[
        PortfolioEntryBlockReason.RESEARCH_PRIOR_ENTRY_VETO.value
    ] > 0
    assert blocked.fill_count == 0


def test_gate_receives_only_completed_bars_and_is_entry_only() -> None:
    seen = []

    def gate(symbol, completed):
        assert all(row.symbol == symbol for row in completed)
        seen.append((symbol, completed[-1].timestamp))
        return symbol != "BTCUSDT"

    result = _portfolio(gate).run(_market())
    assert seen
    assert result.decision_trace[0].selected_symbols[0] == "BTCUSDT"
    assert "BTCUSDT" not in result.decision_trace[0].entered_symbols
    assert "ETHUSDT" in result.decision_trace[0].entered_symbols
    assert len(result.decision_trace) > 1
    assert all(time < result.decision_trace[-1].execution_time for _, time in seen)
    assert all(row.concurrent_positions <= 2 for row in result.decision_trace)


def test_invalid_callable_result_and_invalid_callback_fail_closed() -> None:
    with pytest.raises(ValueError, match="must be callable"):
        _portfolio("INVALID")
    with pytest.raises(ValueError, match="exact boolean"):
        _portfolio(lambda *_: 1).run(_market())


def test_impulse_atr_uses_14_completed_gap_aware_true_ranges() -> None:
    history = _bars("BTCUSDT", size=15)
    assert ATR_LOOKBACK == 14
    assert completed_relative_atr(tuple(history)) == D("0.02")
    changed = history[-1]
    history[-1] = OhlcvBar(
        symbol=changed.symbol,
        timestamp=changed.timestamp,
        open=D("100"),
        high=D("105"),
        low=D("99"),
        close=D("104"),
        volume=1000,
        trade_count=100,
        vwap=D("103"),
    )
    prior = tuple(history)
    assert completed_relative_atr(prior) > D("0.02")
    audit1 = []
    one_day = make_prior_impulse_gate(
        targeted_symbols=("BTCUSDT",),
        hypothesis="PRIOR_1D_ATR_150",
        audit=audit1,
    )
    audit2 = []
    two_day = make_prior_impulse_gate(
        targeted_symbols=("BTCUSDT",),
        hypothesis="PRIOR_2D_ATR_225",
        audit=audit2,
    )
    assert one_day("BTCUSDT", prior) is False
    assert two_day("BTCUSDT", prior) is True
    assert one_day("ETHUSDT", tuple(_bars("ETHUSDT", size=15))) is True
    assert len(audit1) == len(audit2) == 1
    assert audit1[0]["vetoed_new_entry"] is True
    assert audit2[0]["vetoed_new_entry"] is False
    assert all(x["old_position_exit_rules_unchanged"] for x in audit1)


def test_no_atr_reverts_to_default_permit_not_fabricated_history() -> None:
    audit = []
    gate = make_prior_impulse_gate(
        targeted_symbols=("BTCUSDT",),
        hypothesis="PRIOR_1D_ATR_150",
        audit=audit,
    )
    assert gate("BTCUSDT", tuple(_bars("BTCUSDT", size=8))) is True
    assert audit[0]["relative_atr14"] is None
    assert audit[0]["vetoed_new_entry"] is False


def test_invalid_coin_and_unknown_hypothesis_fail_closed() -> None:
    with pytest.raises(ValueError, match="subset"):
        make_prior_impulse_gate(
            targeted_symbols=("NOT_A_COIN",),
            hypothesis="PRIOR_1D_ATR_150",
        )
    with pytest.raises(ValueError, match="duplicate"):
        make_prior_impulse_gate(
            targeted_symbols=("BTCUSDT", "BTCUSDT"),
            hypothesis="PRIOR_1D_ATR_150",
        )
    with pytest.raises(ValueError, match="unknown preregistered"):
        make_prior_impulse_gate(
            targeted_symbols=("BTCUSDT",),
            hypothesis="FITTED_ON_WINNERS",
        )
    assert len(SIX) == 6
    assert len(HYPOTHESES) == 2
