"""Immutable instrument specification and the normalization that must precede risk.

A venue publishes the rules an order has to satisfy: what prices exist, what quantities
exist, how small an order may be. Nothing in this application modelled them. The Alpaca
paper adapter forwards caller-supplied decimals, and the canonical Bybit code carries no
instrument acquisition path at all, so the first component that knows whether an order is
expressible is the venue itself - after risk has already admitted something else.

That ordering is the defect. If the broker rounds a quantity, clips it, or rejects it,
the position that results is not the position risk approved. The correct order is

    fresh spec -> normalized executable economics -> risk admission -> authorization

and this module supplies the first two steps.

Normalization is deliberately one-directional about risk. Quantity rounds down, never up,
because rounding up buys more than was asked for. A buy price rounds down and a sell price
rounds up, so neither side pays more than intended to reach a permitted tick. When
rounding down pushes an order under a venue minimum the order is rejected rather than
quietly resized: a smaller order than requested is a different order, and silently
substituting one is how a risk decision stops describing what was sent.

Nothing here talks to a venue or authorizes anything. It decides whether a desired order
is expressible, and says exactly why when it is not.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_UP, Decimal
from enum import StrEnum

from app.domain.trading import Side

# A specification older than this cannot support a new risk decision. The venue
# documents that some maximum-quantity fields are revised periodically, so a snapshot
# is evidence about a moment rather than a standing fact.
DEFAULT_MAXIMUM_SPEC_AGE = timedelta(minutes=15)


class InstrumentStatus(StrEnum):
    """Tradability as the venue reports it."""

    TRADING = "TRADING"
    PRE_LAUNCH = "PRE_LAUNCH"
    SETTLING = "SETTLING"
    DELIVERING = "DELIVERING"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


class RejectionReason(StrEnum):
    """Why a desired order cannot become executable economics."""

    SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
    NOT_TRADABLE = "NOT_TRADABLE"
    SPEC_STALE = "SPEC_STALE"
    NON_POSITIVE_QUANTITY = "NON_POSITIVE_QUANTITY"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    QUANTITY_ROUNDS_TO_ZERO = "QUANTITY_ROUNDS_TO_ZERO"
    BELOW_MINIMUM_QUANTITY = "BELOW_MINIMUM_QUANTITY"
    ABOVE_MAXIMUM_QUANTITY = "ABOVE_MAXIMUM_QUANTITY"
    PRICE_BELOW_MINIMUM = "PRICE_BELOW_MINIMUM"
    PRICE_ABOVE_MAXIMUM = "PRICE_ABOVE_MAXIMUM"
    PRICE_ROUNDS_OUT_OF_RANGE = "PRICE_ROUNDS_OUT_OF_RANGE"
    BELOW_MINIMUM_NOTIONAL = "BELOW_MINIMUM_NOTIONAL"


class InstrumentNormalizationError(ValueError):
    """Raised when a desired order cannot be expressed under the instrument's rules."""

    def __init__(self, reason: RejectionReason, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


def _positive(value: Decimal, name: str) -> None:
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be finite and positive")


@dataclass(frozen=True)
class InstrumentSpec:
    """One venue's published rules for one instrument, observed at one instant."""

    venue: str
    environment: str
    category: str
    symbol: str
    status: InstrumentStatus
    base_coin: str
    quote_coin: str
    settle_coin: str
    tick_size: Decimal
    minimum_price: Decimal
    maximum_price: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    maximum_quantity: Decimal
    minimum_notional: Decimal
    source_timestamp: datetime
    observed_timestamp: datetime
    maximum_market_quantity: Decimal | None = None
    minimum_leverage: Decimal | None = None
    maximum_leverage: Decimal | None = None
    leverage_step: Decimal | None = None

    def validate(self) -> None:
        for name in ("venue", "environment", "category", "symbol"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.symbol != self.symbol.upper():
            raise ValueError("symbol must be normalized uppercase")
        for name in ("base_coin", "quote_coin", "settle_coin"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in (
            "tick_size",
            "minimum_price",
            "maximum_price",
            "quantity_step",
            "minimum_quantity",
            "maximum_quantity",
        ):
            _positive(getattr(self, name), name)
        if not self.minimum_notional.is_finite() or self.minimum_notional < 0:
            raise ValueError("minimum_notional must be finite and non-negative")
        if self.minimum_price > self.maximum_price:
            raise ValueError("minimum_price must not exceed maximum_price")
        if self.minimum_quantity > self.maximum_quantity:
            raise ValueError("minimum_quantity must not exceed maximum_quantity")
        if self.maximum_market_quantity is not None:
            _positive(self.maximum_market_quantity, "maximum_market_quantity")
        for name in ("source_timestamp", "observed_timestamp"):
            value = getattr(self, name)
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")

    @property
    def is_tradable(self) -> bool:
        return self.status is InstrumentStatus.TRADING

    def age_at(self, moment: datetime) -> timedelta:
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("moment must be timezone-aware")
        return moment.astimezone(UTC) - self.observed_timestamp.astimezone(UTC)

    def is_stale_at(
        self, moment: datetime, maximum_age: timedelta = DEFAULT_MAXIMUM_SPEC_AGE
    ) -> bool:
        return self.age_at(moment) > maximum_age

    @property
    def revision(self) -> str:
        """Canonical digest of the rules, excluding when they happened to be observed.

        Two snapshots taken minutes apart describe the same rules and must share a
        revision; a venue that changes a tick size must not. Observation time is
        therefore not part of the material, while the venue's own source timestamp is.
        """
        material = {
            "venue": self.venue,
            "environment": self.environment,
            "category": self.category,
            "symbol": self.symbol,
            "status": self.status.value,
            "base_coin": self.base_coin,
            "quote_coin": self.quote_coin,
            "settle_coin": self.settle_coin,
            "tick_size": str(self.tick_size),
            "minimum_price": str(self.minimum_price),
            "maximum_price": str(self.maximum_price),
            "quantity_step": str(self.quantity_step),
            "minimum_quantity": str(self.minimum_quantity),
            "maximum_quantity": str(self.maximum_quantity),
            "minimum_notional": str(self.minimum_notional),
            "maximum_market_quantity": (
                None if self.maximum_market_quantity is None else str(self.maximum_market_quantity)
            ),
            "minimum_leverage": (
                None if self.minimum_leverage is None else str(self.minimum_leverage)
            ),
            "maximum_leverage": (
                None if self.maximum_leverage is None else str(self.maximum_leverage)
            ),
            "leverage_step": (None if self.leverage_step is None else str(self.leverage_step)),
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class NormalizedOrderEconomics:
    """Exactly what may be sent, and the rules revision that made it expressible."""

    symbol: str
    side: Side
    quantity: Decimal
    limit_price: Decimal
    notional: Decimal
    spec_revision: str
    normalized_at: datetime
    quantity_was_reduced: bool
    price_was_moved: bool

    def validate(self) -> None:
        _positive(self.quantity, "quantity")
        _positive(self.limit_price, "limit_price")
        _positive(self.notional, "notional")
        if len(self.spec_revision) != 64:
            raise ValueError("spec_revision must be a sha256 digest")
        if self.normalized_at.tzinfo is None or self.normalized_at.utcoffset() is None:
            raise ValueError("normalized_at must be timezone-aware")


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _ceil_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_UP) * step


def normalize_order(
    spec: InstrumentSpec,
    *,
    side: Side,
    quantity: Decimal,
    limit_price: Decimal,
    observed_at: datetime,
    maximum_spec_age: timedelta = DEFAULT_MAXIMUM_SPEC_AGE,
    symbol: str | None = None,
) -> NormalizedOrderEconomics:
    """Return the only executable order these rules permit, or refuse and say why.

    Rounding never increases exposure. Quantity moves down to the permitted step; a buy
    price moves down to the permitted tick and a sell price moves up. An order that falls
    under a venue minimum after that is rejected rather than resized upward, because
    resizing it would send something risk did not admit.
    """
    spec.validate()
    if symbol is not None and symbol != spec.symbol:
        raise InstrumentNormalizationError(
            RejectionReason.SYMBOL_MISMATCH, f"{symbol} is not {spec.symbol}"
        )
    if not spec.is_tradable:
        raise InstrumentNormalizationError(
            RejectionReason.NOT_TRADABLE, f"status is {spec.status.value}"
        )
    if spec.is_stale_at(observed_at, maximum_spec_age):
        raise InstrumentNormalizationError(
            RejectionReason.SPEC_STALE, f"observed {spec.age_at(observed_at)} ago"
        )
    if not quantity.is_finite() or quantity <= 0:
        raise InstrumentNormalizationError(RejectionReason.NON_POSITIVE_QUANTITY)
    if not limit_price.is_finite() or limit_price <= 0:
        raise InstrumentNormalizationError(RejectionReason.NON_POSITIVE_PRICE)

    if limit_price < spec.minimum_price:
        raise InstrumentNormalizationError(
            RejectionReason.PRICE_BELOW_MINIMUM, f"{limit_price} < {spec.minimum_price}"
        )
    if limit_price > spec.maximum_price:
        raise InstrumentNormalizationError(
            RejectionReason.PRICE_ABOVE_MAXIMUM, f"{limit_price} > {spec.maximum_price}"
        )

    # A buy never pays more to reach a tick; a sell never accepts less.
    if side is Side.BUY:
        price = _floor_to_step(limit_price, spec.tick_size)
    else:
        price = _ceil_to_step(limit_price, spec.tick_size)
    if price < spec.minimum_price or price > spec.maximum_price:
        raise InstrumentNormalizationError(
            RejectionReason.PRICE_ROUNDS_OUT_OF_RANGE,
            f"{limit_price} became {price}",
        )

    normalized_quantity = _floor_to_step(quantity, spec.quantity_step)
    if normalized_quantity <= 0:
        raise InstrumentNormalizationError(
            RejectionReason.QUANTITY_ROUNDS_TO_ZERO,
            f"{quantity} below step {spec.quantity_step}",
        )
    if normalized_quantity < spec.minimum_quantity:
        raise InstrumentNormalizationError(
            RejectionReason.BELOW_MINIMUM_QUANTITY,
            f"{normalized_quantity} < {spec.minimum_quantity}",
        )
    if normalized_quantity > spec.maximum_quantity:
        raise InstrumentNormalizationError(
            RejectionReason.ABOVE_MAXIMUM_QUANTITY,
            f"{normalized_quantity} > {spec.maximum_quantity}",
        )

    notional = normalized_quantity * price
    if notional < spec.minimum_notional:
        raise InstrumentNormalizationError(
            RejectionReason.BELOW_MINIMUM_NOTIONAL,
            f"{notional} < {spec.minimum_notional}",
        )

    economics = NormalizedOrderEconomics(
        symbol=spec.symbol,
        side=side,
        quantity=normalized_quantity,
        limit_price=price,
        notional=notional,
        spec_revision=spec.revision,
        normalized_at=observed_at.astimezone(UTC),
        quantity_was_reduced=normalized_quantity != quantity,
        price_was_moved=price != limit_price,
    )
    economics.validate()
    return economics
