"""Independent cash/event reconstruction; does NOT import the replay or funding book.

Verify raw-file hashes, all 36 predeclared ledgers, every emitted funding event,
trades, each daily cash/holding value, fees, drawdown and reporting clocks.
This is a second arithmetic implementation, not an independent human review.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes

D = Decimal
ZERO = D('0')
CASH = D('10000')
USD_TOLERANCE = D('0.000001')
RATE_TOLERANCE = D('0.0000000001')
HOUR_ROOTS = (
    'bybit_intraday_trade_cases_60m', 'bybit_ambiguous_six_hourly_20261013',
    'bybit_ambiguous_broad_hourly_20261013', 'bybit_hourly_portfolio_stress_20261015',
)


def dt(text):
    stamp = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise ValueError('non-UTC evidence timestamp')
    return stamp.astimezone(UTC)


def near(left, right, tolerance=USD_TOLERANCE):
    a, b = D(left), D(right)
    if not (a.is_finite() and b.is_finite()) or abs(a-b) > tolerance:
        raise ValueError(f'independent ledger mismatch: {a} != {b}')


def read_csv(path):
    with path.open(newline='', encoding='utf-8') as stream:
        return list(csv.DictReader(stream))


def verify(report, snapshot_root):
    payload = {k: v for k, v in report.items() if k != 'evidence_sha256'}
    expected_digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    if expected_digest != report['evidence_sha256']:
        raise ValueError('report content hash mismatch')
    for flag in ('live_trading_allowed', 'strategy_promotion_allowed', 'untouched_holdout',
                 'funding_exact_exchange_timing', 'mark_price_used',
                 'funding_scenario_endpoints_are_global_portfolio_bounds',
                 'intraday_reinvestment', 'margin_or_liquidation_modelled'):
        if report[flag] is not False:
            raise ValueError(f'unverified authority or model scope: {flag}')
    if (report['funding_cash_feedback_modelled'] is not True or
            report['cash_booking_clock'] != 'AFTER_DAILY_TRADES_BEFORE_NEXT_DAY_SIZING'):
        raise ValueError('funding booking-clock contract changed')
    price_root = snapshot_root/'bybit_3y_2023-10-09_2026-10-08'
    funding_root = snapshot_root/'bybit_funding_2023-10-09_2026-10-08'
    for key, checksum in report['source_daily_and_funding_sha256'].items():
        root = price_root if key.startswith('daily/') else funding_root
        path = root/key.split('/', 1)[1]
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError('source bytes no longer match report')
    for name, digests in report['hourly_source_sha256'].items():
        locations = [snapshot_root/root/name for root in HOUR_ROOTS
                     if (snapshot_root/root/name).is_dir()]
        if len(locations) != 1:
            raise ValueError('missing or duplicated hourly proof source')
        symbol = name.split('_')[0]
        paths = {'csv': locations[0]/f'{symbol}_60m.csv',
                 'manifest': locations[0]/'provenance.json'}
        for label, path in paths.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digests[label]:
                raise ValueError('hourly source identity drift')
    prices, funding = {}, {}
    def sources(symbol):
        if symbol not in prices:
            prices[symbol] = {dt(x['timestamp']): D(x['close'])
                              for x in read_csv(price_root/f'{symbol}_D.csv')}
            funding[symbol] = [(dt(x['timestamp']), D(x['funding_rate']))
                               for x in read_csv(funding_root/f'{symbol}_funding.csv')]
        return prices[symbol], funding[symbol]
    periods = ('six_full_history', 'six_matched_window', 'broad21_matched_window')
    expected = {(p, f, s) for p in periods for f, s in (('8', '5'), ('10', '10'))}
    seen = [(x['period_id'], x['fee_bps_per_fill'], x['slippage_bps_per_side'])
            for x in report['comparisons']]
    if len(seen) != 6 or set(seen) != expected:
        raise ValueError('missing, repeated or unregistered study configuration')
    model_keys = {f'{m}:{s}' for m in ('DAILY_STOP_FIRST', 'HOURLY_COLLISION_REFINED')
                  for s in ('NO_FUNDING', 'UPPER_COST', 'LOWER_COST')}
    models_checked = trades_checked = events_checked = days_checked = 0
    matched_dates = set()
    for comparison in report['comparisons']:
        if set(comparison['models']) != model_keys:
            raise ValueError('missing or unexpected funding/exit model')
        start = dt(comparison['first_execution_day_utc'])
        end = dt(comparison['last_source_day_utc'])
        if 'matched_window' in comparison['period_id']:
            matched_dates.add((start, end))
        fees = D(comparison['fee_bps_per_fill'])/D('10000')
        slip = D(comparison['slippage_bps_per_side'])/D('10000')
        for model_key, result in comparison['models'].items():
            scenario = model_key.split(':')[1]
            cash_change = defaultdict(lambda: ZERO)
            quantity_change = defaultdict(lambda: defaultdict(lambda: ZERO))
            funding_expected = {}
            total_fees = total_closed_net = ZERO
            identities = set()
            def funding_for_position(symbol, entered, quantity, entry_price, exited,
                                     intrabar, still_open, *, period_start=start,
                                     period_end=end, cost_scenario=scenario,
                                     event_map=funding_expected):
                _, rate_history = sources(symbol)
                for timestamp, rate in rate_history:
                    if (timestamp < entered or timestamp < period_start
                            or timestamp > period_end+timedelta(days=1)):
                        continue
                    if still_open:
                        eligible = timestamp < period_end+timedelta(days=1)
                        uncertain = timestamp == entered
                    elif intrabar:
                        eligible = timestamp < exited+timedelta(days=1)
                        uncertain = timestamp == entered or timestamp >= exited
                    else:
                        eligible = timestamp <= exited
                        uncertain = timestamp == entered or timestamp == exited
                    if not eligible:
                        continue
                    cost = quantity*entry_price*rate
                    if uncertain:
                        cost = max(ZERO, cost) if cost_scenario == 'UPPER_COST' else min(ZERO, cost)
                    identity = f'research-funding:{symbol}:{timestamp.isoformat()}'
                    if identity in event_map:
                        raise ValueError('overlapping reconstructed position funding')
                    event_map[identity] = (timestamp, symbol, -cost, rate, quantity,
                                                  entry_price, uncertain)
            for trade in result['trades']:
                symbol = trade['symbol']
                entry, exit_ = dt(trade['entry_time']), dt(trade['exit_time'])
                identity = (symbol, entry)
                if identity in identities or not start <= entry <= exit_ <= end:
                    raise ValueError('duplicate trade identity or trade outside study')
                identities.add(identity)
                quantity = D(trade['quantity'])
                ep, xp = D(trade['entry_execution_price']), D(trade['exit_execution_price'])
                ef, xf = D(trade['entry_fee']), D(trade['exit_fee'])
                near(ep, D(trade['entry_reference_price'])*(D('1')+slip))
                near(xp, D(trade['exit_reference_price'])*(D('1')-slip))
                near(ef, quantity*ep*fees)
                near(xf, quantity*xp*fees)
                net = quantity*(xp-ep)-ef-xf
                near(net, trade['net_pnl'])
                near(D(trade['gross_pnl_before_costs'])-D(trade['slippage_cost'])-ef-xf, net)
                total_closed_net += net
                total_fees += ef+xf
                cash_change[entry] -= quantity*ep+ef
                cash_change[exit_] += quantity*xp-xf
                quantity_change[entry][symbol] += quantity
                quantity_change[exit_][symbol] -= quantity
                if scenario != 'NO_FUNDING':
                    funding_for_position(symbol, entry, quantity, ep, exit_,
                                         trade['exit_reason'].startswith('INTRABAR_'), False)
                trades_checked += 1
            for held in result['final_open_positions']:
                symbol, entry = held['symbol'], dt(held['entry_time'])
                quantity, ep, ef = (D(held[k]) for k in (
                    'quantity', 'entry_execution_price', 'entry_fee'))
                if (symbol, entry) in identities or not start <= entry <= end:
                    raise ValueError('duplicate final holding or holding outside study')
                total_fees += ef
                near(ef, quantity*ep*fees)
                cash_change[entry] -= quantity*ep+ef
                quantity_change[entry][symbol] += quantity
                near(held['final_mark'], sources(symbol)[0][end])
                if scenario != 'NO_FUNDING':
                    funding_for_position(symbol, entry, quantity, ep, end, False, True)
            output_events = {x['activity_id']: x for x in result['funding_events']}
            if (len(output_events) != len(result['funding_events'])
                    or set(output_events) != set(funding_expected)):
                raise ValueError('funding event population incomplete or duplicated')
            funding_by_day = defaultdict(lambda: ZERO)
            for identity, event in funding_expected.items():
                stamp, symbol, amount, rate, qty, ep, uncertain = event
                row = output_events[identity]
                near(row['cash_amount_usd'], amount)
                near(row['provider_rate'], rate, RATE_TOLERANCE)
                near(row['quantity'], qty)
                near(row['entry_execution_price'], ep)
                if (dt(row['settlement_time']) != stamp
                        or row['participation_uncertain'] != uncertain or row['symbol'] != symbol):
                    raise ValueError('funding event has incorrect participation or symbol')
                day = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
                if dt(row['booked_after_day_utc']) != day+timedelta(days=1):
                    raise ValueError('funding applied to wrong daily cash cycle')
                funding_by_day[day] += amount
                events_checked += 1
            if scenario != 'NO_FUNDING':
                if len(result['funding_days']) != (end-start).days+1:
                    raise ValueError('daily funding audit has missing dates')
                for i, summary in enumerate(result['funding_days']):
                    day = start+timedelta(days=i)
                    if dt(summary['day_utc']) != day:
                        raise ValueError('daily funding audit has duplicate or misplaced date')
                    near(summary['net_funding_cash_usd'], funding_by_day[day])
            elif result['funding_days']:
                raise ValueError('no-funding control unexpectedly has funding ledger rows')
            if model_key.startswith('HOURLY_COLLISION_REFINED'):
                for item in result['collision_audit']:
                    name = item['symbol']+'_'+dt(item['day_utc']).date().isoformat()
                    locations = [snapshot_root/root/name for root in HOUR_ROOTS
                                 if (snapshot_root/root/name).is_dir()]
                    if len(locations) != 1:
                        raise ValueError('refined collision has no unique hourly proof')
                    source = locations[0]/(item['symbol']+'_60m.csv')
                    if hashlib.sha256(source.read_bytes()).hexdigest() != item['source_sha256']:
                        raise ValueError('refined collision proof hash mismatch')
                    protective = D(item['daily_reference_price'])
                    target = D(item['target'])
                    found = False
                    for hour in read_csv(source):
                        opening, high, low = (D(hour[k]) for k in ('open', 'high', 'low'))
                        if opening <= protective:
                            status, price = 'PROTECTIVE_FIRST_HOURLY', opening
                        elif opening >= target:
                            status, price = 'TARGET_FIRST_HOURLY', target
                        elif low <= protective:
                            status = ('UNRESOLVED_SAME_HOUR_STOP_FIRST' if high >= target
                                      else 'PROTECTIVE_FIRST_HOURLY')
                            price = protective
                        elif high >= target:
                            status, price = 'TARGET_FIRST_HOURLY', target
                        else:
                            continue
                        if (item['status'] != status
                                or dt(item['first_touch_hour']) != dt(hour['timestamp'])):
                            raise ValueError('independent first-touch ordering differs from report')
                        near(item['refined_reference_price'], price)
                        found = True
                        break
                    if not found:
                        raise ValueError('reported hourly threshold touch does not exist')
                if sum(result['collision_counts'].values()) != len(result['collision_audit']):
                    raise ValueError('collision denominator mismatch')
            equity_rows = result['daily_equity']
            if len(equity_rows) != (end-start).days+1:
                raise ValueError('daily ledger has missing observations')
            cash, holdings, peak, drawdown = CASH, defaultdict(lambda: ZERO), CASH, ZERO
            previous_equity = CASH
            for index, row in enumerate(equity_rows):
                day = start+timedelta(days=index)
                if dt(row['day_utc']) != day:
                    raise ValueError('daily ledger reordered or missing')
                near(row['prior_equity'], previous_equity)
                cash += cash_change[day]+funding_by_day[day]
                for symbol, change in quantity_change[day].items():
                    holdings[symbol] += change
                    if holdings[symbol] < 0:
                        raise ValueError('negative holdings in independent ledger')
                if cash < 0:
                    raise ValueError('negative cash in independent daily ledger')
                mark = sum((qty*sources(symbol)[0][day] for symbol, qty in holdings.items()
                            if qty), ZERO)
                equity = cash+mark
                near(row['closing_equity'], equity)
                peak = max(peak, equity)
                drawdown = max(drawdown, (peak-equity)/peak)
                previous_equity = equity
                days_checked += 1
            total_funding = sum(funding_by_day.values(), ZERO)
            metrics = result['metrics']
            near(metrics['ending_cash_usd'], cash)
            near(metrics['ending_equity_usd'], previous_equity)
            near(metrics['return_including_daily_funding'],
                 (previous_equity-CASH)/CASH, RATE_TOLERANCE)
            near(metrics['funding_cash_net_usd'], total_funding)
            near(metrics['closed_trade_net_excluding_funding_usd'], total_closed_net)
            near(metrics['execution_fees_usd'], total_fees)
            near(metrics['daily_close_max_drawdown_over_peak'], drawdown, RATE_TOLERANCE)
            near(metrics['funding_credits_usd'],
                 sum((max(x[2], ZERO) for x in funding_expected.values()), ZERO))
            near(metrics['funding_debits_usd'],
                 -sum((min(x[2], ZERO) for x in funding_expected.values()), ZERO))
            if metrics['closed_trades'] != len(result['trades']):
                raise ValueError('trade count does not match')
            if metrics['fills'] != 2*len(result['trades'])+len(result['final_open_positions']):
                raise ValueError('fill count incomplete')
            models_checked += 1
    if len(matched_dates) != 1:
        raise ValueError('purported matched-universe comparison has different dates')
    return {
        'arithmetic_verification': 'PASS',
        'configurations': 6, 'model_replays': models_checked,
        'closed_trades_checked': trades_checked, 'funding_events_checked': events_checked,
        'daily_equity_rows_checked': days_checked,
        'matched_dates_verified': True,
        'verification_method': 'second implementation from raw rate CSV, trades and daily prices',
        'independent_human_or_agent_review': False,
        'execution_or_venue_account_truth_verified': False,
        'live_trading_allowed': False,
        'evidence_sha256': report['evidence_sha256'],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--snapshot-root', type=Path, default=Path('research/snapshots'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = verify(json.loads(args.report.read_text()), args.snapshot_root)
    text = json.dumps(result, indent=2)
    print(text)
    if args.output:
        args.output.write_text(text+'\n')


if __name__ == '__main__':
    main()
