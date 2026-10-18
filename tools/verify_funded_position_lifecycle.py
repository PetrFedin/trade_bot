"""Independent event-lifecycle audit of historical ASTRA portfolio research.

Rebuild each instrument's closed and final-open position interval from source
trade records, independently match the daily entry decision trace and bind
funding cash events to those intervals. No broker access or price prediction.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

INTRABAR_REASONS = frozenset({
    "INTRABAR_HARD_STOP", "INTRABAR_TAKE_PROFIT",
    "INTRABAR_TRAILING_STOP",
})
OPEN_REASONS = frozenset({"SELECTION_EXIT", "TIME_STOP"})


class PortfolioLifecycleError(ValueError):
    """Reject inconsistent position, funding and daily decision identities."""


def _utc(value: str) -> datetime:
    if not isinstance(value, str):
        raise PortfolioLifecycleError("position/funding timestamp is not text")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PortfolioLifecycleError("malformed position/funding timestamp") from exc
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise PortfolioLifecycleError("position/funding timestamp must identify UTC")
    return stamp.astimezone(UTC)


def _amount(value: str) -> Decimal:
    if not isinstance(value, str):
        raise PortfolioLifecycleError("position notional must be decimal text")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise PortfolioLifecycleError("invalid position notional") from exc
    if not result.is_finite() or result <= 0:
        raise PortfolioLifecycleError("nonpositive/nonfinite position notional")
    return result


def audit_position_lifecycle(
    *,
    trades: list[dict],
    final_open_positions: list[dict],
    daily_equity: list[dict],
    funding_events: list[dict],
    allowed_symbols: tuple[str, ...],
) -> dict:
    """Verify recorded event relationships, not completeness of market data.

    Funding event presence is validated against the recorded position state.
    Completeness of exchange funding settlements requires a separate provider
    archive and cannot be inferred from the self-reported event list.
    """
    if any(not isinstance(v, list) for v in (
        trades, final_open_positions, daily_equity, funding_events
    )):
        raise PortfolioLifecycleError("position lifecycle input must be lists")
    universe = set(allowed_symbols)
    if not universe or len(universe) != len(allowed_symbols):
        raise PortfolioLifecycleError("invalid frozen position universe")
    if not daily_equity:
        raise PortfolioLifecycleError("position lifecycle missing daily decisions")

    try:
        days = []
        entries_by_day = defaultdict(list)
        intervals_by_symbol = defaultdict(list)

        for row in daily_equity:
            if not isinstance(row, dict):
                raise PortfolioLifecycleError("malformed daily decision")
            stamp = _utc(row["day_utc"])
            if (stamp.hour, stamp.minute, stamp.second, stamp.microsecond) != (
                0, 0, 0, 0
            ):
                raise PortfolioLifecycleError("daily decision not at UTC midnight")
            if days and stamp != days[-1] + timedelta(days=1):
                raise PortfolioLifecycleError("daily decision timeline discontinuity")
            days.append(stamp)
        first_day, last_day = days[0], days[-1]
        day_set = set(days)

        for row in trades + final_open_positions:
            if not isinstance(row, dict):
                raise PortfolioLifecycleError("malformed position/trade")
            symbol = row["symbol"]
            if symbol not in universe:
                raise PortfolioLifecycleError("position outside frozen universe")
            entered = _utc(row["entry_time"])
            end = _utc(row["exit_time"]) if "exit_time" in row else None
            if (
                entered not in day_set
                or end is not None and (end not in day_set or end < entered)
                or end is None and row in trades
            ):
                raise PortfolioLifecycleError("trade interval outside execution timeline")
            if end is not None:
                reason = row["exit_reason"]
                if reason not in INTRABAR_REASONS | OPEN_REASONS:
                    raise PortfolioLifecycleError("unknown trade exit classification")
                if reason in OPEN_REASONS and end == entered:
                    raise PortfolioLifecycleError("same-day open exit is impossible")
                if type(row["holding_bars"]) is not int or row["holding_bars"] != (
                    (end - entered).days
                ):
                    raise PortfolioLifecycleError("holding duration contradicts dates")
                if row.get("ambiguous_intrabar_exit") is True and (
                    reason not in INTRABAR_REASONS
                ):
                    raise PortfolioLifecycleError("ambiguity unrelated to intrabar exit")
            elif row in final_open_positions and entered > last_day:
                raise PortfolioLifecycleError("future terminal open position")
            entry_qty = _amount(row["quantity"])
            entry_price = _amount(row["entry_execution_price"])
            item = (entered, end, entry_qty, entry_price, row)
            intervals_by_symbol[symbol].append(item)
            entries_by_day[entered].append(symbol)

        # Every symbol has a single active position at any given instant. Daily
        # open-exit then same-day re-entry is disallowed by the frozen policy.
        for symbol, entries in intervals_by_symbol.items():
            entries.sort(key=lambda item: item[0])
            for older, newer in zip(entries, entries[1:], strict=False):
                if older[1] is None or newer[0] <= older[1]:
                    raise PortfolioLifecycleError(
                        f"{symbol}: repeated/overlapping position intervals"
                    )

        open_count_max = 0
        exposures_by_day = {}
        closed_count = 0
        for day, row in zip(days, daily_equity, strict=True):
            actual_entries = row["entered_symbols"]
            expected_entries = entries_by_day[day]
            if (
                not isinstance(actual_entries, list)
                or len(actual_entries) != len(set(actual_entries))
                or set(actual_entries) != set(expected_entries)
                or len(actual_entries) != len(expected_entries)
            ):
                raise PortfolioLifecycleError(
                    "daily entries differ from reconstructed trade/open-position events"
                )
            exposures = {}
            at_close = 0
            for symbol, positions in intervals_by_symbol.items():
                for entered, end, qty, price, position in positions:
                    if entered <= day and (end is None or day <= end):
                        if symbol in exposures:
                            raise PortfolioLifecycleError(
                                "multiple same-symbol positions in one funding day"
                            )
                        if end == day:
                            reason = position["exit_reason"]
                            state = (
                                "INTRABAR_EXIT" if reason in INTRABAR_REASONS
                                else "OPEN_EXIT"
                            )
                            closed_count += 1
                        else:
                            state = "OPEN_AT_CLOSE"
                            at_close += 1
                        exposures[symbol] = (state, entered, qty, price)
            if at_close > 2:
                raise PortfolioLifecycleError("more than two concurrent close holdings")
            open_count_max = max(open_count_max, at_close)
            exposures_by_day[day] = exposures

        # Bind funding settlements to the exact position, entry and state.
        bound_events = 0
        for event in funding_events:
            if not isinstance(event, dict):
                raise PortfolioLifecycleError("malformed funding position record")
            day_stamp = _utc(event["settlement_time"])
            day = day_stamp.replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            exposure = exposures_by_day.get(day, {}).get(event["symbol"])
            if exposure is None:
                raise PortfolioLifecycleError(
                    "funding event without an exposed instrument on settlement day"
                )
            state, entered, qty, price = exposure
            if event.get("holding_state") != state:
                raise PortfolioLifecycleError("funding holding state disagrees with trade")
            if _utc(event["entry_time"]) != entered:
                raise PortfolioLifecycleError("funding entry timestamp differs from trade")
            if _amount(event["quantity"]) != qty or _amount(
                event["entry_execution_price"]
            ) != price:
                raise PortfolioLifecycleError("funding notional differs from trade")
            if state == "OPEN_EXIT" and day_stamp != day:
                raise PortfolioLifecycleError("open exit cannot accrue later-day funding")
            uncertain = state != "OPEN_AT_CLOSE" or day_stamp == entered
            if event.get("participation_uncertain") is not uncertain:
                raise PortfolioLifecycleError(
                    "funding uncertainty inconsistent with position state"
                )
            bound_events += 1

    except (KeyError, TypeError, IndexError) as exc:
        raise PortfolioLifecycleError("malformed position lifecycle field") from exc

    return {
        "schema_version": "astra-position-lifecycle-audit-v1",
        "decision_days": len(days),
        "closed_trade_intervals": len(trades),
        "terminal_open_intervals": len(final_open_positions),
        "funding_events_bound_to_positions": bound_events,
        "max_positions_held_at_daily_close": open_count_max,
        "entry_decision_relationship_verified": True,
        "trade_interval_exclusivity_verified": True,
        "funding_position_attribution_verified": True,
        "broker_execution_verified": False,
        "funding_provider_event_completeness_verified": False,
        "live_trading_allowed": False,
    }
