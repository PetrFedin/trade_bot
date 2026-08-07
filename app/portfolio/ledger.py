from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.trading import Fill, Side


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal
    average_cost: Decimal


class PortfolioLedger:
    """Small deterministic long-only ledger used by the paper vertical slice."""

    def __init__(self, *, opening_cash: Decimal) -> None:
        if not opening_cash.is_finite() or opening_cash < 0:
            raise ValueError("opening_cash must be finite and non-negative")
        self.cash = opening_cash
        self._positions: dict[str, Position] = {}
        self._fill_ids: set[str] = set()

    def position(self, symbol: str) -> Position:
        return self._positions.get(symbol, Position(symbol, Decimal("0"), Decimal("0")))

    def apply_fill(self, fill: Fill) -> None:
        fill.validate()
        if fill.fill_id in self._fill_ids:
            return
        prior = self.position(fill.symbol)
        notional = fill.quantity * fill.price
        if fill.side is Side.BUY:
            if notional > self.cash:
                raise ValueError("INSUFFICIENT_CASH")
            new_quantity = prior.quantity + fill.quantity
            new_cost = (
                (prior.quantity * prior.average_cost + notional) / new_quantity
                if new_quantity > 0
                else Decimal("0")
            )
            self.cash -= notional
            self._positions[fill.symbol] = Position(fill.symbol, new_quantity, new_cost)
        else:
            if fill.quantity > prior.quantity:
                raise ValueError("LONG_ONLY_POSITION_EXCEEDED")
            new_quantity = prior.quantity - fill.quantity
            self.cash += notional
            self._positions[fill.symbol] = Position(
                fill.symbol,
                new_quantity,
                prior.average_cost if new_quantity > 0 else Decimal("0"),
            )
        self._fill_ids.add(fill.fill_id)

    def gross_notional(self, prices: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for symbol, position in self._positions.items():
            price = prices.get(symbol)
            if price is None or not price.is_finite() or price <= 0:
                raise ValueError(f"valid price required for {symbol}")
            total += position.quantity * price
        return total

    def equity(self, prices: dict[str, Decimal]) -> Decimal:
        return self.cash + self.gross_notional(prices)
