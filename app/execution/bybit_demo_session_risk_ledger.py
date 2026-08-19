from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.execution.bybit_demo_post_trade_accounting import (
    BybitDemoPostTradeAccountingResult,
    BybitDemoProfitOutcomeStatus,
)
from app.strategy.crypto_session_risk import CryptoSessionRiskState

_ZERO = Decimal("0")


@dataclass(frozen=True)
class BybitDemoSessionTradeOutcome:
    entry_order_link_id: str
    symbol: str
    created_time_ms: int
    updated_time_ms: int
    all_in_net_pnl_usdt: Decimal
    execution_fees_usdt: Decimal

    def validate(self) -> None:
        if not self.entry_order_link_id.startswith("ASTRA-DEMO-"):
            raise ValueError("demo session trade key must use ASTRA-DEMO namespace")
        if self.symbol != self.symbol.strip().upper() or not self.symbol.endswith("USDT"):
            raise ValueError("demo session trade symbol must be normalized USDT")
        if self.created_time_ms < 0 or self.updated_time_ms < self.created_time_ms:
            raise ValueError("demo session trade timestamps are invalid")
        if not self.all_in_net_pnl_usdt.is_finite():
            raise ValueError("demo session all-in PnL must be finite")
        if not self.execution_fees_usdt.is_finite():
            raise ValueError("demo session execution fees must be finite")


@dataclass(frozen=True)
class BybitDemoSessionRiskLedger:
    opening_equity_usdt: Decimal
    outcomes: tuple[BybitDemoSessionTradeOutcome, ...] = ()
    peak_equity_usdt: Decimal | None = None
    demo_only: bool = True
    live_mainnet_order_routing_allowed: bool = False

    def validate(self) -> None:
        if not self.opening_equity_usdt.is_finite() or self.opening_equity_usdt <= 0:
            raise ValueError("demo session opening equity must be positive and finite")
        keys: set[str] = set()
        previous_order: tuple[int, int, str] | None = None
        for outcome in self.outcomes:
            outcome.validate()
            if outcome.entry_order_link_id in keys:
                raise ValueError("demo session ledger contains a duplicate trade key")
            keys.add(outcome.entry_order_link_id)
            order = (
                outcome.updated_time_ms,
                outcome.created_time_ms,
                outcome.entry_order_link_id,
            )
            if previous_order is not None and order < previous_order:
                raise ValueError("demo session outcomes must be chronologically ordered")
            previous_order = order
        if self.peak_equity_usdt is not None:
            if (
                not self.peak_equity_usdt.is_finite()
                or self.peak_equity_usdt < self.opening_equity_usdt
            ):
                raise ValueError("demo session peak equity must be finite and at least opening equity")
            if self.peak_equity_usdt < self.realized_peak_equity_usdt:
                raise ValueError("demo session peak equity cannot be below realized high-water mark")
        if not self.demo_only or self.live_mainnet_order_routing_allowed:
            raise ValueError("demo session ledger cannot grant live routing")

    @property
    def realized_peak_equity_usdt(self) -> Decimal:
        cumulative = _ZERO
        realized_peak = self.opening_equity_usdt
        for outcome in self.outcomes:
            cumulative += outcome.all_in_net_pnl_usdt
            realized_peak = max(realized_peak, self.opening_equity_usdt + cumulative)
        return realized_peak

    @property
    def effective_peak_equity_usdt(self) -> Decimal:
        stored_peak = (
            self.opening_equity_usdt
            if self.peak_equity_usdt is None
            else self.peak_equity_usdt
        )
        return max(stored_peak, self.realized_peak_equity_usdt)

    def to_session_risk_state(self, *, current_equity_usdt: Decimal) -> CryptoSessionRiskState:
        self.validate()
        if not current_equity_usdt.is_finite() or current_equity_usdt <= 0:
            raise ValueError("demo session current equity must be positive and finite")
        realized = sum(
            (outcome.all_in_net_pnl_usdt for outcome in self.outcomes),
            start=_ZERO,
        )
        signed_execution_fees = sum(
            (outcome.execution_fees_usdt for outcome in self.outcomes),
            start=_ZERO,
        )
        execution_cost = max(_ZERO, signed_execution_fees)
        peak = max(self.effective_peak_equity_usdt, current_equity_usdt)
        streak = 0
        for outcome in reversed(self.outcomes):
            if outcome.all_in_net_pnl_usdt < 0:
                streak += 1
                continue
            break
        state = CryptoSessionRiskState(
            opening_equity_usdt=self.opening_equity_usdt,
            current_equity_usdt=current_equity_usdt,
            peak_equity_usdt=peak,
            realized_pnl_usdt=realized,
            execution_cost_usdt=execution_cost,
            consecutive_losses=streak,
        )
        state.validate()
        return state


