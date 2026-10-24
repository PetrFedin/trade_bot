"""Shared-capital, long-only historical research for fixed ASTRA candidate rules.

One USD 10,000 cash ledger across all coins. Completed prior-day signals,
next-open buys, two slots, 60% entry-time gross exposure cap and 29% sizing.
Intrabar stop/target collision is stop-first; funding and venue fills unknown.
This does not alter Strategy A, OMS, permissions, or executable strategies.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from tools.independent_coin_strategy_lab import (
    ARMS,
    BROAD21,
    COSTS,
    SIX,
    WARMUP,
    Candle,
    _exit_reference,
    canonical,
    read_candles,
    signal,
)

D = Decimal
ZERO = D("0")
ONE = D("1")
INITIAL_CASH = D("10000")
TARGET_ALLOCATION = D("0.29")
MAX_ENTRY_EXPOSURE = D("0.60")
MIN_ENTRY_ALLOCATION = D("0.05")
MAX_SLOTS = 2
DEFAULT_FEE_BPS = D("8")
DEFAULT_SLIPPAGE_BPS = D("5")
BPS = D("10000")
RANKERS = ("SYMBOL_ASC", "PREVIOUS_20D_RETURN")
CANDIDATES = tuple(a for a in ARMS if a != "BUY_HOLD_29PC")
BENCHMARK = "BUY_HOLD_BTC_ETH"
STUDIES = CANDIDATES + (BENCHMARK,)
ENTRY_GATES = ("NONE", "BTC_SMA50_BLOCK", "BTC_VOLATILITY_100_BLOCK")


class SharedCapitalError(ValueError):
    """Reject inconsistent research, cash accounting or future-data inputs."""


def _validate(data: dict[str, list[Candle]]) -> tuple:
    if not data or tuple(data) != tuple(sorted(data)):
        raise SharedCapitalError("shared-capital universe must be sorted")
    if not set(data).issubset(BROAD21) or not {"BTCUSDT", "ETHUSDT"} <= set(data):
        raise SharedCapitalError("registered symbols and benchmark are required")
    timeline = tuple(bar.day for bar in next(iter(data.values())))
    if len(timeline) < WARMUP + 5:
        raise SharedCapitalError("insufficient synchronized UTC history")
    for symbol, bars in data.items():
        if tuple(bar.day for bar in bars) != timeline:
            raise SharedCapitalError(f"{symbol}: unsynchronized history")
    if any(b - a != timedelta(days=1) for a, b in zip(timeline, timeline[1:], strict=False)):
        raise SharedCapitalError("shared-capital UTC time spine has a gap")
    return timeline


def _priority(
    data: dict[str, list[Candle]], index: int, symbol: str, ranker: str
) -> tuple:
    if ranker == "SYMBOL_ASC":
        return (symbol,)
    history = data[symbol][:index]
    # Both prices are known at the end of the PRIOR UTC day.
    past_return = history[-1].close / history[-21].close - ONE
    return (-past_return, symbol)


def _signal(
    data: dict[str, list[Candle]], index: int, symbol: str, arm: str
) -> bool:
    if arm == BENCHMARK:
        return index == WARMUP and symbol in ("BTCUSDT", "ETHUSDT")
    return signal(data[symbol], index, arm)


def _reject_btc_entry(data: dict[str, list[Candle]], index: int, gate: str) -> bool:
    """Optional long-entry safety study, using BTC closes only through T-1."""
    if gate not in ENTRY_GATES:
        raise SharedCapitalError("unregistered BTC research entry gate")
    if gate == "NONE":
        return False
    if index < WARMUP:
        raise SharedCapitalError("BTC prior-day research warmup missing")
    past = data["BTCUSDT"][:index]
    if gate == "BTC_SMA50_BLOCK":
        sma50 = sum((bar.close for bar in past[-50:]), ZERO) / 50
        return past[-1].close < sma50
    # Twenty complete daily close returns, annualized with 365 UTC days.
    changes = [
        past[j].close / past[j - 1].close - ONE
        for j in range(len(past) - 20, len(past))
    ]
    mean = sum(changes, ZERO) / 20
    variance = sum(((value - mean) ** 2 for value in changes), ZERO) / 20
    volatility = (max(ZERO, variance) * D("365")).sqrt()
    return volatility > ONE


def replay_shared(
    data: dict[str, list[Candle]],
    arm: str,
    *,
    fee_bps: D = DEFAULT_FEE_BPS,
    slippage_bps: D = DEFAULT_SLIPPAGE_BPS,
    ranker: str = "SYMBOL_ASC",
    finalize: bool = True,
    entry_gate: str = "NONE",
) -> dict:
    """Replay one common capital book; never assume same-day exit cash at open."""
    days = _validate(data)
    if arm not in STUDIES or ranker not in RANKERS or entry_gate not in ENTRY_GATES:
        raise SharedCapitalError("unsupported historical arm/ranker")
    if arm == BENCHMARK and entry_gate != "NONE":
        raise SharedCapitalError("fixed benchmark cannot be retrospectively gated")
    if arm == BENCHMARK and ranker != "SYMBOL_ASC":
        raise SharedCapitalError("benchmark has one fixed symbol selection")
    fee, slip = fee_bps / BPS, slippage_bps / BPS
    if not ZERO <= fee <= D("0.01") or not ZERO <= slip <= D("0.01"):
        raise SharedCapitalError("unqualified cost assumption")
    cash = INITIAL_CASH
    positions: dict[str, dict] = {}
    trades: list[dict] = []
    decisions: list[dict] = []
    marks: list[dict] = []
    blocks: Counter[str] = Counter()
    fees_paid = ZERO
    slippage_paid = ZERO
    max_entry_exposure = ZERO
    maximum_daily_close_exposure = ZERO
    over_cap_closing_days = 0
    total_position_days = 0
    for index in range(WARMUP, len(days)):
        day = days[index]
        opening_market_value = sum(
            (position["quantity"] * data[symbol][index].open
             for symbol, position in positions.items()),
            ZERO,
        )
        opening_equity = cash + opening_market_value
        if opening_equity <= ZERO:
            raise SharedCapitalError("insolvent unleveraged simulated account")
        available = [
            symbol for symbol in data
            if _signal(data, index, symbol, arm) and symbol not in positions
        ]
        priority = sorted(
            available, key=lambda s: _priority(data, index, s, ranker)
        )
        entries: list[str] = []
        rejected: dict[str, str] = {}
        gross = opening_market_value
        gate_blocked = bool(priority) and _reject_btc_entry(data, index, entry_gate)
        for symbol in priority:
            if gate_blocked:
                reason = "BTC_PRIOR_CLOSE_ENTRY_GATE"
            elif len(positions) >= MAX_SLOTS:
                reason = "MAX_TWO_POSITIONS"
            elif gross >= opening_equity * MAX_ENTRY_EXPOSURE:
                reason = "ENTRY_EXPOSURE_CAP"
            else:
                # Never re-use proceeds of same-day exits to fund open entries.
                allowance = min(
                    opening_equity * TARGET_ALLOCATION,
                    opening_equity * MAX_ENTRY_EXPOSURE - gross,
                    cash,
                )
                reason = (
                    "MIN_ALLOCATION_NOT_REACHED"
                    if allowance < opening_equity * MIN_ENTRY_ALLOCATION else ""
                )
            if reason:
                rejected[symbol] = reason
                blocks[reason] += 1
                continue
            reference = data[symbol][index].open
            execution = reference * (ONE + slip)
            quantity = allowance / (execution * (ONE + fee))
            if quantity <= ZERO:
                raise SharedCapitalError("nonpositive model allocation")
            cost = quantity * execution
            paid_fee = cost * fee
            cash -= cost + paid_fee
            if cash < -D("0.00000001"):
                raise SharedCapitalError("negative funding cash")
            cash = max(ZERO, cash)
            gross += quantity * reference
            actual_entry_exposure = gross / opening_equity
            if actual_entry_exposure > MAX_ENTRY_EXPOSURE + D("0.000000001"):
                raise SharedCapitalError("entry exposure cap exceeded")
            max_entry_exposure = max(max_entry_exposure, actual_entry_exposure)
            positions[symbol] = {
                "entry_index": index, "entry_utc": day.isoformat(),
                "entry_reference": reference, "entry_execution": execution,
                "entry_fee": paid_fee, "quantity": quantity,
            }
            fees_paid += paid_fee
            slippage_paid += quantity * (execution - reference)
            entries.append(symbol)
        # Protective orders and time exits are evaluated AFTER opening buys.
        # This avoids predicting same-day exits or recycling their future cash.
        exits: list[str] = []
        for symbol in sorted(tuple(positions)):
            candle = data[symbol][index]
            position = positions[symbol]
            exit_ref = (
                None if arm == BENCHMARK
                else _exit_reference(position, candle, index, arm)
            )
            if index == len(days) - 1 and finalize and exit_ref is None:
                exit_ref = (candle.close, "ARCHIVE_END_CLOSE")
            if exit_ref is None:
                continue
            reference, reason = exit_ref
            execution = reference * (ONE - slip)
            qty = position["quantity"]
            exit_fee = qty * execution * fee
            gross_gain = qty * (reference - position["entry_reference"])
            slips = qty * (
                position["entry_execution"] - position["entry_reference"]
                + reference - execution
            )
            fees = position["entry_fee"] + exit_fee
            net_gain = gross_gain - slips - fees
            direct = qty * execution - exit_fee
            direct -= qty * position["entry_execution"] + position["entry_fee"]
            if abs(net_gain - direct) > D("0.000000001"):
                raise SharedCapitalError("independent trade cashflow differs")
            cash += qty * execution - exit_fee
            fees_paid += exit_fee
            slippage_paid += qty * (reference - execution)
            trades.append({
                "symbol": symbol,
                "entry_utc": position["entry_utc"],
                "exit_utc": day.isoformat(),
                "quantity_model": str(qty),
                "gross_usdt": str(gross_gain),
                "fees_usdt": str(fees),
                "slippage_usdt": str(slips),
                "net_usdt": str(net_gain),
                "exit_reason": reason,
            })
            exits.append(symbol)
            del positions[symbol]
        if cash < -D("0.00000001"):
            raise SharedCapitalError("negative model cash after exits")
        close_gross = sum(
            (position["quantity"] * data[symbol][index].close
             for symbol, position in positions.items()),
            ZERO,
        )
        equity = cash + close_gross
        if equity <= ZERO:
            raise SharedCapitalError("unleveraged daily close equity exhausted")
        close_exposure = close_gross / equity
        maximum_daily_close_exposure = max(
            maximum_daily_close_exposure, close_exposure
        )
        if close_exposure > MAX_ENTRY_EXPOSURE:
            # Natural post-entry price moves are not a new order violation.
            over_cap_closing_days += 1
        total_position_days += len(positions)
        if len(positions) > MAX_SLOTS:
            raise SharedCapitalError("more than two simultaneously held coins")
        decisions.append({
            "day": day.isoformat(), "ranked_prior_day_signals": priority,
            "entered": entries, "exited": exits, "entry_rejections": rejected,
            "opening_equity": str(opening_equity),
            "opening_cash_before_entries": str(
                opening_equity - opening_market_value
            ),
        })
        marks.append({
            "day": day.isoformat(), "equity": str(equity),
            "free_cash": str(cash), "gross_close_value": str(close_gross),
            "closing_gross_exposure_fraction": str(close_exposure),
            "open_symbols": sorted(positions),
        })
    if finalize and positions:
        raise SharedCapitalError("archive-end portfolio not flat")
    net_closed = sum((D(t["net_usdt"]) for t in trades), ZERO)
    last_equity = D(marks[-1]["equity"])
    if finalize and abs(
        (last_equity - INITIAL_CASH) - net_closed
    ) > D("0.000001") * max(1, len(trades)):
        raise SharedCapitalError("cash ledger does not equal closed trade net")
    peak = INITIAL_CASH
    max_drawdown = ZERO
    for row in marks:
        equity = D(row["equity"])
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)
    result = {
        "arm": arm, "entry_ranker": ranker,
        "fee_bps_per_fill": str(fee_bps),
        "slippage_bps_per_fill": str(slippage_bps),
        "starting_cash_usdt": str(INITIAL_CASH),
        "ending_equity_usdt": str(last_equity),
        "return_pct": str((last_equity / INITIAL_CASH - ONE) * 100),
        "max_daily_close_drawdown_pct": str(max_drawdown * 100),
        "max_entry_exposure_fraction": str(max_entry_exposure),
        "max_daily_close_exposure_fraction": str(
            maximum_daily_close_exposure
        ),
        "closing_days_above_entry_cap": over_cap_closing_days,
        "average_open_positions": str(D(total_position_days) / len(marks)),
        "closed_trade_count": len(trades),
        "wins": sum(D(t["net_usdt"]) > ZERO for t in trades),
        "losses": sum(D(t["net_usdt"]) < ZERO for t in trades),
        "entry_block_counts": dict(sorted(blocks.items())),
        "gross_fee_paid_usdt": str(fees_paid),
        "gross_slippage_paid_usdt": str(slippage_paid),
        "closed_trade_net_usdt": str(net_closed),
        "closed_trades": trades,
        "decision_trace": decisions,
        "daily_equity": marks,
        "terminal_open_positions": sorted(positions),
        "historical_funding_included": False,
        "historical_orderbook_included": False,
        "unseen_holdout": False,
        "live_trading_allowed": False,
        "strategy_promotion_allowed": False,
    }
    if entry_gate != "NONE":
        result["entry_gate_policy"] = entry_gate
    return result


def study_shared(
    archive: Path, *, symbols: tuple[str, ...] = BROAD21
) -> dict:
    if tuple(sorted(set(symbols))) != symbols or not set(SIX) <= set(symbols):
        raise SharedCapitalError("must include sorted six majors")
    if not set(symbols).issubset(BROAD21):
        raise SharedCapitalError("unregistered historical instruments")
    raw = {
        symbol: read_candles(archive / f"{symbol}_D.csv", symbol)
        for symbol in symbols
    }
    shared = sorted(set.intersection(*(
        {bar.day for bar in rows} for rows in raw.values()
    )))
    if len(shared) < 450:
        raise SharedCapitalError("insufficient synchronized historical dataset")
    accepted = set(shared)
    matched = {
        sym: [bar for bar in raw[sym] if bar.day in accepted]
        for sym in symbols
    }
    if any(tuple(bar.day for bar in bars) != tuple(shared)
           for bars in matched.values()):
        raise SharedCapitalError("inconsistent instrument chronology")
    _validate(matched)
    records = []
    for label, allowed in (("SIX", SIX), ("BROAD21", BROAD21)):
        if not set(allowed).issubset(symbols):
            continue
        cohort = {symbol: matched[symbol] for symbol in allowed}
        for arm in STUDIES:
            rankers = ("SYMBOL_ASC",) if arm == BENCHMARK else RANKERS
            for ranker in rankers:
                for fee, slip in COSTS:
                    model = replay_shared(
                        cohort, arm, ranker=ranker,
                        fee_bps=fee, slippage_bps=slip,
                    )
                    records.append({
                        "universe": label, "symbols": list(allowed),
                        **model,
                    })
    expected = 36 if set(BROAD21) <= set(symbols) else 18
    if len(records) != expected:
        raise SharedCapitalError("incomplete predefined capital stress matrix")
    source_hashes = {
        f"{symbol}_D.csv": hashlib.sha256(
            (archive / f"{symbol}_D.csv").read_bytes()
        ).hexdigest()
        for symbol in symbols
    }
    report = {
        "schema_version": "astra-shared-capital-candidate-study-v1",
        "evaluation_class": "REUSED_INSPECTED_DAILY_HISTORY_SHARED_CASH",
        "research_only": True,
        "source_sha256": source_hashes,
        "symbols": list(symbols), "shared_days": len(shared),
        "first_utc": shared[0].isoformat(),
        "last_utc": shared[-1].isoformat(),
        "scenario_count": len(records),
        "initial_shared_cash_usdt": str(INITIAL_CASH),
        "max_simultaneous_positions": MAX_SLOTS,
        "max_new_position_opening_equity_fraction": str(
            TARGET_ALLOCATION
        ),
        "new_entry_exposure_cap_fraction": str(MAX_ENTRY_EXPOSURE),
        "ranking_policies_fixed_before_results": list(RANKERS),
        "pre_registered_arms": list(STUDIES),
        "fee_slippage_scenarios": [[str(f), str(s)] for f, s in COSTS],
        "strategies_reoptimized_on_test_period": False,
        "current_orderbook_mixed_into_history": False,
        "actual_fee_tier_verified": False,
        "funding_settlement_verified": False,
        "true_market_fills_verified": False,
        "unseen_holdout": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "profitability_proven": False,
        "scenarios": records,
        "scope": (
            "36 shared-cash scenarios, same 981 inspected UTC days. "
            "Nine predeclared arm/rank configurations x two cost models "
            "x two universes. Daily OHLC stop-first, fractional quantities, "
            "cash ledger, no borrow. Live/demo routing not implemented."
        ),
    }
    report["report_sha256"] = hashlib.sha256(canonical(report)).hexdigest()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = study_shared(args.bars_dir)
    from tools.publish_research_receipt import publish_new_json_receipt

    publish_new_json_receipt(args.output, result)
    for row in result["scenarios"]:
        print("SHARED_CAPITAL_SCENARIO", json.dumps({
            "universe": row["universe"], "arm": row["arm"],
            "ranker": row["entry_ranker"],
            "cost": [row["fee_bps_per_fill"], row["slippage_bps_per_fill"]],
            "return_pct": row["return_pct"],
            "max_drawdown_pct": row["max_daily_close_drawdown_pct"],
            "trades": row["closed_trade_count"],
            "entry_blocks": row["entry_block_counts"],
        }, sort_keys=True))
    print("SHARED_REPORT_SHA256", result["report_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
