from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.execution.bybit_demo_persistent_supervisor import (
    BybitDemoPersistentSupervisorStatus,
    run_bybit_demo_persistent_supervisor_cycle,
)
from app.execution.bybit_demo_session_risk_runtime import BybitDemoSessionRiskObservation
from app.execution.bybit_demo_trading_runtime import BybitDemoTradingRuntimeStatus
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState

_NOW = datetime(2026, 8, 26, 15, tzinfo=UTC)


class _Safe:
    live_mainnet_order_routing_allowed = False


class _OrderClient(_Safe):
    environment = "BYBIT_DEMO"


class _Accounting(_Safe):
    order_writes_supported = False

    def __init__(self) -> None:
        self.wallet_reads = 0

    def get_wallet_balance(self):
        self.wallet_reads += 1
        return SimpleNamespace(
            total_equity_usd=Decimal("1040"),
            validate=lambda: None,
        )


class _Excursion(_Safe):
    order_writes_supported = False

    def __init__(self, *, active: bool = True, error: Exception | None = None) -> None:
        self.active = active
        self.error = error
        self.load_calls = 0

    def load(self):
        self.load_calls += 1
        if self.error is not None:
            raise self.error
        if not self.active:
            raise FileNotFoundError
        return SimpleNamespace(state=SimpleNamespace(symbol="BTCUSDT"))


class _Lease(_Safe):
    order_writes_supported = False
    automatic_stale_takeover_allowed = False


class _Evidence(_Safe):
    order_writes_supported = False


class _RiskDependency(_Safe):
    order_writes_supported = False
    automatic_reset_allowed = False


class _Observer(_RiskDependency):
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.values: list[Decimal] = []

    def observe(self, *, current_equity_usdt: Decimal) -> BybitDemoSessionRiskObservation:
        self.values.append(current_equity_usdt)
        if self.fail:
            raise RuntimeError("risk database unavailable")
        return BybitDemoSessionRiskObservation(
            ledger_revision_sha256="a" * 64,
            outcome_count=2,
            session_state=CryptoSessionRiskState(
                opening_equity_usdt=Decimal("1000"),
                current_equity_usdt=current_equity_usdt,
                peak_equity_usdt=Decimal("1100"),
                realized_pnl_usdt=Decimal("-10"),
                execution_cost_usdt=Decimal("4"),
                consecutive_losses=1,
            ),
            high_water_advanced=False,
        )


def _instrument() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.1"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("1000"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _call(*, excursion=None, accounting=None, observer=None, canonical=None):
    active_excursion = _Excursion() if excursion is None else excursion
    active_accounting = _Accounting() if accounting is None else accounting
    active_observer = _Observer() if observer is None else observer
    seen: dict[str, object] = {}

    def _canonical(_bars, **kwargs):
        seen.update(kwargs)
        if canonical is not None:
            return canonical(kwargs)
        return SimpleNamespace(
            status=BybitDemoTradingRuntimeStatus.ACTIVE_TRADE_POLLED,
            reasons=("OPEN_POSITION_MANAGED",),
            same_invocation_additional_entry_allowed=False,
            live_mainnet_order_routing_allowed=False,
        )

    result = run_bybit_demo_persistent_supervisor_cycle(
        instruments={"BTCUSDT": _instrument()},
        strategy_config=CryptoPerpStrategyConfig(),
        now=_NOW,
        now_ms=int(_NOW.timestamp() * 1000),
        client=_OrderClient(),
        accounting_client=active_accounting,
        excursion_store=active_excursion,
        completed_bar_client=_Safe(),
        quote_client=_Safe(),
        runtime_lease=_Lease(),
        terminal_evidence_store=_Evidence(),
        session_risk_committer=_RiskDependency(),
        session_risk_observer=active_observer,
        canonical_runtime=_canonical,
    )
    return result, seen, active_accounting, active_observer


