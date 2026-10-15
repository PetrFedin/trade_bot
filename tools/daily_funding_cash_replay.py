"""Research daily funding cash feedback; not venue-exact futures accounting.

Recompute the entire portfolio after each day's modelled funding debit/credit.
Settlement timestamps and signed rates come from the retained provider archive.
Notional stays the entry notional (NO archived mark prices), and unknown entry/
exit-time participation is represented by two explicitly labelled scenarios.
Cashflows are booked AFTER the daily trading loop, before next-day sizing.
No intraday financing, margin/liquidation, or untouched-holdout claim is made.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

from app.qualification.portable_artifact_codec import canonical_json_bytes
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioBacktester,
    CrossSectionalPortfolioPolicy,
    PortfolioExitReason,
    ResearchDailyFundingContext,
    ResearchFundingCashflow,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelector
from app.strategy.reentry_confirmation import ReentryConfirmationPolicy
from tools.historical_ambiguity_census import BROAD_21, SIX_MAJOR
from tools.historical_portfolio_evidence import _common_timestamps
from tools.historical_portfolio_funding import FundingSettlement
from tools.portfolio_hourly_replay import (
    SOURCE_ROOTS,
    FrozenSelection,
    HourlyCollisionResolver,
    digest,
    load_hourly_cases,
)

D = Decimal
ZERO = D('0')
CASH = D('10000')
COSTS = ((D('8'), D('5')), (D('10'), D('10')))
SCENARIOS = ('UPPER_COST', 'LOWER_COST')
INTRABAR = {
    PortfolioExitReason.INTRABAR_HARD_STOP,
    PortfolioExitReason.INTRABAR_TRAILING_STOP,
    PortfolioExitReason.INTRABAR_TAKE_PROFIT,
}


def utc(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError('funding timestamps must explicitly identify UTC')
    return parsed.astimezone(UTC)


def load_signed_funding(root: Path, symbols, start, end):
    """Verify retained bytes and row identities, not upstream authenticity."""
    schedules, fingerprints = {}, {}
    for symbol in symbols:
        source = root / f'{symbol}_funding.csv'
        manifest = root / f'{symbol}_funding.manifest.json'
        if source.is_symlink() or manifest.is_symlink():
            raise ValueError('funding source symlinks forbidden')
        meta = json.loads(manifest.read_text(encoding='utf-8'))
        if (meta.get('symbol') != symbol or meta.get('category') != 'linear'
                or meta.get('dataset_sha256') != digest(source)):
            raise ValueError('funding source checksum, category or symbol mismatch')
        with source.open(newline='', encoding='utf-8') as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != ['timestamp', 'symbol', 'funding_rate']:
                raise ValueError('unexpected raw funding schema')
            values = []
            for row in reader:
                stamp, rate = utc(row['timestamp']), D(row['funding_rate'])
                if row['symbol'] != symbol or not rate.is_finite():
                    raise ValueError('funding has wrong instrument or nonfinite rate')
                if values and (stamp <= values[-1].timestamp
                               or stamp - values[-1].timestamp > timedelta(days=1)):
                    raise ValueError('duplicate, reordered or >24h funding gap')
                values.append(FundingSettlement(stamp, rate))
        if not values or len(values) != meta.get('settlement_count'):
            raise ValueError('funding record count differs from retained manifest')
        if (values[0].timestamp != utc(meta['first_settlement'])
                or values[-1].timestamp != utc(meta['last_settlement'])):
            raise ValueError('funding manifest boundary mismatch')
        if values[0].timestamp > start or values[-1].timestamp < end + timedelta(days=1):
            raise ValueError('funding does not span the complete requested period')
        schedules[symbol] = tuple(values)
        fingerprints[source.name] = digest(source)
        fingerprints[manifest.name] = digest(manifest)
    return schedules, fingerprints


class DailyFundingBook:
    """Signed per-settlement scenarios booked at each DAILY close.

    UPPER_COST includes ambiguous positive payments, excludes ambiguous credits.
    LOWER_COST includes ambiguous credits, excludes ambiguous positive payments.
    Definite payments AND definite credits are always kept (no zero-clipping).
    They are scenario endpoints for a *fixed notional*. After cash feedback,
    their total portfolio returns are NOT mathematical or statistical bounds.
    """

    def __init__(self, schedules, scenario: str):
        if scenario not in SCENARIOS:
            raise ValueError('unknown predeclared funding scenario')
        indexed = {}
        for symbol, events in schedules.items():
            previous = None
            for event in events:
                if (event.timestamp.tzinfo is None
                        or event.timestamp.utcoffset() != timedelta(0)
                        or not event.rate.is_finite()
                        or (previous is not None and event.timestamp <= previous)):
                    raise ValueError('invalid or duplicate funding event')
                previous = event.timestamp
                day = event.timestamp.replace(hour=0, minute=0, second=0, microsecond=0)
                indexed.setdefault((symbol, day), []).append(event)
        self.events = MappingProxyType({k: tuple(v) for k, v in indexed.items()})
        self.symbols = frozenset(schedules)
        self.scenario = scenario
        self.audit: list[dict] = []
        self.daily_audit: list[dict] = []
        self.last_day = None

    def __call__(self, ctx: ResearchDailyFundingContext):
        day = ctx.day_utc
        if (day.tzinfo is None or day.utcoffset() != timedelta(0)
                or (day.hour, day.minute, day.second, day.microsecond) != (0, 0, 0, 0)):
            raise ValueError('funding context requires UTC midnight')
        if self.last_day is not None and day != self.last_day + timedelta(days=1):
            raise ValueError('funding day duplicated, reordered or skipped')
        exposures = []
        for trade in ctx.closed_today:
            if trade.exit_time != day or trade.entry_time > day:
                raise ValueError('closed trade belongs to a different day')
            exposures.append((trade.symbol, trade.entry_time, trade.quantity,
                              trade.entry_execution_price,
                              'INTRABAR_EXIT' if trade.exit_reason in INTRABAR else 'OPEN_EXIT'))
        for position in ctx.open_positions:
            if position.entry_time > day:
                raise ValueError('future open-position entry')
            exposures.append((position.symbol, position.entry_time, position.quantity,
                              position.entry_execution_price, 'OPEN_AT_CLOSE'))
        names = [x[0] for x in exposures]
        if len(names) != len(set(names)):
            raise ValueError('multiple same-symbol intraday positions are not qualified')
        flows, pending = [], []
        for symbol, entered, quantity, entry_price, state in sorted(exposures):
            if symbol not in self.symbols:
                raise ValueError('exposed instrument lacks a funding source')
            if (not quantity.is_finite() or quantity <= 0
                    or not entry_price.is_finite() or entry_price <= 0):
                raise ValueError('funding notional must be finite and positive')
            daily_events = self.events.get((symbol, day))
            if not daily_events:
                raise ValueError(f'missing funding day for held instrument: {symbol} {day}')
            for event in daily_events:
                if state == 'OPEN_EXIT' and event.timestamp != day:
                    continue
                uncertain = state != 'OPEN_AT_CLOSE' or event.timestamp == entered
                cost = quantity * entry_price * event.rate
                chosen = (max(cost, ZERO) if self.scenario == 'UPPER_COST'
                          else min(cost, ZERO)) if uncertain else cost
                amount = -chosen
                identity = f'research-funding:{symbol}:{event.timestamp.isoformat()}'
                pending.append({
                    'activity_id': identity, 'symbol': symbol,
                    'settlement_time': event.timestamp.isoformat(),
                    'booked_after_day_utc': (day + timedelta(days=1)).isoformat(),
                    'entry_time': entered.isoformat(), 'holding_state': state,
                    'quantity': str(quantity), 'entry_execution_price': str(entry_price),
                    'provider_rate': str(event.rate), 'participation_uncertain': uncertain,
                    'cost_if_held_usd': str(cost), 'cash_amount_usd': str(amount),
                    'mark_price_verified': False,
                })
                if amount != ZERO:
                    flows.append(ResearchFundingCashflow(identity, symbol, event.timestamp, amount))
        self.audit.extend(pending)
        self.daily_audit.append({
            'day_utc': day.isoformat(),
            'net_funding_cash_usd': str(sum((x.amount for x in flows), ZERO)),
            'nonzero_flow_count': len(flows),
            'uncertain_event_count': sum(x['participation_uncertain'] for x in pending),
        })
        self.last_day = day
        return tuple(flows)


def replay(bars, fees, slip, selector=None, resolver=None, funding_book=None):
    return CrossSectionalPortfolioBacktester(
        selector=CrossSectionalSelector(top_k=2) if selector is None else selector,
        portfolio_policy=CrossSectionalPortfolioPolicy(
            opening_cash=CASH, fee_per_fill=ZERO,
            fee_bps_per_fill=fees, slippage_bps=slip,
            maximum_gross_exposure_fraction=D('0.60'),
            new_position_target_equity_fraction=D('0.29'),
        ),
        reentry_policy=ReentryConfirmationPolicy(minimum_consecutive_eligible_bars=2),
    ).run(bars, research_collision_resolver=resolver, research_daily_funding=funding_book)


def make_result(result, sync, fees, slip, book, resolver):
    """Money reconciles against executions, final holdings and signed funding."""
    funding_cash = sum((D(row['cash_amount_usd']) for row in book.audit), ZERO) if book else ZERO
    open_positions = []
    for symbol, quantity in result.final_quantities.items():
        if quantity <= 0:
            continue
        trace = next(x for x in reversed(result.decision_trace) if symbol in x.entered_symbols)
        price = sync[symbol][trace.execution_index].open * (D('1') + slip / D('10000'))
        entry_fee = price * quantity * fees / D('10000')
        open_positions.append({
            'symbol': symbol, 'entry_time': trace.execution_time.isoformat(),
            'quantity': str(quantity), 'entry_execution_price': str(price),
            'entry_fee': str(entry_fee), 'final_mark': str(sync[symbol][-1].close),
        })
    closed_net = sum((t.net_pnl for t in result.closed_trades), ZERO)
    open_cost = sum((D(x['quantity'])*D(x['entry_execution_price'])+D(x['entry_fee'])
                     for x in open_positions), ZERO)
    open_value = sum((D(x['quantity'])*D(x['final_mark']) for x in open_positions), ZERO)
    ending_cash = CASH + closed_net + funding_cash - open_cost
    if abs(ending_cash + open_value - result.ending_equity) > D('0.000001'):
        raise ValueError('cash + marked holdings != ending equity')
    peak, relative_dd = CASH, ZERO
    for day in result.decision_trace:
        peak = max(peak, day.closing_equity)
        relative_dd = max(relative_dd, (peak-day.closing_equity)/peak)
    collision_counts = (dict(sorted(Counter(x['status'] for x in resolver.audit).items()))
                        if resolver else {})
    return {
        'metrics': {
            'return_including_daily_funding': str(result.total_return),
            'ending_equity_usd': str(result.ending_equity),
            'ending_cash_usd': str(ending_cash),
            'funding_cash_net_usd': str(funding_cash),
            'funding_debits_usd': str(-sum((min(D(x['cash_amount_usd']), ZERO)
                                           for x in book.audit), ZERO)) if book else '0',
            'funding_credits_usd': str(sum((max(D(x['cash_amount_usd']), ZERO)
                                          for x in book.audit), ZERO)) if book else '0',
            'closed_trade_net_excluding_funding_usd': str(closed_net),
            'open_cost_usd': str(open_cost), 'open_marked_value_usd': str(open_value),
            'execution_fees_usd': str(result.fees_paid),
            'closed_trades': result.closed_trade_count, 'fills': result.fill_count,
            'daily_close_max_drawdown_over_peak': str(relative_dd),
            'turnover_over_starting_cash': str(result.turnover_fraction),
            'remaining_ambiguous_daily_exits': sum(t.ambiguous_intrabar_exit
                                                 for t in result.closed_trades),
        },
        'all_reached_collisions_sourced': 'MISSING_SOURCE_STOP_FIRST' not in collision_counts,
        'collision_counts': collision_counts,
        'collision_audit': resolver.audit if resolver else [],
        'trades': [asdict(t) for t in result.closed_trades],
        'final_open_positions': open_positions,
        'daily_equity': [{'day_utc': t.execution_time.isoformat(),
                          'prior_equity': str(t.equity_at_prior_close),
                          'closing_equity': str(t.closing_equity),
                          'selected_symbols': list(t.selected_symbols),
                          'entered_symbols': list(t.entered_symbols)}
                         for t in result.decision_trace],
        'funding_events': book.audit if book else [],
        'funding_days': book.daily_audit if book else [],
    }


def run_study(root: Path) -> dict:
    bars_dir = root/'bybit_3y_2023-10-09_2026-10-08'
    funding_dir = root/'bybit_funding_2023-10-09_2026-10-08'
    hourly = load_hourly_cases(bars_dir, tuple(root/s for s in SOURCE_ROOTS))
    all_loaded, common_dates = _common_timestamps([bars_dir/f'{s}_D.csv' for s in BROAD_21])
    six_loaded, six_dates = _common_timestamps([bars_dir/f'{s}_D.csv' for s in SIX_MAJOR])
    raw_common_dates = common_dates
    funding_ends = {
        symbol: utc(json.loads((funding_dir/f'{symbol}_funding.manifest.json').read_text())[
            'last_settlement'
        ]) for symbol in BROAD_21
    }
    last_common_funding_observation = min(funding_ends.values())
    common_dates = tuple(
        day for day in common_dates if day+timedelta(days=1) <= last_common_funding_observation
    )
    if not common_dates:
        raise ValueError('no fully funding-covered matched daily window')
    coverage_restriction = {
        'rule': 'retain a day only when all funding sources extend through its next UTC midnight',
        'original_common_price_end': raw_common_dates[-1].isoformat(),
        'last_common_funding_observation': last_common_funding_observation.isoformat(),
        'limiting_symbols': [s for s, t in funding_ends.items()
                             if t == last_common_funding_observation],
        'excluded_price_dates': [d.isoformat() for d in raw_common_dates if d not in common_dates],
        'matched_last_daily_bar': common_dates[-1].isoformat(),
    }
    periods = (
        ('six_full_history', SIX_MAJOR, six_loaded, six_dates),
        ('six_matched_window', SIX_MAJOR, all_loaded, common_dates),
        ('broad21_matched_window', BROAD_21, all_loaded, common_dates),
    )
    comparisons, source_hashes = [], {}
    for period, symbols, loaded, dates in periods:
        if any(b-a != timedelta(days=1) for a, b in zip(dates, dates[1:], strict=False)):
            raise ValueError('noncontiguous common historical window')
        wanted = set(dates)
        sync = {s: tuple(b for b in loaded[s] if b.timestamp in wanted) for s in symbols}
        bars = [b for s in symbols for b in sync[s]]
        schedules, funding_hashes = load_signed_funding(
            funding_dir, symbols, dates[0], dates[-1],
        )
        source_hashes.update({f'funding/{k}': v for k, v in funding_hashes.items()})
        source_hashes.update({f'daily/{s}_D.csv': digest(bars_dir/f'{s}_D.csv') for s in symbols})
        print('FUNDING_START', period, dates[0].date(), dates[-1].date(), flush=True)
        original = replay(bars, *COSTS[0])
        if period == 'six_full_history':
            if (original.closed_trade_count != 256
                    or abs(original.total_return-D('0.007515525902184908204853186')) > D('1e-12')):
                raise ValueError('unmodified Strategy A historical control drifted')
        selector = FrozenSelection(original.decision_trace, symbols)
        if replay(bars, *COSTS[0], selector=selector) != original:
            raise ValueError('frozen selection cache does not reproduce full control')
        for fees, slip in COSTS:
            row = {
                'period_id': period, 'symbols': list(symbols),
                'first_source_day_utc': dates[0].isoformat(),
                'last_source_day_utc': dates[-1].isoformat(),
                'first_execution_day_utc': original.decision_trace[0].execution_time.isoformat(),
                'fee_bps_per_fill': str(fees), 'slippage_bps_per_side': str(slip),
                'models': {},
            }
            for model in ('DAILY_STOP_FIRST', 'HOURLY_COLLISION_REFINED'):
                for scenario in ('NO_FUNDING', *SCENARIOS):
                    book = (None if scenario == 'NO_FUNDING'
                            else DailyFundingBook(schedules, scenario))
                    resolver = (HourlyCollisionResolver(hourly)
                                if model == 'HOURLY_COLLISION_REFINED' else None)
                    result = replay(bars, fees, slip, selector, resolver, book)
                    record = make_result(result, sync, fees, slip, book, resolver)
                    row['models'][f'{model}:{scenario}'] = record
                    print('FUNDED_PORTFOLIO', json.dumps({
                        'period': period, 'fees': str(fees), 'slippage': str(slip),
                        'model': model, 'scenario': scenario, **record['metrics'],
                        'hourly_missing': not record['all_reached_collisions_sourced'],
                    }), flush=True)
            comparisons.append(row)
    payload = {
        'schema_version': 'astra-daily-funding-cash-feedback-v1',
        'evaluation_class': 'REUSED_HISTORY_DAILY_CASH_FEEDBACK_NOT_VENUE_SETTLEMENT',
        'research_only': True, 'strategy_promotion_allowed': False,
        'live_trading_allowed': False, 'untouched_holdout': False,
        'funding_cash_feedback_modelled': True,
        'funding_exact_exchange_timing': False,
        'mark_price_used': False,
        'notional_model': 'CONSTANT_ENTRY_EXECUTION_NOTIONAL',
        'quote_currency': 'USDT',
        'fx_or_stablecoin_depeg_modelled': False,
        'cash_booking_clock': 'AFTER_DAILY_TRADES_BEFORE_NEXT_DAY_SIZING',
        'funding_scenario_endpoints_are_global_portfolio_bounds': False,
        'funding_same_hour_exit_refinement_used': False,
        'intraday_reinvestment': False, 'margin_or_liquidation_modelled': False,
        'matched_period_coverage_policy': coverage_restriction,
        'source_daily_and_funding_sha256': source_hashes,
        'hourly_cases_in_archive': len(hourly),
        'hourly_source_sha256': {
            f'{s}_{t.date()}': {'csv': v.source_sha256, 'manifest': v.provenance_sha256}
            for (s, t), v in sorted(hourly.items())
        },
        'comparisons': comparisons,
        'limitations': [
            'Daily cash timing approximates exchange settlement; no intraday solvency proof',
            'Funding uses entry notional, not unobserved historical venue mark prices',
            'Entry/exit boundary participation is unknown; both signed scenarios are retained',
            'Scenario endpoints are not global mathematical bounds after capital feedback',
            'Hourly sources refine reached daily collisions only, never the daily trailing policy',
            'Stop/target cost basis includes entry fees as in the unchanged baseline',
            'Missing or unresolved hourly cases remain stop-first and explicitly counted',
            'Cash plus fully paid spot-like holdings is a research proxy, '
            'not a derivatives margin ledger',
            'Daily-close drawdown omits intraday losses; fees and slippage are fixed hypotheses',
            'Only the two matched-window universes share identical dates',
            'Inspected historical data and many prior trials do not establish '
            'future expected returns',
        ],
    }
    serializable = json.loads(json.dumps(payload, default=str))
    serializable['evidence_sha256'] = hashlib.sha256(
        canonical_json_bytes(serializable)
    ).hexdigest()
    return serializable


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot-root', type=Path, default=Path('research/snapshots'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = run_study(args.snapshot_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True)+'\n', encoding='utf-8')
    print('DAILY_FUNDING_EVIDENCE', report['evidence_sha256'], flush=True)


if __name__ == '__main__':
    main()
