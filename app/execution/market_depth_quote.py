"""Read-only visible-depth quotation for Bybit-style linear order books.

No venue connection, order API, strategy decision or historical interpolation.
A quote estimates an immediate cross of one public snapshot, NOT a fill.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

D = Decimal
ZERO = D("0")
ONE = D("1")
BPS = D("10000")


class QuoteRejected(ValueError):
    """Invalid, stale or inadequate source: refuse the research quotation."""


def dec(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise QuoteRejected(f"{name}: bool is not numeric")
    try:
        result = D(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise QuoteRejected(f"{name}: invalid decimal") from exc
    if not result.is_finite():
        raise QuoteRejected(f"{name}: nonfinite decimal")
    return result


def positive(value: Any, name: str) -> Decimal:
    result = dec(value, name)
    if result <= ZERO:
        raise QuoteRejected(f"{name}: must be positive")
    return result


@dataclass(frozen=True)
class InstrumentRules:
    symbol: str
    min_qty: Decimal
    max_qty: Decimal
    qty_step: Decimal
    tick_size: Decimal
    min_notional: Decimal

    @classmethod
    def parse(cls, raw: dict, expected_symbol: str) -> InstrumentRules:
        if (
            not isinstance(raw, dict)
            or raw.get("symbol") != expected_symbol
            or raw.get("status") != "Trading"
            or not isinstance(raw.get("lotSizeFilter"), dict)
            or not isinstance(raw.get("priceFilter"), dict)
        ):
            raise QuoteRejected("untrusted, inactive or mismatched instrument")
        lot = raw["lotSizeFilter"]
        px = raw["priceFilter"]
        min_qty = positive(lot.get("minOrderQty"), "minOrderQty")
        max_qty = positive(lot.get("maxOrderQty"), "maxOrderQty")
        step = positive(lot.get("qtyStep"), "qtyStep")
        tick = positive(px.get("tickSize"), "tickSize")
        minimum = dec(lot.get("minOrderAmt", "0"), "minOrderAmt")
        if min_qty > max_qty or step > max_qty or minimum < ZERO:
            raise QuoteRejected("contradictory instrument filters")
        return cls(expected_symbol, min_qty, max_qty, step, tick, minimum)

    def round_qty(self, requested: Decimal) -> Decimal:
        if not requested.is_finite() or requested <= ZERO:
            raise QuoteRejected("requested quantity must be positive")
        return (requested / self.qty_step).to_integral_value(
            rounding=ROUND_DOWN
        ) * self.qty_step


@dataclass(frozen=True)
class DepthSnapshot:
    symbol: str
    timestamp_ms: int
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]

    @classmethod
    def parse(
        cls, raw: dict, rules: InstrumentRules, *, observation_ms: int,
        max_age_ms: int = 2000,
    ) -> DepthSnapshot:
        if not isinstance(raw, dict) or raw.get("symbol") != rules.symbol:
            raise QuoteRejected("orderbook instrument mismatch")
        stamp = raw.get("ts")
        if type(stamp) is not int or stamp < 1_600_000_000_000:
            raise QuoteRejected("missing or invalid exchange timestamp")
        if type(observation_ms) is not int or type(max_age_ms) is not int:
            raise QuoteRejected("invalid observation timestamp contract")
        if max_age_ms < 0 or not 0 <= observation_ms - stamp <= max_age_ms:
            raise QuoteRejected("stale or future orderbook snapshot")
        sides = []
        for label, descending in (("bids", True), ("asks", False)):
            levels = raw.get(label)
            if not isinstance(levels, list) or not levels:
                raise QuoteRejected(f"{label}: missing depth")
            if len(levels) > 1000:
                raise QuoteRejected(f"{label}: excessive depth")
            found: list[tuple[Decimal, Decimal]] = []
            for row in levels:
                if not isinstance(row, dict):
                    raise QuoteRejected(f"{label}: invalid level")
                price = positive(row.get("price"), "level price")
                qty = positive(row.get("qty"), "level quantity")
                if price % rules.tick_size:
                    raise QuoteRejected("orderbook price violates tick size")
                if found:
                    previous = found[-1][0]
                    if (descending and price >= previous) or (
                        not descending and price <= previous
                    ):
                        raise QuoteRejected("duplicate or unordered book price")
                found.append((price, qty))
            sides.append(tuple(found))
        bids, asks = sides
        if bids[0][0] >= asks[0][0]:
            raise QuoteRejected("locked or crossed visible orderbook")
        return cls(rules.symbol, stamp, bids, asks)


def quote_market_cross(
    book: DepthSnapshot,
    rules: InstrumentRules,
    *,
    side: str,
    requested_notional: Decimal,
    fee_bps: Decimal,
    depth_haircut: Decimal = ZERO,
) -> dict:
    """Cross displayed levels all-or-none; only a nonexecutable quote proxy."""
    if book.symbol != rules.symbol or side not in ("BUY", "SELL"):
        raise QuoteRejected("invalid instrument or direction")
    notional = positive(requested_notional, "requested_notional")
    fee = dec(fee_bps, "fee_bps")
    haircut = dec(depth_haircut, "depth_haircut")
    if not ZERO <= fee <= D("100"):
        raise QuoteRejected("unsupported hypothetical fee")
    if not ZERO <= haircut < ONE:
        raise QuoteRejected("invalid visible-depth haircut")
    levels = book.asks if side == "BUY" else book.bids
    best = levels[0][0]
    mid = (book.bids[0][0] + book.asks[0][0]) / 2
    desired_qty = rules.round_qty(notional / best)
    if (
        desired_qty < rules.min_qty
        or desired_qty > rules.max_qty
        or desired_qty * best < rules.min_notional
    ):
        return {
            "status": "INSTRUMENT_FILTER_REJECTED",
            "requested_notional_usdt": str(notional),
            "rounded_required_qty": str(desired_qty),
            "hypothetical_fill_confirmed": False,
            "usable_for_live_routing": False,
        }
    remaining = desired_qty
    supplied = ZERO
    value = ZERO
    for price, visible_qty in levels:
        available = visible_qty * (ONE - haircut)
        eaten = min(remaining, available)
        supplied += eaten
        value += eaten * price
        remaining -= eaten
        if remaining <= ZERO:
            break
    status = (
        "VISIBLE_DEPTH_SUFFICIENT" if remaining == ZERO
        else "INSUFFICIENT_VISIBLE_DEPTH"
    )
    vw = value / supplied if supplied > ZERO else None
    adverse = (
        (vw - mid) * BPS / mid if side == "BUY"
        else (mid - vw) * BPS / mid
    ) if vw is not None else None
    return {
        "status": status,
        "side": side,
        "symbol": book.symbol,
        "book_timestamp_ms": book.timestamp_ms,
        "requested_notional_usdt": str(notional),
        "rounded_required_qty": str(desired_qty),
        "visible_qty_at_haircut": str(supplied),
        "uncovered_qty": str(remaining),
        "depth_haircut_fraction_assumed": str(haircut),
        "best_opposite_price": str(best),
        "mid_price": str(mid),
        "full_visible_vwap": (
            str(vw) if status == "VISIBLE_DEPTH_SUFFICIENT" else None
        ),
        "adverse_bps_from_mid_if_fully_visible": (
            str(adverse) if status == "VISIBLE_DEPTH_SUFFICIENT" else None
        ),
        "hypothetical_fee_bps": str(fee),
        "hypothetical_fee_usdt": (
            str(value * fee / BPS)
            if status == "VISIBLE_DEPTH_SUFFICIENT" else None
        ),
        "hypothetical_cross_cost_usdt_vs_mid": (
            str(value - mid * desired_qty + value * fee / BPS)
            if side == "BUY" and status == "VISIBLE_DEPTH_SUFFICIENT" else
            str(mid * desired_qty - value + value * fee / BPS)
            if side == "SELL" and status == "VISIBLE_DEPTH_SUFFICIENT"
            else None
        ),
        "hypothetical_fill_confirmed": False,
        "visible_liquidity_not_guaranteed": True,
        "queue_and_market_impact_unobserved": True,
        "usable_for_live_routing": False,
        "source_is_historical_market_depth": False,
    }