def test_missing_checkpoint_is_idle_and_never_reads_wallet_or_runtime() -> None:
    excursion = _Excursion(active=False)
    accounting = _Accounting()
    runtime_called = False

    def _canonical(_kwargs):
        nonlocal runtime_called
        runtime_called = True
        raise AssertionError("idle supervisor must not call runtime")

    result, _, _, _ = _call(
        excursion=excursion,
        accounting=accounting,
        canonical=_canonical,
    )

    assert result.status is BybitDemoPersistentSupervisorStatus.IDLE_NO_ACTIVE_TRADE
    assert result.new_entry_attempted is False
    assert result.autonomous_entry_allowed is False
    assert result.next_entry_allowed is False
    assert accounting.wallet_reads == 0
    assert runtime_called is False


def test_active_trade_uses_wallet_backed_durable_session_state() -> None:
    result, seen, accounting, observer = _call()

    assert result.status is BybitDemoPersistentSupervisorStatus.ACTIVE_TRADE_CYCLE
    assert result.active_symbol == "BTCUSDT"
    assert accounting.wallet_reads == 1
    assert observer.values == [Decimal("1040")]
    assert seen["session_state"].current_equity_usdt == Decimal("1040")
    assert seen["session_state"].peak_equity_usdt == Decimal("1100")
    assert seen["session_risk_committer"].automatic_reset_allowed is False
    assert result.new_entry_attempted is False
    assert result.same_invocation_additional_entry_allowed is False


def test_race_to_missing_checkpoint_cannot_fall_through_to_new_entry() -> None:
    def _canonical(kwargs):
        with pytest.raises(RuntimeError, match="forbids new entry"):
            kwargs["entry_executor"]()
        return SimpleNamespace(
            status=BybitDemoTradingRuntimeStatus.RUNTIME_BLOCKED,
            reasons=("PERSISTENT_ENTRY_BLOCKED",),
            same_invocation_additional_entry_allowed=False,
            live_mainnet_order_routing_allowed=False,
        )

    result, _, _, _ = _call(canonical=_canonical)

    assert result.status is BybitDemoPersistentSupervisorStatus.BLOCKED
    assert result.reasons == ("PERSISTENT_ENTRY_BLOCKED",)
    assert result.new_entry_attempted is False


def test_terminal_handoff_never_grants_supervisor_reentry() -> None:
    def _canonical(_kwargs):
        return SimpleNamespace(
            status=BybitDemoTradingRuntimeStatus.TERMINAL_HANDOFF_COMPLETE,
            reasons=(),
            same_invocation_additional_entry_allowed=False,
            live_mainnet_order_routing_allowed=False,
        )

    result, _, _, _ = _call(canonical=_canonical)

    assert result.status is BybitDemoPersistentSupervisorStatus.TERMINAL_HANDOFF_COMPLETE
    assert result.next_entry_allowed is False
    assert result.autonomous_entry_allowed is False
    assert result.operator_approval_bypass_allowed is False


def test_unknown_session_risk_blocks_before_canonical_runtime() -> None:
    runtime_called = False

    def _canonical(_kwargs):
        nonlocal runtime_called
        runtime_called = True
        raise AssertionError("unknown risk must block runtime")

    result, _, _, _ = _call(
        observer=_Observer(fail=True),
        canonical=_canonical,
    )

    assert result.status is BybitDemoPersistentSupervisorStatus.BLOCKED
    assert result.reasons == ("PERSISTENT_SUPERVISOR_SESSION_RISK_FAILED:RuntimeError",)
    assert runtime_called is False


def test_mainnet_capable_order_client_is_rejected() -> None:
    unsafe = _OrderClient()
    unsafe.live_mainnet_order_routing_allowed = True

    with pytest.raises(ValueError, match="mainnet-capable order client"):
        run_bybit_demo_persistent_supervisor_cycle(
            instruments={"BTCUSDT": _instrument()},
            strategy_config=CryptoPerpStrategyConfig(),
            now=_NOW,
            now_ms=int(_NOW.timestamp() * 1000),
            client=unsafe,
            accounting_client=_Accounting(),
            excursion_store=_Excursion(),
            completed_bar_client=_Safe(),
            quote_client=_Safe(),
            runtime_lease=_Lease(),
            terminal_evidence_store=_Evidence(),
            session_risk_committer=_RiskDependency(),
            session_risk_observer=_Observer(),
        )
