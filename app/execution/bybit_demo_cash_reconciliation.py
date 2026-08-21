from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.execution.bybit_demo_session_risk_ledger import BybitDemoSessionRiskLedger

_ZERO = Decimal("0")


@dataclass(frozen=True)
class BybitDemoCashBaseline:
    currency: str
    wallet_balance_usdt: Decimal
    cumulative_all_in_pnl_usdt: Decimal
    session_revision: str
    created_time_ms: int
    demo_only: bool = True
    live_mainnet_order_routing_allowed: bool = False

    def validate(self) -> None:
        if self.currency != "USDT":
            raise ValueError("Bybit cash baseline currency must be USDT")
        if not self.wallet_balance_usdt.is_finite():
            raise ValueError("Bybit cash baseline wallet balance must be finite")
        if not self.cumulative_all_in_pnl_usdt.is_finite():
            raise ValueError("Bybit cash baseline cumulative PnL must be finite")
        if len(self.session_revision) != 64 or any(
            character not in "0123456789abcdef" for character in self.session_revision
        ):
            raise ValueError("Bybit cash baseline session revision must be sha256 hex")
        if (
            isinstance(self.created_time_ms, bool)
            or not isinstance(self.created_time_ms, int)
            or self.created_time_ms < 0
        ):
            raise ValueError("Bybit cash baseline timestamp must be non-negative integer ms")
        if not self.demo_only or self.live_mainnet_order_routing_allowed:
            raise ValueError("Bybit cash baseline cannot grant live routing")


@dataclass(frozen=True)
class BybitDemoCashReconciliation:
    baseline_wallet_balance_usdt: Decimal
    baseline_cumulative_all_in_pnl_usdt: Decimal
    current_cumulative_all_in_pnl_usdt: Decimal
    expected_wallet_balance_usdt: Decimal | None
    broker_wallet_balance_usdt: Decimal | None
    cash_mismatch_usdt: Decimal | None
    active_trade_deferred: bool
    reasons: tuple[str, ...]
    demo_only: bool = True
    live_mainnet_order_routing_allowed: bool = False


def cumulative_all_in_pnl_usdt(ledger: BybitDemoSessionRiskLedger) -> Decimal:
    ledger.validate()
    return sum(
        (outcome.all_in_net_pnl_usdt for outcome in ledger.outcomes),
        start=_ZERO,
    )


def build_bybit_demo_cash_baseline(
    ledger: BybitDemoSessionRiskLedger,
    *,
    session_revision: str,
    broker_wallet_balance_usdt: Decimal,
    created_time_ms: int,
) -> BybitDemoCashBaseline:
    ledger.validate()
    baseline = BybitDemoCashBaseline(
        currency="USDT",
        wallet_balance_usdt=broker_wallet_balance_usdt,
        cumulative_all_in_pnl_usdt=cumulative_all_in_pnl_usdt(ledger),
        session_revision=session_revision,
        created_time_ms=created_time_ms,
    )
    baseline.validate()
    return baseline


def reconcile_bybit_demo_cash(
    baseline: BybitDemoCashBaseline,
    ledger: BybitDemoSessionRiskLedger,
    *,
    broker_wallet_balance_usdt: Decimal | None,
    active_trade: bool,
) -> BybitDemoCashReconciliation:
    """Measure unexplained flat-state USDT wallet movement against durable local economics.

    While a trade is active, entry/exit fees and funding can move wallet cash before the terminal
    all-in outcome is persisted. Cash reconciliation therefore stays unavailable rather than
    reporting a false mismatch. In flat state, any movement not explained by fully reconciled
    terminal outcomes remains visible as a mismatch; external transfers are intentionally not
    auto-accepted by this first production cash boundary.
    """

    baseline.validate()
    ledger.validate()
    if not isinstance(active_trade, bool):
        raise ValueError("Bybit cash active-trade state must be boolean")
    current_cumulative = cumulative_all_in_pnl_usdt(ledger)
    if active_trade:
        return BybitDemoCashReconciliation(
            baseline_wallet_balance_usdt=baseline.wallet_balance_usdt,
            baseline_cumulative_all_in_pnl_usdt=baseline.cumulative_all_in_pnl_usdt,
            current_cumulative_all_in_pnl_usdt=current_cumulative,
            expected_wallet_balance_usdt=None,
            broker_wallet_balance_usdt=None,
            cash_mismatch_usdt=None,
            active_trade_deferred=True,
            reasons=("ACTIVE_TRADE_CASH_RECONCILIATION_DEFERRED",),
        )
    if broker_wallet_balance_usdt is None:
        return BybitDemoCashReconciliation(
            baseline_wallet_balance_usdt=baseline.wallet_balance_usdt,
            baseline_cumulative_all_in_pnl_usdt=baseline.cumulative_all_in_pnl_usdt,
            current_cumulative_all_in_pnl_usdt=current_cumulative,
            expected_wallet_balance_usdt=None,
            broker_wallet_balance_usdt=None,
            cash_mismatch_usdt=None,
            active_trade_deferred=False,
            reasons=("BROKER_USDT_WALLET_BALANCE_UNAVAILABLE",),
        )
    if not broker_wallet_balance_usdt.is_finite():
        raise ValueError("Bybit broker USDT wallet balance must be finite")
    expected = baseline.wallet_balance_usdt + (
        current_cumulative - baseline.cumulative_all_in_pnl_usdt
    )
    if not expected.is_finite():
        raise ValueError("Bybit expected USDT wallet balance must be finite")
    mismatch = abs(broker_wallet_balance_usdt - expected)
    return BybitDemoCashReconciliation(
        baseline_wallet_balance_usdt=baseline.wallet_balance_usdt,
        baseline_cumulative_all_in_pnl_usdt=baseline.cumulative_all_in_pnl_usdt,
        current_cumulative_all_in_pnl_usdt=current_cumulative,
        expected_wallet_balance_usdt=expected,
        broker_wallet_balance_usdt=broker_wallet_balance_usdt,
        cash_mismatch_usdt=mismatch,
        active_trade_deferred=False,
        reasons=() if mismatch == _ZERO else ("UNEXPLAINED_USDT_WALLET_DELTA",),
    )
