from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.execution.bybit_demo_account_reader import (
    BybitDemoAccountInfo,
    BybitDemoWalletBalance,
)
from app.execution.bybit_demo_account_sized_strategy import (
    BybitDemoAccountSizedCycleStatus,
    execute_account_sized_reconciled_guarded_bybit_demo_cycle,
)
from app.execution.bybit_demo_cycle import BybitDemoCyclePolicy
from app.execution.bybit_demo_strategy_selector import (
    BybitDemoStrategyCycleResult,
    BybitDemoStrategyCycleStatus,
    BybitDemoStrategySelection,
    BybitDemoStrategySelectionStatus,
)
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState


def _session(
    *,
    current: str = "1000",
    peak: str = "1000",
) -> CryptoSessionRiskState:
    return CryptoSessionRiskState(
        opening_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal(current),
        peak_equity_usdt=Decimal(peak),
        realized_pnl_usdt=Decimal("-5"),
        execution_cost_usdt=Decimal("2"),
        consecutive_losses=1,
    )


def _wallet(
    *,
    equity: str = "800",
    available: str = "700",
) -> BybitDemoWalletBalance:
    return BybitDemoWalletBalance(
        total_equity_usd=Decimal(equity),
        total_wallet_balance_usd=Decimal(equity),
        total_margin_balance_usd=Decimal(equity),
        total_available_balance_usd=Decimal(available),
        total_perp_upl_usd=Decimal("0"),
        total_initial_margin_usd=Decimal("10"),
        total_maintenance_margin_usd=Decimal("2"),
    )


def _info(margin_mode: str = "REGULAR_MARGIN") -> BybitDemoAccountInfo:
    return BybitDemoAccountInfo(
        margin_mode=margin_mode,
        unified_margin_status=5,
        updated_time_ms=1787076000000,
    )


class _AccountingReader:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(
        self,
        *,
        wallet: BybitDemoWalletBalance | None = None,
        info: BybitDemoAccountInfo | None = None,
        fail: bool = False,
    ) -> None:
        self.wallet = _wallet() if wallet is None else wallet
        self.info = _info() if info is None else info
        self.fail = fail
        self.calls: list[str] = []

    def get_account_info(self) -> BybitDemoAccountInfo:
        self.calls.append("info")
        if self.fail:
            raise RuntimeError("account-info-down")
        return self.info

    def get_wallet_balance(self) -> BybitDemoWalletBalance:
        self.calls.append("wallet")
        if self.fail:
            raise RuntimeError("wallet-down")
        return self.wallet


def _strategy_result() -> BybitDemoStrategyCycleResult:
    selection = BybitDemoStrategySelection(
        status=BybitDemoStrategySelectionStatus.NO_EXECUTABLE_PLAN,
        reasons=("TEST_NO_PLAN",),
        selected_trade_plan=None,
        selected_entry_preflight=None,
        selected_signal_rank=None,
        candidate_audit=(),
        executable_candidate_count=0,
        economic_shadow_selected_symbol=None,
        economic_shadow_selected_side=None,
        economic_shadow_differs_from_current=False,
    )
    return BybitDemoStrategyCycleResult(
        status=BybitDemoStrategyCycleStatus.NO_TRADE,
        selection=selection,
        orchestrator_result=None,
    )


def _call(
    *,
    session_state: CryptoSessionRiskState | None = None,
    accounting_client: object | None = None,
    writes_enabled: bool,
    executor: object,
):
    return execute_account_sized_reconciled_guarded_bybit_demo_cycle(
        {},
        instruments={},
        strategy_config=CryptoPerpStrategyConfig(target_net_profit_usd=Decimal("20")),
        session_state=_session() if session_state is None else session_state,
        now=datetime(2026, 8, 18, tzinfo=UTC),
        client=object(),
        accounting_client=accounting_client,
        cycle_policy=BybitDemoCyclePolicy(writes_enabled=writes_enabled),
        strategy_cycle_executor=executor,
    )


def test_dry_run_preserves_supplied_session_state_without_account_access() -> None:
    observed: dict[str, object] = {}

    def executor(*_args: object, **kwargs: object) -> BybitDemoStrategyCycleResult:
        observed["session"] = kwargs["session_state"]
        return _strategy_result()

    original = _session(current="950", peak="1000")
    result = _call(
        session_state=original,
        accounting_client=None,
        writes_enabled=False,
        executor=executor,
    )

    assert result.status is BybitDemoAccountSizedCycleStatus.STRATEGY_CYCLE_CALLED
    assert result.account_state_checked is False
    assert observed["session"] is original
    assert result.effective_session_equity_usdt == Decimal("950")


