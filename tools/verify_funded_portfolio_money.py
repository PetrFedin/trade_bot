"""Independent arithmetic invariants for a modelled ASTRA portfolio replay.

Checks the money and event ledger emitted by PR #324 without importing its
backtester. This is NOT a venue-account ledger, Mark Price settlement, holdout
evaluation or permission for paper/live trading.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from tools.verify_funded_position_lifecycle import (
    PortfolioLifecycleError,
    audit_position_lifecycle,
)

OPENING_CASH = Decimal("10000")
USD_TOLERANCE = Decimal("0.000001")
RETURN_TOLERANCE = Decimal("0.0000000001")
RELATIVE_PRICE_TOLERANCE = Decimal("0.00000000001")
ZERO = Decimal("0")
SCENARIOS = ("NO_FUNDING", "UPPER_COST", "LOWER_COST")


class PortfolioMoneyInvariantError(ValueError):
    """Reject money, event, chronology, or authority drift."""


def _amount(value, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise PortfolioMoneyInvariantError(f"{field}: expected decimal text or integer")
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PortfolioMoneyInvariantError(f"{field}: invalid decimal") from exc
    if not result.is_finite():
        raise PortfolioMoneyInvariantError(f"{field}: non-finite amount")
    return result


def _close(left: Decimal, right: Decimal, field: str, *, tol=USD_TOLERANCE) -> None:
    if abs(left - right) > tol:
        raise PortfolioMoneyInvariantError(f"{field}: independent arithmetic mismatch")


def _utc(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise PortfolioMoneyInvariantError(f"{field}: invalid timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PortfolioMoneyInvariantError(f"{field}: invalid timestamp") from exc
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise PortfolioMoneyInvariantError(f"{field}: timestamp must identify UTC")
    return stamp.astimezone(UTC)


def _positive(row: dict, key: str) -> Decimal:
    value = _amount(row[key], key)
    if value <= ZERO:
        raise PortfolioMoneyInvariantError(f"{key}: expected positive")
    return value


def _nonnegative(row: dict, key: str) -> Decimal:
    value = _amount(row[key], key)
    if value < ZERO:
        raise PortfolioMoneyInvariantError(f"{key}: expected non-negative")
    return value


def _rows(record: dict, key: str) -> list[dict]:
    rows = record.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise PortfolioMoneyInvariantError(f"{key}: expected list of objects")
    return rows


def audit_portfolio_model(
    record: dict, *, scenario: str, allowed_symbols: tuple[str, ...],
    fee_bps_per_fill: str | None = None,
    slippage_bps_per_side: str | None = None,
    expected_first_execution_day_utc: str | None = None,
    expected_last_source_day_utc: str | None = None,
) -> dict:
    """Reconcile reported trade PnL, funding, costs, holdings and cash.

    Reconciliation is deliberately independent of the earlier source hashes.
    On acceptance the numerical equations hold, but fills and market prices
    are still hypothetical research assumptions, not actual transactions.
    """
    if scenario not in SCENARIOS or not isinstance(record, dict):
        raise PortfolioMoneyInvariantError("unknown funding scenario or model")
    if (fee_bps_per_fill is None) != (slippage_bps_per_side is None):
        raise PortfolioMoneyInvariantError("fee and slippage schedule must be paired")
    cost_schedule = fee_bps_per_fill is not None
    if cost_schedule:
        fee_rate = _amount(fee_bps_per_fill, "fee bps") / Decimal("10000")
        slip_rate = _amount(slippage_bps_per_side, "slippage bps") / Decimal("10000")
        if fee_rate < ZERO or slip_rate < ZERO or slip_rate >= Decimal("1"):
            raise PortfolioMoneyInvariantError("invalid execution-cost schedule")
    universe = set(allowed_symbols)
    if not universe or len(universe) != len(allowed_symbols):
        raise PortfolioMoneyInvariantError("invalid research universe")
    try:
        metrics = record["metrics"]
        if not isinstance(metrics, dict):
            raise PortfolioMoneyInvariantError("metrics must be an object")

        closed_net = ZERO
        fee_sum = ZERO
        trades = _rows(record, "trades")
        for trade in trades:
            if trade["symbol"] not in universe:
                raise PortfolioMoneyInvariantError("trade outside frozen universe")
            entered = _utc(trade["entry_time"], "trade entry")
            exited = _utc(trade["exit_time"], "trade exit")
            if exited < entered:
                raise PortfolioMoneyInvariantError("trade exit precedes entry")
            quantity = _positive(trade, "quantity")
            entry_reference = _positive(trade, "entry_reference_price")
            exit_reference = _positive(trade, "exit_reference_price")
            entry_execution = _positive(trade, "entry_execution_price")
            exit_execution = _positive(trade, "exit_execution_price")
            entry_fee = _nonnegative(trade, "entry_fee")
            exit_fee = _nonnegative(trade, "exit_fee")
            if cost_schedule:
                _close(
                    entry_execution, entry_reference * (Decimal("1") + slip_rate),
                    "entry slippage bps",
                    tol=entry_reference * RELATIVE_PRICE_TOLERANCE,
                )
                _close(
                    exit_execution, exit_reference * (Decimal("1") - slip_rate),
                    "exit slippage bps",
                    tol=exit_reference * RELATIVE_PRICE_TOLERANCE,
                )
                _close(entry_fee, quantity * entry_execution * fee_rate,
                       "entry execution fee bps")
                _close(exit_fee, quantity * exit_execution * fee_rate,
                       "exit execution fee bps")
            gross = (exit_reference - entry_reference) * quantity
            slippage = (
                entry_execution - entry_reference + exit_reference - exit_execution
            ) * quantity
            net = gross - slippage - entry_fee - exit_fee
            _close(_amount(trade["gross_pnl_before_costs"], "gross PnL"), gross, "gross PnL")
            _close(_amount(trade["slippage_cost"], "slippage"), slippage, "trade slippage")
            _close(_amount(trade["net_pnl"], "trade net PnL"), net, "trade net PnL")
            fee_sum += entry_fee + exit_fee
            closed_net += net

        open_cost = ZERO
        open_value = ZERO
        open_positions = _rows(record, "final_open_positions")
        open_symbols = set()
        for position in open_positions:
            symbol = position["symbol"]
            if symbol not in universe or symbol in open_symbols:
                raise PortfolioMoneyInvariantError("invalid or repeated open instrument")
            open_symbols.add(symbol)
            _utc(position["entry_time"], "open position entry")
            quantity = _positive(position, "quantity")
            entry_price = _positive(position, "entry_execution_price")
            fee = _nonnegative(position, "entry_fee")
            if cost_schedule:
                _close(fee, quantity * entry_price * fee_rate, "open entry fee bps")
            mark = _positive(position, "final_mark")
            open_cost += quantity * entry_price + fee
            open_value += quantity * mark
            fee_sum += fee

        funding_events = _rows(record, "funding_events")
        funding_days = _rows(record, "funding_days")
        if scenario == "NO_FUNDING" and (funding_events or funding_days):
            raise PortfolioMoneyInvariantError("NO_FUNDING contains funding events")
        identities = set()
        daily_values = defaultdict(lambda: [ZERO, 0, 0])
        debits = ZERO
        credits = ZERO
        for event in funding_events:
            symbol = event["symbol"]
            if symbol not in universe:
                raise PortfolioMoneyInvariantError("funding outside frozen universe")
            stamp = _utc(event["settlement_time"], "funding settlement")
            entered = _utc(event["entry_time"], "funding entry")
            if entered > stamp:
                raise PortfolioMoneyInvariantError("funding settled before entry")
            booked = _utc(event["booked_after_day_utc"], "funding booking")
            if booked != stamp.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) + timedelta(days=1):
                raise PortfolioMoneyInvariantError("funding booked on wrong day")
            identity = f"research-funding:{symbol}:{stamp.isoformat()}"
            if event["activity_id"] != identity or identity in identities:
                raise PortfolioMoneyInvariantError("duplicate/mismatched funding event")
            identities.add(identity)
            if event.get("mark_price_verified") is not False:
                raise PortfolioMoneyInvariantError("false exact Mark Price claim")
            if not isinstance(event.get("participation_uncertain"), bool):
                raise PortfolioMoneyInvariantError("participation uncertainty missing")
            quantity = _positive(event, "quantity")
            entry_price = _positive(event, "entry_execution_price")
            rate = _amount(event["provider_rate"], "provider rate")
            cost = quantity * entry_price * rate
            _close(_amount(event["cost_if_held_usd"], "funding cost"), cost, "funding cost")
            uncertain = event["participation_uncertain"]
            if uncertain:
                selected = max(cost, ZERO) if scenario == "UPPER_COST" else min(cost, ZERO)
            else:
                selected = cost
            amount = -selected
            _close(_amount(event["cash_amount_usd"], "funding cash"), amount, "funding cash")
            daily = daily_values[stamp.date()]
            daily[0] += amount
            daily[1] += int(amount != ZERO)
            daily[2] += int(uncertain)
            if amount > ZERO:
                credits += amount
            elif amount < ZERO:
                debits -= amount

        seen_days = set()
        previous_day = None
        for day in funding_days:
            stamp = _utc(day["day_utc"], "funding day")
            if (stamp.hour, stamp.minute, stamp.second, stamp.microsecond) != (
                0, 0, 0, 0
            ):
                raise PortfolioMoneyInvariantError("funding day not UTC midnight")
            if stamp.date() in seen_days or (
                previous_day is not None and stamp <= previous_day
            ):
                raise PortfolioMoneyInvariantError("duplicate/reordered funding day")
            seen_days.add(stamp.date())
            previous_day = stamp
            expected = daily_values.get(stamp.date(), [ZERO, 0, 0])
            _close(
                _amount(day["net_funding_cash_usd"], "daily funding"),
                expected[0], "funding day cash",
            )
            if (
                day["nonzero_flow_count"] != expected[1]
                or day["uncertain_event_count"] != expected[2]
            ):
                raise PortfolioMoneyInvariantError("funding day event counters mismatch")
        if set(daily_values) - seen_days:
            raise PortfolioMoneyInvariantError("unbooked funding settlement day")

        net_funding = credits - debits
        _close(
            _amount(metrics["closed_trade_net_excluding_funding_usd"], "closed net"),
            closed_net, "closed trade sum",
        )
        _close(_amount(metrics["execution_fees_usd"], "fees"), fee_sum, "execution fee sum")
        _close(_amount(metrics["funding_cash_net_usd"], "net funding"), net_funding,
               "funding cash sum")
        _close(_amount(metrics["funding_debits_usd"], "funding debits"), debits,
               "funding debit sum")
        _close(_amount(metrics["funding_credits_usd"], "funding credits"), credits,
               "funding credit sum")
        _close(_amount(metrics["open_cost_usd"], "open cost"), open_cost, "open cost")
        _close(_amount(metrics["open_marked_value_usd"], "open value"), open_value,
               "open marked value")
        ending_cash = OPENING_CASH + closed_net + net_funding - open_cost
        ending_equity = ending_cash + open_value
        _close(_amount(metrics["ending_cash_usd"], "ending cash"), ending_cash,
               "cash reconciliation")
        _close(_amount(metrics["ending_equity_usd"], "ending equity"), ending_equity,
               "equity reconciliation")
        _close(
            _amount(metrics["return_including_daily_funding"], "portfolio return"),
            (ending_equity - OPENING_CASH) / OPENING_CASH,
            "funded portfolio return", tol=RETURN_TOLERANCE,
        )
        if (
            type(metrics["closed_trades"]) is not int
            or type(metrics["fills"]) is not int
            or metrics["closed_trades"] != len(trades)
            or metrics["fills"] != 2 * len(trades) + len(open_positions)
        ):
            raise PortfolioMoneyInvariantError("trade/fill count mismatch")

        daily_equity = _rows(record, "daily_equity")
        if not daily_equity:
            raise PortfolioMoneyInvariantError("missing daily equity history")
        peak = OPENING_CASH
        drawdown = ZERO
        last_day = None
        previous_close = OPENING_CASH
        day_dates = []
        for row in daily_equity:
            day = _utc(row["day_utc"], "daily equity")
            if (day.hour, day.minute, day.second, day.microsecond) != (0, 0, 0, 0):
                raise PortfolioMoneyInvariantError("daily equity timestamp not UTC midnight")
            if last_day is not None and day != last_day + timedelta(days=1):
                raise PortfolioMoneyInvariantError("daily equity chronology invalid")
            last_day = day
            day_dates.append(day.date())
            prior_equity = _amount(row["prior_equity"], "prior daily equity")
            _close(prior_equity, previous_close, "daily equity carry-forward")
            close = _amount(row["closing_equity"], "daily closing equity")
            if prior_equity <= ZERO or close <= ZERO:
                raise PortfolioMoneyInvariantError("nonpositive research daily equity")
            selected = row["selected_symbols"]
            entered = row["entered_symbols"]
            if (
                not isinstance(selected, list) or not isinstance(entered, list)
                or any(not isinstance(symbol, str) for symbol in selected + entered)
                or len(selected) > 2 or len(set(selected)) != len(selected)
                or len(set(entered)) != len(entered)
                or len(entered) > 2 or not set(selected).issubset(universe)
                or not set(entered).issubset(set(selected))
            ):
                raise PortfolioMoneyInvariantError("invalid daily decision/entry relationship")
            peak = max(peak, close)
            drawdown = max(drawdown, (peak - close) / peak)
            previous_close = close
        if expected_first_execution_day_utc is not None and _utc(
            expected_first_execution_day_utc, "first execution day"
        ) != _utc(daily_equity[0]["day_utc"], "first daily equity"):
            raise PortfolioMoneyInvariantError("replay first execution day differs")
        if expected_last_source_day_utc is not None and _utc(
            expected_last_source_day_utc, "last source day"
        ) != last_day:
            raise PortfolioMoneyInvariantError("replay terminal daily equity day differs")
        if scenario != "NO_FUNDING" and (
            [stamp.date() for stamp in (
                _utc(day["day_utc"], "funding day") for day in funding_days
            )] != day_dates
        ):
            raise PortfolioMoneyInvariantError("funding day / daily equity coverage mismatch")
        if set(daily_values) - set(day_dates):
            raise PortfolioMoneyInvariantError("funding settlement outside daily history")
        for trade in trades:
            if (
                _utc(trade["entry_time"], "trade entry").date() not in day_dates
                or _utc(trade["exit_time"], "trade exit").date() not in day_dates
            ):
                raise PortfolioMoneyInvariantError("trade outside recorded daily history")
        for position in open_positions:
            if _utc(position["entry_time"], "open entry").date() not in day_dates:
                raise PortfolioMoneyInvariantError("open entry outside recorded daily history")
        _close(
            _amount(daily_equity[-1]["closing_equity"], "last daily equity"),
            ending_equity, "terminal daily equity",
        )
        _close(
            _amount(metrics["daily_close_max_drawdown_over_peak"], "drawdown"),
            drawdown, "daily drawdown", tol=RETURN_TOLERANCE,
        )
        try:
            lifecycle = audit_position_lifecycle(
                trades=trades,
                final_open_positions=open_positions,
                daily_equity=daily_equity,
                funding_events=funding_events,
                allowed_symbols=allowed_symbols,
            )
        except PortfolioLifecycleError as exc:
            raise PortfolioMoneyInvariantError(
                f"position lifecycle mismatch: {exc}"
            ) from exc
    except (KeyError, IndexError, TypeError, ZeroDivisionError) as exc:
        raise PortfolioMoneyInvariantError("missing/malformed portfolio ledger field") from exc

    return {
        "closed_trades": len(trades),
        "open_positions": len(open_positions),
        "funding_events": len(funding_events),
        "funding_days": len(funding_days),
        "ending_equity_usd": str(ending_equity),
        "net_funding_usd": str(net_funding),
        "fees_usd": str(fee_sum),
        "cash_equity_reconciled": True,
        "funding_cash_reconciled": True,
        "fee_slippage_schedule_verified": cost_schedule,
        "terminal_daily_equity_verified": True,
        "daily_equity_carry_forward_verified": True,
        "funding_day_coverage_verified": True,
        "decision_universe_verified": True,
        "position_lifecycle_verified": True,
        "funding_position_attributions": lifecycle["funding_events_bound_to_positions"],
        "max_positions_held_at_daily_close": lifecycle["max_positions_held_at_daily_close"],
        "trading_authority_granted": False,
    }


def canonical_audit_digest(audits: dict[str, dict]) -> str:
    """Stable digest of second-pass money reconciliation, not venue evidence."""
    return hashlib.sha256(
        json.dumps(
            audits, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
    ).hexdigest()
