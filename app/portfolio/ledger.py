from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from app.domain.trading import Fill, Side


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: Decimal
    average_cost: Decimal


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash: Decimal
    positions: tuple[Position, ...]
    gross_notional: Decimal
    equity: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    cash_income: Decimal
    total_pnl: Decimal
    fees_paid: Decimal


class PortfolioLedger:
    """Deterministic long-only ledger with fee, P&L and corporate-action accounting."""

    def __init__(self, *, opening_cash: Decimal) -> None:
        if not opening_cash.is_finite() or opening_cash < 0:
            raise ValueError("opening_cash must be finite and non-negative")
        self.opening_cash = opening_cash
        self.cash = opening_cash
        self.realized_pnl = Decimal("0")
        self.cash_income = Decimal("0")
        self.fees_paid = Decimal("0")
        self._positions: dict[str, Position] = {}
        self._fill_ids: set[str] = set()
        self._corporate_action_ids: set[str] = set()

    def position(self, symbol: str) -> Position:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("symbol is required")
        return self._positions.get(normalized, Position(normalized, Decimal("0"), Decimal("0")))

    def positions(self) -> tuple[Position, ...]:
        return tuple(self._positions[symbol] for symbol in sorted(self._positions))

    def apply_fill(self, fill: Fill) -> None:
        fill.validate()
        if fill.fill_id in self._fill_ids:
            return
        prior = self.position(fill.symbol)
        notional = fill.quantity * fill.price
        if fill.side is Side.BUY:
            total_cash_cost = notional + fill.fee
            if total_cash_cost > self.cash:
                raise ValueError("INSUFFICIENT_CASH")
            new_quantity = prior.quantity + fill.quantity
            new_cost = (
                (prior.quantity * prior.average_cost + total_cash_cost) / new_quantity
                if new_quantity > 0
                else Decimal("0")
            )
            self.cash -= total_cash_cost
            self._positions[fill.symbol] = Position(fill.symbol, new_quantity, new_cost)
        else:
            if fill.quantity > prior.quantity:
                raise ValueError("LONG_ONLY_POSITION_EXCEEDED")
            proceeds = notional - fill.fee
            if proceeds < 0:
                raise ValueError("FEE_EXCEEDS_PROCEEDS")
            self.realized_pnl += (fill.price - prior.average_cost) * fill.quantity - fill.fee
            new_quantity = prior.quantity - fill.quantity
            self.cash += proceeds
            self._positions[fill.symbol] = Position(
                fill.symbol,
                new_quantity,
                prior.average_cost if new_quantity > 0 else Decimal("0"),
            )
        self.fees_paid += fill.fee
        self._fill_ids.add(fill.fill_id)

    def apply_split(self, *, action_id: str, symbol: str, ratio: Decimal) -> None:
        if not action_id.strip():
            raise ValueError("action_id is required")
        if action_id in self._corporate_action_ids:
            return
        normalized = symbol.strip().upper()
        if not normalized or normalized != symbol:
            raise ValueError("symbol must be normalized uppercase")
        if not ratio.is_finite() or ratio <= 0:
            raise ValueError("split ratio must be positive and finite")
        prior = self.position(normalized)
        if prior.quantity > 0:
            self._positions[normalized] = Position(
                normalized,
                prior.quantity * ratio,
                prior.average_cost / ratio,
            )
        self._corporate_action_ids.add(action_id)

    def apply_cash_dividend(
        self,
        *,
        action_id: str,
        symbol: str,
        amount_per_share: Decimal,
    ) -> Decimal:
        if not action_id.strip():
            raise ValueError("action_id is required")
        if action_id in self._corporate_action_ids:
            return Decimal("0")
        normalized = symbol.strip().upper()
        if not normalized or normalized != symbol:
            raise ValueError("symbol must be normalized uppercase")
        if not amount_per_share.is_finite() or amount_per_share < 0:
            raise ValueError("amount_per_share must be finite and non-negative")
        amount = self.position(normalized).quantity * amount_per_share
        self.cash += amount
        self.cash_income += amount
        self._corporate_action_ids.add(action_id)
        return amount

    @staticmethod
    def _valid_price(symbol: str, prices: Mapping[str, Decimal]) -> Decimal:
        price = prices.get(symbol)
        if price is None or not price.is_finite() or price <= 0:
            raise ValueError(f"valid price required for {symbol}")
        return price

    def gross_notional(self, prices: Mapping[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for symbol, position in self._positions.items():
            if position.quantity == 0:
                continue
            total += position.quantity * self._valid_price(symbol, prices)
        return total

    def unrealized_pnl(self, prices: Mapping[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for symbol, position in self._positions.items():
            if position.quantity == 0:
                continue
            price = self._valid_price(symbol, prices)
            total += (price - position.average_cost) * position.quantity
        return total

    def equity(self, prices: Mapping[str, Decimal]) -> Decimal:
        return self.cash + self.gross_notional(prices)

    def snapshot(self, prices: Mapping[str, Decimal]) -> PortfolioSnapshot:
        gross = self.gross_notional(prices)
        unrealized = self.unrealized_pnl(prices)
        equity = self.cash + gross
        return PortfolioSnapshot(
            cash=self.cash,
            positions=self.positions(),
            gross_notional=gross,
            equity=equity,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=unrealized,
            cash_income=self.cash_income,
            total_pnl=equity - self.opening_cash,
            fees_paid=self.fees_paid,
        )
