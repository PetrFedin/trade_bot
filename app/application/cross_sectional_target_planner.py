from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from app.domain.trading import TargetPosition
from app.marketdata.ohlcv import OhlcvBar
from app.portfolio.ledger import PortfolioLedger
from app.strategy.cross_sectional_portfolio import (
    CrossSectionalPortfolioPolicy,
    PortfolioEntryBlockReason,
    PortfolioExitReason,
)
from app.strategy.cross_sectional_selection import CrossSectionalSelection
from app.strategy.position_management import PositionManagementPolicy
from app.strategy.position_sizing import RiskAwareSizingPolicy, size_position_from_risk


class CrossSectionalSelectionProvider(Protocol):
    top_k: int

    def select(self, bars: Iterable[OhlcvBar]) -> CrossSectionalSelection: ...


class EntryBlockProvider(Protocol):
    def blocks_for_selection(
        self,
        selection: CrossSectionalSelection,
    ) -> Mapping[str, PortfolioEntryBlockReason]: ...


@dataclass(frozen=True)
class CrossSectionalTargetPlan:
    decision_time: datetime
    generated_at: datetime
    selected_symbols: tuple[str, ...]
    held_selected_symbols: tuple[str, ...]
    unmanaged_position_symbols: tuple[str, ...]
    targets: tuple[TargetPosition, ...]
    exit_reasons: tuple[tuple[str, PortfolioExitReason], ...]
    entry_blocks: tuple[tuple[str, PortfolioEntryBlockReason], ...]
    equity: Decimal
    starting_gross_notional: Decimal
    gross_admission_cap: Decimal
    reserved_entry_notional: Decimal


