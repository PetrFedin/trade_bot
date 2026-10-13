"""No-lookahead, entry-freeze and replay-parity safeguards for ATR research."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.ohlcv_exit import evaluate_long_intrabar_exit
from app.strategy.position_management import PositionManagementPolicy
from tools.atr_entry_frozen_lab import (
    ATR_ARMS,
    ATR_LOOKBACK,
    AtrEntryProfile,
    FrozenHistoricalSelections,
    atr_fraction_from_completed,
    fixed_entry_policy,
)

D = Decimal
START = datetime(2026, 1, 1, tzinfo=UTC)


def _series(symbol: str, *, drift: Decimal, n: int = 30):
    result = []
    for index in range(n):
        price = D("100") + drift * index
        result.append(OhlcvBar(
            symbol=symbol,
            timestamp=START + timedelta(days=index),
            open=price,
            high=price + D("1"),
            low=price - D("1"),
            close=price,
            volume=10000,
            trade_count=100,
            vwap=price,
        ))
    return result


def _portfolio(*, selector=None, resolver=None):
    return CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(top_k=2) if selector is None else selector,
        portfolio_policy=CrossSectionalPortfolioPolicy(
            opening_cash=D("10000"),
            fee_per_fill=D("0"),
            fee_bps_per_fill=D("8"),
            slippage_bps=D("5"),
            maximum_gross_exposure_fraction=D("0.60"),
            new_position_target_equity_fraction=D("0.29"),
        ),
        position_policy=PositionManagementPolicy(),
        research_entry_policy_resolver=resolver,
    )


def _universe(n=30):
    return [
        *_series("BTCUSDT", drift=D("1.2"), n=n),
        *_series("ETHUSDT", drift=D("0.7"), n=n),
        *_series("DOGEUSDT", drift=D("-0.4"), n=n),
    ]


def test_atr_uses_only_completed_symbol_bars_and_previous_close() -> None:
    history = tuple(_series("BTCUSDT", drift=D("0"), n=15))
    assert ATR_LOOKBACK == 14
    fraction = atr_fraction_from_completed(history)
    assert fraction == D("0.02")
    compact, observed = fixed_entry_policy(
        completed_bars=history, profile=AtrEntryProfile(D("0.50"), D("1.0"))
    )
    assert observed == D("0.02")
    assert compact.stop_loss_fraction == D("0.01")
    assert compact.take_profit_fraction == D("0.02")
    extended, _ = fixed_entry_policy(
        completed_bars=history, profile=AtrEntryProfile(D("0.75"), D("1.5"))
    )
    assert extended.stop_loss_fraction == D("0.015")
    assert extended.take_profit_fraction == D("0.03")


def test_missing_history_and_future_extreme_cannot_change_prior_atr() -> None:
    completed = tuple(_series("BTCUSDT", drift=D("0"), n=15))
    assert atr_fraction_from_completed(completed[:14]) is None
    before = atr_fraction_from_completed(completed)
    future = OhlcvBar(
        symbol="BTCUSDT",
        timestamp=START + timedelta(days=15),
        open=D("100"),
        high=D("500"),
        low=D("1"),
        close=D("110"),
        volume=10000,
        trade_count=100,
        vwap=D("110"),
    )
    assert atr_fraction_from_completed(completed) == before
    assert atr_fraction_from_completed((*completed, future)) > before
    original, factor = fixed_entry_policy(
        completed_bars=completed[:14],
        profile=AtrEntryProfile(D("1"), D("2")),
    )
    assert original == PositionManagementPolicy()
    assert factor is None


def test_profile_and_chronological_contracts_fail_closed() -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        AtrEntryProfile(D("NaN"), D("1")).validate()
    with pytest.raises(ValueError, match="positive and finite"):
        AtrEntryProfile(D("1"), D("0")).validate()
    with pytest.raises(ValueError, match="strictly chronological"):
        atr_fraction_from_completed(tuple(_series("BTCUSDT", drift=D("0"), n=15))[::-1])
    with pytest.raises(ValueError, match="exactly one symbol"):
        x = tuple(_series("BTCUSDT", drift=D("0"), n=14))
        atr_fraction_from_completed((*x, _series("ETHUSDT", drift=D("0"), n=1)[0]))
    with pytest.raises(ValueError, match="ATR periods"):
        atr_fraction_from_completed((), periods=1)
    assert len(ATR_ARMS) == 2


def test_frozen_selection_cache_is_identical_to_regular_portfolio() -> None:
    bars = _universe()
    original = _portfolio().run(bars)
    cached = FrozenHistoricalSelections(
        original.decision_trace,
        symbols=("BTCUSDT", "DOGEUSDT", "ETHUSDT"),
    )
    replayed = _portfolio(selector=cached).run(bars)
    assert original == replayed
    with pytest.raises(ValueError, match="unregistered decision"):
        cached.select(_universe(n=6))
    with pytest.raises(ValueError, match="incomplete or altered"):
        cached.select(_universe(n=10))


def test_entry_policy_is_frozen_across_later_market_bars() -> None:
    calls = []
    seen = []

    def resolver(symbol, completed):
        assert all(x.symbol == symbol for x in completed)
        assert completed[-1].timestamp < START + timedelta(days=30)
        calls.append((symbol, completed[-1].timestamp))
        if symbol == "BTCUSDT":
            return PositionManagementPolicy(
                stop_loss_fraction=D("0.04"),
                take_profit_fraction=D("0.80"),
                trailing_activation_fraction=D("0.90"),
                maximum_holding_bars=5,
            )
        return PositionManagementPolicy()

    def watched(*, average_cost, bar, state, policy):
        if bar.symbol == "BTCUSDT":
            seen.append(policy)
        return evaluate_long_intrabar_exit(
            average_cost=average_cost, bar=bar, state=state, policy=policy
        )

    with patch(
        "app.strategy.cross_sectional_portfolio.evaluate_long_intrabar_exit",
        side_effect=watched,
    ):
        result = _portfolio(resolver=resolver).run(_universe())
    assert result.closed_trade_count > 0
    assert len(seen) >= 3
    assert all(p.stop_loss_fraction == D("0.04") for p in seen)
    assert len([s for s, _ in calls if s == "BTCUSDT"]) < len(seen)


def test_invalid_resolver_never_enters_a_position() -> None:
    with pytest.raises(ValueError, match="conflicts"):
        CrossSectionalPortfolioBacktester(
            selector=CrossSectionalSelector(top_k=2),
            position_policies_by_symbol={"BTCUSDT": PositionManagementPolicy()},
            research_entry_policy_resolver=lambda *_: PositionManagementPolicy(),
        )
    with pytest.raises(ValueError, match="must return PositionManagementPolicy"):
        _portfolio(resolver=lambda *_: None).run(_universe())
