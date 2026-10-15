"""Funding cash feedback, signed credits and deterministic clock safeguards."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.ohlcv import OhlcvBar
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
    PortfolioExitReason,
    PortfolioTrade,
    ResearchDailyFundingContext,
    ResearchFundingCashflow,
    ResearchFundingPosition,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from tools.daily_funding_cash_replay import DailyFundingBook
from tools.historical_portfolio_funding import (
    FundingOpenPosition,
    FundingSettlement,
    funding_bounds_for_trades,
)

D = Decimal
ZERO = D('0')
MINUS_ONE = D('-1')
DAY = datetime(2025, 1, 9, tzinfo=UTC)


def schedule(rate='0.001'):
    return {'BTCUSDT': tuple(FundingSettlement(DAY+timedelta(hours=h), D(rate))
                             for h in (0, 8, 16))}


def position(entry=None):
    return ResearchFundingPosition('BTCUSDT', entry or DAY-timedelta(days=1), D('2'), D('100'))


def context(entry=None):
    return ResearchDailyFundingContext(DAY, (), (position(entry),))


def closed(reason=PortfolioExitReason.INTRABAR_HARD_STOP, entry=None):
    return PortfolioTrade(
        symbol='BTCUSDT', entry_time=entry or DAY-timedelta(days=1), exit_time=DAY,
        entry_reference_price=D('100'), exit_reference_price=D('98'),
        entry_execution_price=D('100'), exit_execution_price=D('98'),
        entry_fee=ZERO, exit_fee=ZERO, slippage_cost=ZERO,
        gross_pnl_before_costs=D('-4'), quantity=D('2'), net_pnl=D('-4'),
        holding_bars=1, exit_reason=reason,
    )


def amount(rows):
    return sum((x.amount for x in rows), ZERO)


def test_definite_negative_funding_is_cash_income_not_zero_clipped():
    events = schedule('-0.001')
    for mode in ('UPPER_COST', 'LOWER_COST'):
        book = DailyFundingBook(events, mode)
        assert amount(book(context())) == D('0.6')
        assert all(not x['participation_uncertain'] for x in book.audit)
    # Preserve the old result as a regression witness rather than rewrite history.
    legacy = funding_bounds_for_trades((), events, open_positions=(FundingOpenPosition(
        'BTCUSDT', DAY-timedelta(days=1), DAY+timedelta(days=1), D('2'), D('100'),
    ),))
    assert legacy.lower_cost == legacy.upper_cost == ZERO


def test_new_entry_at_settlement_boundary_is_uncertain_not_double_charged():
    high = DailyFundingBook(schedule(), 'UPPER_COST')
    low = DailyFundingBook(schedule(), 'LOWER_COST')
    assert amount(high(context(DAY))) == D('-0.6')
    assert amount(low(context(DAY))) == D('-0.4')
    assert sum(x['participation_uncertain'] for x in high.audit) == 1


def test_positive_negative_ambiguous_costs_are_not_blindly_assumed_paid():
    ctx = ResearchDailyFundingContext(DAY, (closed(),), ())
    assert amount(DailyFundingBook(schedule(), 'UPPER_COST')(ctx)) == D('-0.6')
    assert amount(DailyFundingBook(schedule(), 'LOWER_COST')(ctx)) == ZERO
    assert amount(DailyFundingBook(schedule('-0.001'), 'UPPER_COST')(ctx)) == ZERO
    assert amount(DailyFundingBook(schedule('-0.001'), 'LOWER_COST')(ctx)) == D('0.6')


def test_open_exit_cannot_accrue_later_same_day_funding():
    ctx = ResearchDailyFundingContext(DAY, (closed(PortfolioExitReason.SELECTION_EXIT),), ())
    book = DailyFundingBook(schedule(), 'UPPER_COST')
    assert amount(book(ctx)) == D('-0.2')
    assert len(book.audit) == 1
    assert book.audit[0]['settlement_time'] == DAY.isoformat()


def test_final_open_day_has_no_future_midnight_funding():
    original = schedule()
    original['BTCUSDT'] += (FundingSettlement(DAY+timedelta(days=1), D('0.1')),)
    rows = DailyFundingBook(original, 'UPPER_COST')(context())
    assert amount(rows) == D('-0.6')
    assert all(row.settlement_time < DAY+timedelta(days=1) for row in rows)


def test_context_clock_duplicates_missing_data_and_unqualified_symbols_fail_closed():
    book = DailyFundingBook(schedule(), 'UPPER_COST')
    book(context())
    with pytest.raises(ValueError, match='duplicated, reordered or skipped'):
        book(context())
    with pytest.raises(ValueError, match='missing funding day'):
        DailyFundingBook(schedule(), 'UPPER_COST')(
            ResearchDailyFundingContext(DAY+timedelta(days=1), (), (position(),)))
    with pytest.raises(ValueError, match='lacks a funding source'):
        DailyFundingBook(schedule(), 'UPPER_COST')(
            ResearchDailyFundingContext(DAY, (), (replace(position(), symbol='ETHUSDT'),)))
    with pytest.raises(ValueError, match='multiple same-symbol'):
        DailyFundingBook(schedule(), 'UPPER_COST')(
            ResearchDailyFundingContext(DAY, (closed(),), (position(),)))
    with pytest.raises(ValueError, match='unknown predeclared'):
        DailyFundingBook(schedule(), 'OPTIMIZED')
    with pytest.raises(ValueError, match='duplicate funding event'):
        DailyFundingBook({'BTCUSDT': (schedule()['BTCUSDT'][0],)*2}, 'UPPER_COST')


def market():
    bars = []
    for symbol, step in (('BTCUSDT', D('1')), ('ETHUSDT', D('0.5'))):
        for i in range(20):
            price = D('100')+step*i
            bars.append(OhlcvBar(symbol, DAY-timedelta(days=8)+timedelta(days=i),
                                 price, price+D('0.2'), price-D('0.2'), price, 1000, 1))
    return bars


def engine():
    return CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(top_k=2),
        portfolio_policy=CrossSectionalPortfolioPolicy(fee_per_fill=ZERO),
    )


def one_flow(ctx, amount=MINUS_ONE):
    name = (ctx.open_positions[0].symbol if ctx.open_positions else ctx.closed_today[0].symbol)
    time = ctx.day_utc+timedelta(hours=8)
    return ResearchFundingCashflow(
        f'research-funding:{name}:{time.isoformat()}', name, time, amount)


def test_none_and_empty_funding_callback_preserve_complete_original_result():
    base = engine().run(market())
    assert engine().run(market(), research_daily_funding=lambda _: ()) == base


def test_daily_funding_changes_later_sizes_and_equity_but_not_today_entry():
    original = engine().run(market())
    funded = engine().run(
        market(), research_daily_funding=lambda ctx: (one_flow(ctx),)
        if ctx.open_positions or ctx.closed_today else (),
    )
    assert funded.ending_equity < original.ending_equity
    assert funded.closed_trades[0].quantity == original.closed_trades[0].quantity
    # The second generation of positions is still open at the sample end.
    assert funded.final_quantities != original.final_quantities
    assert all(funded.final_quantities[s] < original.final_quantities[s]
               for s in original.final_quantities)
    assert [x.selected_symbols for x in funded.decision_trace] == [
        x.selected_symbols for x in original.decision_trace]
    assert funded.decision_trace[0].equity_at_prior_close == D('10000')
    assert funded.decision_trace[0].closing_equity == (
        original.decision_trace[0].closing_equity-D('1'))


@pytest.mark.parametrize('mode,pattern', [
    ('duplicate', 'duplicate funding'), ('future', 'outside actual'),
    ('unknown', 'outside actual'), ('nan', 'finite Decimal'),
    ('zero', 'nonzero'), ('badid', 'not canonical'), ('not_tuple', 'immutable tuple'),
])
def test_malformed_funding_never_modifies_research_result(mode, pattern):
    def bad(ctx):
        if not (ctx.closed_today or ctx.open_positions):
            return ()
        row = one_flow(ctx)
        return {
            'duplicate': (row, row),
            'future': (replace(row, settlement_time=ctx.day_utc+timedelta(days=2)),),
            'unknown': (replace(row, symbol='FAKE'),),
            'nan': (replace(row, amount=D('NaN')),),
            'zero': (replace(row, amount=ZERO),),
            'badid': (replace(row, activity_id='arbitrary-deposit'),),
            'not_tuple': [row],
        }[mode]
    with pytest.raises(ValueError, match=pattern):
        engine().run(market(), research_daily_funding=bad)


def test_funding_cannot_hide_insufficient_cash_or_reclassify_it_as_a_deposit():
    with pytest.raises(ValueError, match='NEGATIVE_CASH_AFTER_ADJUSTMENT'):
        engine().run(market(), research_daily_funding=lambda ctx: (one_flow(ctx, D('-100000')),))
    with pytest.raises(ValueError, match='must be callable'):
        engine().run(market(), research_daily_funding='INVALID')


def test_discontinuous_or_non_utc_daily_clock_rejected():
    bars = [b for b in market() if b.timestamp != DAY]
    with pytest.raises(ValueError, match='contiguous UTC daily bars'):
        engine().run(bars, research_daily_funding=lambda _: ())


def test_raw_funding_manifest_missing_coverage_and_integrity(tmp_path):
    import json
    import shutil
    from pathlib import Path

    from tools.daily_funding_cash_replay import load_signed_funding

    root = Path('research/snapshots/bybit_funding_2023-10-09_2026-10-08')
    for name in ('BTCUSDT_funding.csv', 'BTCUSDT_funding.manifest.json'):
        shutil.copyfile(root/name, tmp_path/name)
    begin = datetime(2023, 10, 9, tzinfo=UTC)
    finish = datetime(2026, 10, 8, tzinfo=UTC)
    result, hashes = load_signed_funding(tmp_path, ('BTCUSDT',), begin, finish)
    assert len(result['BTCUSDT']) == 3289
    assert len(hashes) == 2
    with pytest.raises(ValueError, match='does not span'):
        load_signed_funding(tmp_path, ('BTCUSDT',), begin, finish+timedelta(days=1))
    manifest = tmp_path/'BTCUSDT_funding.manifest.json'
    data = json.loads(manifest.read_text())
    data['symbol'] = 'ETHUSDT'
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match='checksum, category or symbol'):
        load_signed_funding(tmp_path, ('BTCUSDT',), begin, finish)
    shutil.copyfile(root/'BTCUSDT_funding.manifest.json', manifest)
    path = tmp_path/'BTCUSDT_funding.csv'
    path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(ValueError, match='checksum, category or symbol'):
        load_signed_funding(tmp_path, ('BTCUSDT',), begin, finish)
