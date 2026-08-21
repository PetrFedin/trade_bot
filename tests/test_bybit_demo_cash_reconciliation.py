from decimal import Decimal

from app.execution.bybit_demo_cash_reconciliation import (
    build_bybit_demo_cash_baseline,
    cumulative_all_in_pnl_usdt,
    reconcile_bybit_demo_cash,
)
from app.execution.bybit_demo_session_risk_ledger import (
    BybitDemoSessionRiskLedger,
    BybitDemoSessionTradeOutcome,
)


def _ledger(*pnls: str) -> BybitDemoSessionRiskLedger:
    outcomes = tuple(
        BybitDemoSessionTradeOutcome(
            entry_order_link_id=f"ASTRA-DEMO-E-CASH-{index}",
            symbol="BTCUSDT",
            created_time_ms=index * 100,
            updated_time_ms=index * 100 + 50,
            all_in_net_pnl_usdt=Decimal(pnl),
            execution_fees_usdt=Decimal("0.1"),
        )
        for index, pnl in enumerate(pnls, start=1)
    )
    return BybitDemoSessionRiskLedger(
        opening_equity_usdt=Decimal("1000"),
        outcomes=outcomes,
    )


def test_cash_baseline_captures_local_cumulative_all_in_state() -> None:
    ledger = _ledger("5", "-2")

    baseline = build_bybit_demo_cash_baseline(
        ledger,
        session_revision="a" * 64,
        broker_wallet_balance_usdt=Decimal("1003"),
        created_time_ms=1_700_000_000_000,
    )

    assert baseline.wallet_balance_usdt == Decimal("1003")
    assert baseline.cumulative_all_in_pnl_usdt == Decimal("3")
    assert cumulative_all_in_pnl_usdt(ledger) == Decimal("3")
    assert baseline.live_mainnet_order_routing_allowed is False


def test_flat_cash_reconciliation_matches_only_explained_local_delta() -> None:
    baseline_ledger = _ledger("5", "-2")
    baseline = build_bybit_demo_cash_baseline(
        baseline_ledger,
        session_revision="b" * 64,
        broker_wallet_balance_usdt=Decimal("1003"),
        created_time_ms=1_700_000_000_000,
    )
    current = _ledger("5", "-2", "4")

    reconciled = reconcile_bybit_demo_cash(
        baseline,
        current,
        broker_wallet_balance_usdt=Decimal("1007"),
        active_trade=False,
    )
    mismatch = reconcile_bybit_demo_cash(
        baseline,
        current,
        broker_wallet_balance_usdt=Decimal("1006.25"),
        active_trade=False,
    )

    assert reconciled.expected_wallet_balance_usdt == Decimal("1007")
    assert reconciled.cash_mismatch_usdt == Decimal("0")
    assert reconciled.reasons == ()
    assert mismatch.expected_wallet_balance_usdt == Decimal("1007")
    assert mismatch.cash_mismatch_usdt == Decimal("0.75")
    assert mismatch.reasons == ("UNEXPLAINED_USDT_WALLET_DELTA",)


def test_active_trade_cash_reconciliation_is_unknown_not_false_mismatch() -> None:
    baseline = build_bybit_demo_cash_baseline(
        _ledger(),
        session_revision="c" * 64,
        broker_wallet_balance_usdt=Decimal("1000"),
        created_time_ms=1_700_000_000_000,
    )

    result = reconcile_bybit_demo_cash(
        baseline,
        _ledger(),
        broker_wallet_balance_usdt=Decimal("997"),
        active_trade=True,
    )

    assert result.cash_mismatch_usdt is None
    assert result.expected_wallet_balance_usdt is None
    assert result.broker_wallet_balance_usdt is None
    assert result.active_trade_deferred is True
    assert result.reasons == ("ACTIVE_TRADE_CASH_RECONCILIATION_DEFERRED",)


def test_missing_broker_usdt_wallet_balance_stays_unknown() -> None:
    baseline = build_bybit_demo_cash_baseline(
        _ledger(),
        session_revision="d" * 64,
        broker_wallet_balance_usdt=Decimal("1000"),
        created_time_ms=1_700_000_000_000,
    )

    result = reconcile_bybit_demo_cash(
        baseline,
        _ledger(),
        broker_wallet_balance_usdt=None,
        active_trade=False,
    )

    assert result.cash_mismatch_usdt is None
    assert result.reasons == ("BROKER_USDT_WALLET_BALANCE_UNAVAILABLE",)
