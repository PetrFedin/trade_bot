"""Frozen independent per-coin research strategies on inspected daily history.

Long-only, unlevered, price-only 29%-cash allocation. Signal at completed
day T, entry at next daily OPEN. Daily stop/target ambiguity = STOP FIRST.
Fees and slippage are hypothetical; no funding, venue depth, or liquidation.
Each symbol has its own separate $10k account. NEVER sum as a portfolio.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

D = Decimal
ZERO = D("0")
ONE = D("1")
BPS = D("10000")
START_CAPITAL = D("10000")
POSITION_FRACTION = D("0.29")
STOP_FRACTION = D("0.03")
TARGET_FRACTION = D("0.06")
MAX_HOLDING_DAYS = 8
WARMUP = 51
ARMS = ("BREAKOUT", "IMPULSE", "TREND_PULLBACK", "MEAN_REVERSION", "BUY_HOLD_29PC")
COSTS = ((D("8"), D("5")), (D("10"), D("10")))
SIX = ("BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
BROAD21 = (
    "ADAUSDT", "APTUSDT", "ARBUSDT", "ATOMUSDT", "AVAXUSDT",
    "BNBUSDT", "BTCUSDT", "DOGEUSDT", "DOTUSDT", "ETHUSDT",
    "FILUSDT", "INJUSDT", "LINKUSDT", "LTCUSDT", "NEARUSDT",
    "OPUSDT", "SOLUSDT", "SUIUSDT", "TONUSDT", "TRXUSDT", "XRPUSDT",
)


class StrategyStudyError(ValueError):
    """Reject invalid source, future-data leaks or broken PnL bookkeeping."""


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        allow_nan=False, ensure_ascii=True,
    ).encode("ascii")


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def number(text: object, label: str, *, positive: bool = False) -> D:
    try:
        result = D(str(text))
    except (ValueError, TypeError) as exc:
        raise StrategyStudyError(f"{label}: invalid decimal") from exc
    if not result.is_finite() or (positive and result <= ZERO):
        raise StrategyStudyError(f"{label}: invalid/nonpositive value")
    return result


@dataclass(frozen=True)
class Candle:
    day: datetime
    open: D
    high: D
    low: D
    close: D
    volume: D


def read_candles(path: Path, symbol: str) -> list[Candle]:
    if not path.is_file() or path.is_symlink():
        raise StrategyStudyError(f"missing or symlinked archive: {symbol}")
    bars = []
    with path.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if row.get("symbol") != symbol:
                raise StrategyStudyError("CSV symbol differs from requested instrument")
            try:
                day = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            except (KeyError, ValueError) as exc:
                raise StrategyStudyError("invalid UTC candle timestamp") from exc
            if day.utcoffset() != timedelta(0) or day.time().isoformat() != "00:00:00":
                raise StrategyStudyError("historical candles require UTC midnight")
            bar = Candle(
                day.astimezone(UTC),
                number(row["open"], "open", positive=True),
                number(row["high"], "high", positive=True),
                number(row["low"], "low", positive=True),
                number(row["close"], "close", positive=True),
                number(row["volume"], "volume"),
            )
            if (
                bar.high < max(bar.open, bar.close)
                or bar.low > min(bar.open, bar.close)
                or bar.volume < ZERO
                or (bars and bar.day <= bars[-1].day)
            ):
                raise StrategyStudyError("invalid or unsorted historical candle")
            bars.append(bar)
    if len(bars) < 450:
        raise StrategyStudyError(f"insufficient historical research: {symbol}")
    return bars


def signal(bars: list[Candle], index: int, arm: str) -> bool:
    """Use bars[:index] ONLY, never entry-day high, low or close."""
    if arm not in ARMS or index < WARMUP:
        return False
    if arm == "BUY_HOLD_29PC":
        return index == WARMUP
    past = bars[:index]
    close = past[-1].close
    sma50 = sum((bar.close for bar in past[-50:]), ZERO) / 50
    sma20 = sum((bar.close for bar in past[-20:]), ZERO) / 20
    sma10 = sum((bar.close for bar in past[-10:]), ZERO) / 10
    if arm == "BREAKOUT":
        return (
            close > max(bar.high for bar in past[-21:-1])
            and sma10 > sma50
            and past[-1].volume > (
                sum((bar.volume for bar in past[-21:-1]), ZERO) / 20
            )
        )
    if arm == "IMPULSE":
        return close / past[-2].close - ONE >= D("0.04") and close > sma50
    if arm == "TREND_PULLBACK":
        return (
            close > sma50 and close < sma10 * D("0.98")
            and close < past[-2].close
        )
    return close < sma20 * D("0.94") and close < past[-2].close


def _exit_reference(position: dict, candle: Candle, index: int, arm: str):
    if arm == "BUY_HOLD_29PC":
        return None
    stop = position["entry_reference"] * (ONE - STOP_FRACTION)
    target = position["entry_reference"] * (ONE + TARGET_FRACTION)
    if candle.open <= stop:
        return candle.open, "GAP_STOP"
    if candle.open >= target:
        return candle.open, "GAP_TARGET"
    if candle.low <= stop:
        return stop, "STOP_FIRST" if candle.high >= target else "STOP"
    if candle.high >= target:
        return target, "TARGET"
    if index - position["entry_index"] >= MAX_HOLDING_DAYS:
        return candle.close, "TIME_EXIT"
    return None


def run_strategy(
    bars: list[Candle], arm: str, *, fee_bps: D, slip_bps: D,
    finalize: bool = True,
) -> dict:
    if arm not in ARMS or len(bars) < WARMUP + 3:
        raise StrategyStudyError("unknown or insufficient strategy research")
    fee = number(fee_bps, "fee") / BPS
    slip = number(slip_bps, "slippage") / BPS
    if not ZERO <= fee <= D("0.01") or not ZERO <= slip <= D("0.01"):
        raise StrategyStudyError("unreasonable execution cost hypothesis")
    cash = START_CAPITAL
    position = None
    trades = []
    equity_curve = []
    fees_paid, slip_costs = ZERO, ZERO
    decisions = []
    for index in range(WARMUP, len(bars)):
        bar = bars[index]
        candidate = signal(bars, index, arm)
        was_open = position is not None
        if not was_open and candidate:
            reference = bar.open
            executable = reference * (ONE + slip)
            budget = cash * POSITION_FRACTION
            quantity = budget / (executable * (ONE + fee))
            if quantity <= ZERO:
                raise StrategyStudyError("nonpositive model quantity")
            entry_fee = quantity * executable * fee
            cash -= quantity * executable + entry_fee
            position = {
                "entry_index": index, "entry_utc": bar.day.isoformat(),
                "entry_reference": reference, "entry_execution": executable,
                "entry_fee": entry_fee, "qty": quantity,
            }
            fees_paid += entry_fee
            slip_costs += quantity * (executable - reference)
        if position is not None:
            exit_condition = _exit_reference(position, bar, index, arm)
            if index == len(bars) - 1 and finalize and exit_condition is None:
                exit_condition = bar.close, "ARCHIVE_END_CLOSE"
            if exit_condition is not None:
                reference, reason = exit_condition
                executable = reference * (ONE - slip)
                qty = position["qty"]
                exit_fee = qty * executable * fee
                cash += qty * executable - exit_fee
                fees_paid += exit_fee
                slip_costs += qty * (reference - executable)
                gross = qty * (reference - position["entry_reference"])
                slip_trade = qty * (
                    position["entry_execution"] - position["entry_reference"]
                    + reference - executable
                )
                net = gross - slip_trade - position["entry_fee"] - exit_fee
                if abs(
                    net - (qty * executable - exit_fee -
                           qty * position["entry_execution"] - position["entry_fee"])
                ) > D("0.000000001"):
                    raise StrategyStudyError("trade PnL accounting mismatch")
                trades.append({
                    "entry_utc": position["entry_utc"],
                    "exit_utc": bar.day.isoformat(),
                    "entry_price": str(position["entry_execution"]),
                    "exit_price": str(executable),
                    "gross_usdt": str(gross),
                    "slippage_usdt": str(slip_trade),
                    "fees_usdt": str(position["entry_fee"] + exit_fee),
                    "net_usdt": str(net),
                    "reason": reason,
                    "quantity_fractional_model": str(qty),
                })
                position = None
        equity = cash + (position["qty"] * bar.close if position else ZERO)
        if equity <= ZERO:
            raise StrategyStudyError("unlevered research portfolio exhausted equity")
        equity_curve.append({"day": bar.day.isoformat(), "equity": str(equity)})
        decisions.append({
            "day": bar.day.isoformat(),
            "signal_from_prior_close": candidate,
            "entered": candidate and not was_open,
        })
    if finalize and position is not None:
        raise StrategyStudyError("archive end must be flat after close")
    closed = sum((D(t["net_usdt"]) for t in trades), ZERO)
    if finalize and abs(cash - START_CAPITAL - closed) > D("0.000001"):
        raise StrategyStudyError("starting cash to realized trade PnL broken")
    peak = START_CAPITAL
    worst_drawdown = ZERO
    for mark in equity_curve:
        value = D(mark["equity"])
        peak = max(peak, value)
        worst_drawdown = max(worst_drawdown, (peak - value) / peak)
    wins = sum(D(t["net_usdt"]) > ZERO for t in trades)
    losses = sum(D(t["net_usdt"]) < ZERO for t in trades)
    return {
        "arm": arm,
        "fees_bps_per_fill": str(fee_bps),
        "slippage_bps_per_fill": str(slip_bps),
        "start_capital_usdt": str(START_CAPITAL),
        "ending_equity_usdt": str(D(equity_curve[-1]["equity"])),
        "return_pct": str(
            (D(equity_curve[-1]["equity"]) / START_CAPITAL - ONE) * 100
        ),
        "max_daily_close_drawdown_pct": str(worst_drawdown * 100),
        "fees_paid_usdt": str(fees_paid),
        "slippage_paid_usdt": str(slip_costs),
        "closed_trade_net_usdt": str(closed),
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "exit_reason_counts": {
            reason: sum(t["reason"] == reason for t in trades)
            for reason in sorted({t["reason"] for t in trades})
        },
        "trade_tape": trades,
        "decision_tape": decisions,
        "equity_curve": equity_curve,
        "unrealized_pnl_included_in_terminal_equity": not finalize,
        "funding_accounted": False,
        "orderbook_history_available": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
    }


def _rank_summary(rows: list[dict]) -> dict:
    # Independent coin accounts, NOT a shared capital portfolio.
    returns = [D(row["return_pct"]) for row in rows]
    return {
        "independent_symbol_accounts": len(rows),
        "positive_symbols": sum(value > ZERO for value in returns),
        "negative_symbols": sum(value < ZERO for value in returns),
        "mean_independent_return_pct_not_portfolio": str(
            sum(returns, ZERO) / len(rows)
        ),
        "minimum_coin_return_pct": str(min(returns)),
        "maximum_coin_return_pct": str(max(returns)),
    }


def study(directory: Path, symbols: tuple[str, ...] = BROAD21) -> dict:
    if not symbols or tuple(sorted(set(symbols))) != symbols:
        raise StrategyStudyError("symbols must be sorted unique nonempty")
    if any(sym not in BROAD21 for sym in symbols):
        raise StrategyStudyError("unregistered research coin")
    archive_paths = [directory / f"{sym}_D.csv" for sym in symbols]
    all_bars = {
        sym: read_candles(path, sym)
        for sym, path in zip(symbols, archive_paths, strict=True)
    }
    common = set.intersection(*(
        set(b.day for b in rows) for rows in all_bars.values()
    ))
    timeline = sorted(common)
    if len(timeline) < 450:
        raise StrategyStudyError("insufficient coin-universe overlap")
    if any(
        b - a != timedelta(days=1)
        for a, b in zip(timeline, timeline[1:], strict=False)
    ):
        raise StrategyStudyError("gap in matched historical research timeline")
    accepted = set(timeline)
    rows = []
    for symbol in symbols:
        bars = [bar for bar in all_bars[symbol] if bar.day in accepted]
        if [bar.day for bar in bars] != timeline:
            raise StrategyStudyError("cross-symbol history timeline drift")
        for arm in ARMS:
            for fee, slip in COSTS:
                result = run_strategy(bars, arm, fee_bps=fee, slip_bps=slip)
                rows.append({
                    "symbol": symbol,
                    "arm": arm,
                    "fee_bps": str(fee),
                    "slip_bps": str(slip),
                    "ending_equity_usdt": result["ending_equity_usdt"],
                    "return_pct": result["return_pct"],
                    "max_daily_close_drawdown_pct":
                        result["max_daily_close_drawdown_pct"],
                    "trades": result["trades"],
                    "wins": result["wins"],
                    "losses": result["losses"],
                    "fees_paid_usdt": result["fees_paid_usdt"],
                    "slippage_paid_usdt": result["slippage_paid_usdt"],
                    "closed_trade_net_usdt": result["closed_trade_net_usdt"],
                    "exit_reason_counts": result["exit_reason_counts"],
                    "trade_tape": result["trade_tape"],
                })
    summaries = {}
    for arm in ARMS:
        for fee, slip in COSTS:
            for universe, subset in (("six", SIX), ("broad21", BROAD21)):
                if not set(subset).issubset(symbols):
                    continue
                selection = [
                    row for row in rows
                    if (
                        row["symbol"] in subset and row["arm"] == arm
                        and row["fee_bps"] == str(fee)
                        and row["slip_bps"] == str(slip)
                    )
                ]
                summaries[f"{universe}:{arm}:{fee}:{slip}"] = _rank_summary(
                    selection
                )
    source_digests = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in archive_paths
    }
    report = {
        "schema_version": "astra-independent-coin-candidate-lab-v1",
        "evaluation_class": "REUSED_DAILY_PRICE_HISTORY_NEW_FIXED_RESEARCH_ARMS",
        "research_only": True,
        "unseen_holdout_available": False,
        "historical_funding_settlement_verified": False,
        "historical_orderbook_verified": False,
        "historical_trading_rules_verified": False,
        "fees_actual_account_verified": False,
        "multi_coin_portfolio_result": False,
        "real_broker_fills_verified": False,
        "live_trading_allowed": False,
        "strategy_promotion_allowed": False,
        "profitability_proven": False,
        "source_sha256": source_digests,
        "symbols": list(symbols),
        "date_range_utc": [timeline[0].isoformat(), timeline[-1].isoformat()],
        "synchronized_days": len(timeline),
        "warmup_days": WARMUP,
        "capital_per_independent_coin_account_usdt": str(START_CAPITAL),
        "max_cash_allocation_fraction": str(POSITION_FRACTION),
        "take_profit_fraction": str(TARGET_FRACTION),
        "stop_loss_fraction": str(STOP_FRACTION),
        "maximum_holding_days": MAX_HOLDING_DAYS,
        "candidate_rules_fixed_before_reporting": True,
        "selection_based_on_historical_best_return": False,
        "signal_clock": "COMPLETE_PREVIOUS_UTC_DAY",
        "entry_clock": "NEXT_UTC_DAILY_OPEN",
        "same_bar_stop_target_rule": "STOP_FIRST",
        "fee_slippage_costs": [[str(a), str(b)] for a, b in COSTS],
        "candidate_arms": list(ARMS),
        "scenario_count": len(rows),
        "scenarios": rows,
        "universe_descriptive_summary": summaries,
        "limitations": [
            "Standalone $10k model per symbol: returns are NOT a combined portfolio",
            "Daily OHLC stop-first does not reproduce fills or intrahour order",
            "No real funding, settlement Mark Price, margin or liquidation",
            "No historic book liquidity; fee/slippage are hypothetical",
            "Present-day instrument filters cannot be asserted for old dates",
            "Already inspected history: NOT untouched OOS",
            "No optimization or automatic promotion based on observed PnL",
        ],
    }
    report["report_sha256"] = digest(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.output.is_symlink():
        raise StrategyStudyError("refusing to overwrite historical candidate report")
    result = study(args.bars_dir, symbols=tuple(sorted(args.symbols)))
    from tools.publish_research_receipt import publish_new_json_receipt

    publish_new_json_receipt(args.output, result)
    for label, row in result["universe_descriptive_summary"].items():
        print(
            "INDEPENDENT_COIN_CANDIDATE", label,
            json.dumps(row, sort_keys=True),
        )
    print("CANDIDATE_REPORT_SHA256", result["report_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