def start_bybit_demo_session_risk_ledger(
    *,
    opening_equity_usdt: Decimal,
) -> BybitDemoSessionRiskLedger:
    ledger = BybitDemoSessionRiskLedger(
        opening_equity_usdt=opening_equity_usdt,
        peak_equity_usdt=opening_equity_usdt,
    )
    ledger.validate()
    return ledger


def observe_bybit_demo_session_equity(
    ledger: BybitDemoSessionRiskLedger,
    *,
    current_equity_usdt: Decimal,
) -> BybitDemoSessionRiskLedger:
    """Advance the durable session high-water mark from a real wallet observation."""

    ledger.validate()
    if not current_equity_usdt.is_finite() or current_equity_usdt <= 0:
        raise ValueError("demo session current equity must be positive and finite")
    observed_peak = max(ledger.effective_peak_equity_usdt, current_equity_usdt)
    if ledger.peak_equity_usdt == observed_peak:
        return ledger
    updated = BybitDemoSessionRiskLedger(
        opening_equity_usdt=ledger.opening_equity_usdt,
        outcomes=ledger.outcomes,
        peak_equity_usdt=observed_peak,
    )
    updated.validate()
    return updated


def apply_fully_reconciled_trade_to_session_ledger(
    ledger: BybitDemoSessionRiskLedger,
    snapshot: BybitDemoPostTradeAccountingResult,
) -> BybitDemoSessionRiskLedger:
    """Idempotently add one final all-in trade outcome to the demo session-risk ledger."""

    ledger.validate()
    if snapshot.live_mainnet_order_routing_allowed:
        raise ValueError("demo session ledger rejected a mainnet-capable snapshot")
    if snapshot.profit_outcome_status in {
        BybitDemoProfitOutcomeStatus.TRADE_OPEN,
        BybitDemoProfitOutcomeStatus.ALL_IN_ACCOUNTING_PENDING,
    }:
        raise ValueError("demo session ledger requires fully reconciled all-in PnL")
    if not snapshot.trade.terminal:
        raise ValueError("demo session ledger requires a terminal trade")
    all_in = snapshot.fully_reconciled_all_in_net_pnl_usdt
    account = snapshot.account_pnl
    if all_in is None or account is None or account.matched_record is None:
        raise ValueError("demo session ledger requires matched account closed-PnL evidence")
    record = account.matched_record
    outcome = BybitDemoSessionTradeOutcome(
        entry_order_link_id=snapshot.trade.entry_order_link_id,
        symbol=snapshot.trade.symbol,
        created_time_ms=record.created_time_ms,
        updated_time_ms=record.updated_time_ms,
        all_in_net_pnl_usdt=all_in,
        execution_fees_usdt=snapshot.trade.execution_fees_usdt,
    )
    outcome.validate()

    existing = {
        item.entry_order_link_id: item
        for item in ledger.outcomes
    }.get(outcome.entry_order_link_id)
    if existing is not None:
        if existing != outcome:
            raise ValueError("demo session trade key was reconciled with conflicting economics")
        return ledger

    outcomes = tuple(
        sorted(
            (*ledger.outcomes, outcome),
            key=lambda item: (
                item.updated_time_ms,
                item.created_time_ms,
                item.entry_order_link_id,
            ),
        )
    )
    candidate = BybitDemoSessionRiskLedger(
        opening_equity_usdt=ledger.opening_equity_usdt,
        outcomes=outcomes,
    )
    updated = BybitDemoSessionRiskLedger(
        opening_equity_usdt=ledger.opening_equity_usdt,
        outcomes=outcomes,
        peak_equity_usdt=max(
            ledger.effective_peak_equity_usdt,
            candidate.realized_peak_equity_usdt,
        ),
    )
    updated.validate()
    return updated