def test_explicit_write_refreshes_equity_before_strategy_sizing() -> None:
    observed: dict[str, object] = {}
    reader = _AccountingReader(wallet=_wallet(equity="800", available="650"))

    def executor(*_args: object, **kwargs: object) -> BybitDemoStrategyCycleResult:
        observed["session"] = kwargs["session_state"]
        observed["accounting_client"] = kwargs["accounting_client"]
        return _strategy_result()

    result = _call(
        accounting_client=reader,
        writes_enabled=True,
        executor=executor,
    )

    refreshed = observed["session"]
    assert isinstance(refreshed, CryptoSessionRiskState)
    assert refreshed.current_equity_usdt == Decimal("800")
    assert refreshed.peak_equity_usdt == Decimal("1000")
    assert refreshed.opening_equity_usdt == Decimal("1000")
    assert refreshed.realized_pnl_usdt == Decimal("-5")
    assert refreshed.execution_cost_usdt == Decimal("2")
    assert refreshed.consecutive_losses == 1
    assert observed["accounting_client"] is reader
    assert reader.calls == ["info", "wallet"]
    assert result.account_state_checked is True
    assert result.original_session_equity_usdt == Decimal("1000")
    assert result.effective_session_equity_usdt == Decimal("800")
    assert result.margin_mode == "REGULAR_MARGIN"


def test_wallet_equity_above_remembered_peak_advances_peak_without_rewriting_opening() -> None:
    observed: dict[str, object] = {}
    reader = _AccountingReader(wallet=_wallet(equity="1100", available="900"))

    def executor(*_args: object, **kwargs: object) -> BybitDemoStrategyCycleResult:
        observed["session"] = kwargs["session_state"]
        return _strategy_result()

    result = _call(
        accounting_client=reader,
        writes_enabled=True,
        executor=executor,
    )
    refreshed = observed["session"]
    assert isinstance(refreshed, CryptoSessionRiskState)
    assert refreshed.current_equity_usdt == Decimal("1100")
    assert refreshed.peak_equity_usdt == Decimal("1100")
    assert refreshed.opening_equity_usdt == Decimal("1000")
    assert result.effective_peak_equity_usdt == Decimal("1100")


def test_explicit_write_requires_get_only_account_reader() -> None:
    called = False

    def executor(*_args: object, **_kwargs: object) -> BybitDemoStrategyCycleResult:
        nonlocal called
        called = True
        return _strategy_result()

    result = _call(
        accounting_client=None,
        writes_enabled=True,
        executor=executor,
    )
    assert result.status is BybitDemoAccountSizedCycleStatus.ACCOUNT_STATE_BLOCKED
    assert result.reasons == ("DEMO_ACCOUNT_READER_REQUIRED_FOR_WRITES",)
    assert called is False


@pytest.mark.parametrize("margin_mode", ["ISOLATED_MARGIN", "PORTFOLIO_MARGIN"])
def test_incompatible_margin_mode_blocks_before_strategy_or_order(margin_mode: str) -> None:
    called = False
    reader = _AccountingReader(info=_info(margin_mode))

    def executor(*_args: object, **_kwargs: object) -> BybitDemoStrategyCycleResult:
        nonlocal called
        called = True
        return _strategy_result()

    result = _call(
        accounting_client=reader,
        writes_enabled=True,
        executor=executor,
    )
    assert result.status is BybitDemoAccountSizedCycleStatus.ACCOUNT_STATE_BLOCKED
    assert result.margin_mode == margin_mode
    assert result.reasons == (
        f"DEMO_MARGIN_MODE_UNSUPPORTED_FOR_CURRENT_RISK_MODEL:{margin_mode}",
    )
    assert called is False


def test_nonpositive_available_balance_blocks_before_strategy_or_order() -> None:
    called = False
    reader = _AccountingReader(wallet=_wallet(equity="800", available="0"))

    def executor(*_args: object, **_kwargs: object) -> BybitDemoStrategyCycleResult:
        nonlocal called
        called = True
        return _strategy_result()

    result = _call(
        accounting_client=reader,
        writes_enabled=True,
        executor=executor,
    )
    assert result.status is BybitDemoAccountSizedCycleStatus.ACCOUNT_STATE_BLOCKED
    assert result.reasons == ("DEMO_AVAILABLE_BALANCE_NOT_POSITIVE",)
    assert called is False


def test_account_read_failure_blocks_before_strategy_or_order() -> None:
    called = False
    reader = _AccountingReader(fail=True)

    def executor(*_args: object, **_kwargs: object) -> BybitDemoStrategyCycleResult:
        nonlocal called
        called = True
        return _strategy_result()

    result = _call(
        accounting_client=reader,
        writes_enabled=True,
        executor=executor,
    )
    assert result.status is BybitDemoAccountSizedCycleStatus.ACCOUNT_STATE_BLOCKED
    assert result.reasons == ("DEMO_ACCOUNT_STATE_READ_FAILED:RuntimeError",)
    assert called is False


def test_mainnet_capable_or_write_capable_account_reader_is_rejected() -> None:
    class UnsafeReader(_AccountingReader):
        live_mainnet_order_routing_allowed = True

    with pytest.raises(ValueError, match="mainnet-capable"):
        _call(
            accounting_client=UnsafeReader(),
            writes_enabled=True,
            executor=lambda *_args, **_kwargs: _strategy_result(),
        )

    class WriteCapableReader(_AccountingReader):
        order_writes_supported = True

    with pytest.raises(ValueError, match="GET-only"):
        _call(
            accounting_client=WriteCapableReader(),
            writes_enabled=True,
            executor=lambda *_args, **_kwargs: _strategy_result(),
        )