class CrossSectionalTargetPlanner:
    """Translate qualified cross-sectional selection into auditable paper targets.

    Signal selection, risk-aware sizing and the portfolio gross cap reuse the same
    strategy primitives as the shadow backtester. Existing selected positions are not
    mechanically rebalanced. Deselects and explicit protective exits become zero
    targets; exits are never credited toward same-cycle entry capacity because their
    fills are not yet known. An optional durable entry-block provider can enforce
    restart-safe re-entry confirmation before target generation.
    """

    def __init__(
        self,
        *,
        selector: CrossSectionalSelectionProvider,
        portfolio_policy: CrossSectionalPortfolioPolicy,
        position_policy: PositionManagementPolicy,
        sizing_policy: RiskAwareSizingPolicy | None = None,
        entry_block_provider: EntryBlockProvider | None = None,
        strategy_id: str = "cross-sectional-quality-v2-paper-shadow",
    ) -> None:
        portfolio_policy.validate(top_k=selector.top_k)
        position_policy.validate()
        if sizing_policy is not None:
            sizing_policy.validate()
        if not strategy_id.strip():
            raise ValueError("strategy_id is required")
        self.selector = selector
        self.portfolio_policy = portfolio_policy
        self.position_policy = position_policy
        self.sizing_policy = sizing_policy
        self.entry_block_provider = entry_block_provider
        self.strategy_id = strategy_id.strip()

    def plan(
        self,
        bars: Iterable[OhlcvBar],
        *,
        ledger: PortfolioLedger,
        reference_prices: Mapping[str, Decimal],
        generated_at: datetime,
        blocked_entries: Mapping[str, PortfolioEntryBlockReason] | None = None,
        protective_exits: Mapping[str, PortfolioExitReason] | None = None,
    ) -> CrossSectionalTargetPlan:
        if generated_at.tzinfo is None or generated_at.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        materialized = tuple(bars)
        selection = self.selector.select(materialized)
        if generated_at < selection.decision_time:
            raise ValueError("target generation cannot precede completed-bar decision")

        candidates = {candidate.symbol: candidate for candidate in selection.candidates}
        managed_symbols = set(candidates)
        prices = self._reference_prices(reference_prices, ledger=ledger)
        for symbol in managed_symbols:
            if symbol not in prices:
                raise ValueError(f"valid reference price required for {symbol}")

        external_blocks = {} if blocked_entries is None else dict(blocked_entries)
        durable_blocks = (
            {}
            if self.entry_block_provider is None
            else dict(self.entry_block_provider.blocks_for_selection(selection))
        )
        combined_blocks = dict(durable_blocks)
        for symbol, reason in external_blocks.items():
            prior = combined_blocks.get(symbol)
            if prior is not None and prior is not reason:
                raise ValueError(f"conflicting entry block reason for {symbol}")
            combined_blocks[symbol] = reason
        protection = {} if protective_exits is None else dict(protective_exits)
        unknown_blocks = set(combined_blocks) - managed_symbols
        if unknown_blocks:
            raise ValueError(
                "entry blocks reference unmanaged symbols:"
                + ",".join(sorted(unknown_blocks))
            )
        unknown_protection = set(protection) - managed_symbols
        if unknown_protection:
            raise ValueError(
                "protective exits reference unmanaged symbols:"
                + ",".join(sorted(unknown_protection))
            )

        equity = ledger.equity(prices)
        if equity <= 0:
            raise ValueError("portfolio equity must remain positive")
        starting_gross = ledger.gross_notional(prices)
        gross_cap = equity * self.portfolio_policy.maximum_gross_exposure_fraction
        selected_set = set(selection.selected_symbols)
        target_exits: list[TargetPosition] = []
        exit_reasons: list[tuple[str, PortfolioExitReason]] = []
        held_selected: list[str] = []

        for position in ledger.positions():
            if position.quantity <= 0 or position.symbol not in managed_symbols:
                continue
            reason = protection.get(position.symbol)
            if reason is None and position.symbol not in selected_set:
                reason = PortfolioExitReason.SELECTION_EXIT
            if reason is None:
                held_selected.append(position.symbol)
                continue
            target_exits.append(
                self._target(
                    symbol=position.symbol,
                    quantity=Decimal("0"),
                    price=prices[position.symbol],
                    generated_at=generated_at,
                )
            )
            exit_reasons.append((position.symbol, reason))

        for symbol in protection:
            if ledger.position(symbol).quantity <= 0:
                raise ValueError(f"protective exit requires open position:{symbol}")

        target_entries: list[TargetPosition] = []
        entry_blocks: list[tuple[str, PortfolioEntryBlockReason]] = []
        reserved_entry = Decimal("0")
        for symbol in selection.selected_symbols:
            if ledger.position(symbol).quantity > 0:
                continue
            block = combined_blocks.get(symbol)
            if block is not None:
                entry_blocks.append((symbol, block))
                continue
            candidate = candidates[symbol]
            if self.sizing_policy is None:
                target_notional = (
                    equity
                    * self.portfolio_policy.new_position_target_equity_fraction
                )
            else:
                sizing = size_position_from_risk(
                    equity=equity,
                    realized_volatility=candidate.realized_volatility,
                    stop_loss_fraction=self.position_policy.stop_loss_fraction,
                    policy=self.sizing_policy,
                )
                target_notional = sizing.target_notional
            if starting_gross + reserved_entry + target_notional > gross_cap:
                entry_blocks.append(
                    (symbol, PortfolioEntryBlockReason.GROSS_EXPOSURE_CAP)
                )
                continue
            target_entries.append(
                self._target(
                    symbol=symbol,
                    quantity=target_notional / prices[symbol],
                    price=prices[symbol],
                    generated_at=generated_at,
                )
            )
            reserved_entry += target_notional

        unmanaged = tuple(
            position.symbol
            for position in ledger.positions()
            if position.quantity > 0 and position.symbol not in managed_symbols
        )
        return CrossSectionalTargetPlan(
            decision_time=selection.decision_time,
            generated_at=generated_at,
            selected_symbols=selection.selected_symbols,
            held_selected_symbols=tuple(sorted(held_selected)),
            unmanaged_position_symbols=unmanaged,
            targets=tuple(target_exits + target_entries),
            exit_reasons=tuple(exit_reasons),
            entry_blocks=tuple(entry_blocks),
            equity=equity,
            starting_gross_notional=starting_gross,
            gross_admission_cap=gross_cap,
            reserved_entry_notional=reserved_entry,
        )

    def _target(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        generated_at: datetime,
    ) -> TargetPosition:
        return TargetPosition(
            symbol=symbol,
            quantity=quantity,
            reference_price=price,
            generated_at=generated_at,
            strategy_id=self.strategy_id,
        )

    @staticmethod
    def _reference_prices(
        values: Mapping[str, Decimal],
        *,
        ledger: PortfolioLedger,
    ) -> dict[str, Decimal]:
        prices: dict[str, Decimal] = {}
        for symbol, price in values.items():
            if not symbol or symbol != symbol.strip().upper():
                raise ValueError("reference price symbols must be normalized uppercase")
            if not price.is_finite() or price <= 0:
                raise ValueError(f"valid reference price required for {symbol}")
            prices[symbol] = price
        for position in ledger.positions():
            if position.quantity > 0 and position.symbol not in prices:
                raise ValueError(f"valid reference price required for {position.symbol}")
        return prices
