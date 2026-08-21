from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.application.bybit_product_composition as product
from app.execution.bybit_demo_session_risk_ledger import start_bybit_demo_session_risk_ledger


class _LeaseStore:
    def __init__(self) -> None:
        self.released: list[str] = []

    def acquire(self):
        return SimpleNamespace(owner_token="owner-1")

    def release(self, *, owner_token: str) -> None:
        self.released.append(owner_token)


class _SessionStore:
    def __init__(self) -> None:
        self.checkpoint = SimpleNamespace(
            ledger=start_bybit_demo_session_risk_ledger(
                opening_equity_usdt=Decimal("1000")
            ),
            revision="a" * 64,
        )
        self.load_calls = 0

    def load_current(self):
        self.load_calls += 1
        return self.checkpoint


class _Accounting:
    def __init__(self, *, usdt_wallet_balance: Decimal | None) -> None:
        self.usdt_wallet_balance = usdt_wallet_balance
        self.calls = 0

    def get_wallet_balance(self):
        self.calls += 1
        return SimpleNamespace(
            total_equity_usd=Decimal("1000"),
            usdt_wallet_balance=self.usdt_wallet_balance,
        )


class _CashStore:
    def __init__(self) -> None:
        self.initialized = []

    def initialize(self, baseline):
        self.initialized.append(baseline)
        return baseline


class _StartupReconciler:
    def __init__(self, *, next_entry_allowed: bool) -> None:
        self.next_entry_allowed = next_entry_allowed
        self.run_calls = 0

    def run(self):
        self.run_calls += 1
        return SimpleNamespace(next_entry_allowed=self.next_entry_allowed)


def _patch_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    *,
    next_entry_allowed: bool,
    usdt_wallet_balance: Decimal | None,
):
    lease = _LeaseStore()
    session = _SessionStore()
    accounting = _Accounting(usdt_wallet_balance=usdt_wallet_balance)
    cash = _CashStore()
    startup = _StartupReconciler(next_entry_allowed=next_entry_allowed)

    monkeypatch.setattr(product, "PostgresBybitDemoRuntimeLease", lambda _dsn: lease)
    monkeypatch.setattr(
        product,
        "PostgresBybitDemoExcursionStore",
        lambda _dsn, *, runtime_lease: SimpleNamespace(runtime_lease=runtime_lease),
    )
    monkeypatch.setattr(product, "PostgresBybitEntryOms", lambda _dsn: object())
    monkeypatch.setattr(
        product,
        "BybitDemoBrokerTruthClient",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        product,
        "BybitDemoAccountingClient",
        lambda **_kwargs: accounting,
    )
    monkeypatch.setattr(
        product,
        "PostgresBybitDemoSessionRiskLedgerStore",
        lambda _dsn: session,
    )
    monkeypatch.setattr(
        product,
        "PostgresBybitDemoCashBaselineStore",
        lambda _dsn: cash,
    )

    class _Factory:
        def __init__(self, **_kwargs) -> None:
            pass

        def run(self):
            return startup.run()

    monkeypatch.setattr(product, "BybitProductStartupReconciler", _Factory)
    return lease, session, accounting, cash, startup


def _config():
    return product.BybitProductConfig.from_env(
        {
            "ASTRA_ENV": "demo",
            "ASTRA_BROKER": "bybit",
            "ASTRA_SYMBOLS": "",
            "BYBIT_API_KEY": "demo-key",
            "BYBIT_API_SECRET": "demo-secret",
            "DATABASE_URL": "postgresql://astra:secret@db/astra",
            "TRADING_WRITES_ENABLED": "false",
            "MAINNET_ENABLED": "false",
        }
    )


def test_cash_bootstrap_requires_flat_reconciliation_then_persists_exact_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, session, accounting, cash, startup = _patch_bootstrap(
        monkeypatch,
        next_entry_allowed=True,
        usdt_wallet_balance=Decimal("1000.25"),
    )

    baseline = product.bootstrap_bybit_product_cash_baseline(
        _config(),
        clock_ms=lambda: 1_700_000_000_000,
    )

    assert startup.run_calls == 1
    assert session.load_calls == 1
    assert accounting.calls == 1
    assert cash.initialized == [baseline]
    assert baseline.wallet_balance_usdt == Decimal("1000.25")
    assert baseline.cumulative_all_in_pnl_usdt == Decimal("0")
    assert baseline.session_revision == "a" * 64
    assert baseline.created_time_ms == 1_700_000_000_000
    assert baseline.live_mainnet_order_routing_allowed is False
    assert lease.released == ["owner-1"]


def test_cash_bootstrap_blocks_before_wallet_or_database_write_when_state_is_not_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, session, accounting, cash, _startup = _patch_bootstrap(
        monkeypatch,
        next_entry_allowed=False,
        usdt_wallet_balance=Decimal("1000"),
    )

    with pytest.raises(RuntimeError, match="fully reconciled flat"):
        product.bootstrap_bybit_product_cash_baseline(_config())

    assert session.load_calls == 0
    assert accounting.calls == 0
    assert cash.initialized == []
    assert lease.released == ["owner-1"]


def test_cash_bootstrap_rejects_missing_usdt_wallet_and_releases_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, session, accounting, cash, _startup = _patch_bootstrap(
        monkeypatch,
        next_entry_allowed=True,
        usdt_wallet_balance=None,
    )

    with pytest.raises(RuntimeError, match="requires broker USDT wallet balance"):
        product.bootstrap_bybit_product_cash_baseline(_config())

    assert session.load_calls == 1
    assert accounting.calls == 1
    assert cash.initialized == []
    assert lease.released == ["owner-1"]
