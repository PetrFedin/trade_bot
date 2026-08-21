from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.application.bybit_product_composition as product
from app.execution.bybit_demo_cash_reconciliation import BybitDemoCashBaseline
from app.execution.bybit_demo_session_risk_ledger import start_bybit_demo_session_risk_ledger
from app.runtime.bybit_product_config import BybitProductConfig
from app.strategy.crypto_perp import CryptoPerpStrategyConfig


class _ExcursionStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, checkpoint=None) -> None:
        self.checkpoint = checkpoint

    def load(self):
        if self.checkpoint is None:
            raise FileNotFoundError
        return self.checkpoint


class _SessionStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, *, missing: bool = False, fail_save: bool = False) -> None:
        self.missing = missing
        self.fail_save = fail_save
        self.save_calls = 0
        self.revision_counter = 1
        self.checkpoint = SimpleNamespace(
            ledger=start_bybit_demo_session_risk_ledger(
                opening_equity_usdt=Decimal("1000")
            ),
            revision="a" * 64,
        )

    def load_current(self):
        if self.missing:
            raise FileNotFoundError
        return self.checkpoint

    def save(self, ledger, *, expected_revision):
        self.save_calls += 1
        if self.fail_save:
            raise RuntimeError("session risk save unavailable")
        if expected_revision != self.checkpoint.revision:
            raise RuntimeError("session risk revision changed concurrently")
        self.revision_counter += 1
        self.checkpoint = SimpleNamespace(
            ledger=ledger,
            revision=f"{self.revision_counter:064x}",
        )
        return self.checkpoint


class _CashBaselineStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, *, wallet_balance: str = "950", missing: bool = False) -> None:
        self.missing = missing
        self.baseline = BybitDemoCashBaseline(
            currency="USDT",
            wallet_balance_usdt=Decimal(wallet_balance),
            cumulative_all_in_pnl_usdt=Decimal("0"),
            session_revision="a" * 64,
            created_time_ms=1_700_000_000_000,
        )

    def load(self):
        if self.missing:
            raise FileNotFoundError
        return self.baseline


class _Accounting:
    live_mainnet_order_routing_allowed = False

    def __init__(self, equity: str = "950", wallet_usdt: str | None = None) -> None:
        self.equity = Decimal(equity)
        self.wallet_usdt = Decimal(equity if wallet_usdt is None else wallet_usdt)

    def get_wallet_balance(self):
        return SimpleNamespace(
            total_equity_usd=self.equity,
            usdt_wallet_balance=self.wallet_usdt,
        )


class _Instruments:
    def __init__(self) -> None:
        self.requests: list[tuple[str, ...]] = []

    def fetch_symbols(self, symbols):
        request = tuple(symbols)
        self.requests.append(request)
        return {symbol: object() for symbol in request}


class _Bars:
    live_mainnet_order_routing_allowed = False

    def __init__(self, count: int) -> None:
        self.count = count
        self.calls: list[tuple[str, int, int, str]] = []

    def fetch_completed_range(self, *, symbol, start_ms, now_ms, interval):
        self.calls.append((symbol, start_ms, now_ms, interval))
        return tuple(object() for _ in range(self.count))


class _Safe:
    live_mainnet_order_routing_allowed = False


class _Operator:
    def inspect(self):
        return SimpleNamespace(
            kill_switch_engaged=False,
            live_mainnet_order_routing_allowed=False,
        )


class _PrivateStream:
    def snapshot(self):
        return SimpleNamespace(
            healthy=True,
            last_message_monotonic=100.0,
            live_mainnet_order_routing_allowed=False,
        )


class _EntryOms:
    def count_unresolved_entry_submissions(self):
        return 0


def _config(*, writes: bool = False) -> BybitProductConfig:
    return BybitProductConfig.from_env(
        {
            "ASTRA_ENV": "demo",
            "ASTRA_BROKER": "bybit",
            "ASTRA_SYMBOLS": "BTCUSDT,ETHUSDT",
            "ASTRA_BAR_INTERVAL": "5",
            "ASTRA_BAR_LOOKBACK": "50",
            "BYBIT_API_KEY": "key",
            "BYBIT_API_SECRET": "secret",
            "DATABASE_URL": "postgresql://astra:secret@db/astra",
            "TRADING_WRITES_ENABLED": "true" if writes else "false",
            "MAINNET_ENABLED": "false",
        },
        require_universe=True,
    )


def _executor(
    *,
    writes: bool = False,
    active_symbol: str | None = None,
    missing_session: bool = False,
    fail_session_save: bool = False,
    wallet_equity: str = "950",
    bar_count: int = 50,
    market_data_observation_hook=None,
    account_risk_observation_hook=None,
) -> product.BybitProductCycleExecutor:
    checkpoint = (
        None
        if active_symbol is None
        else SimpleNamespace(state=SimpleNamespace(symbol=active_symbol))
    )
    return product.BybitProductCycleExecutor(
        config=_config(writes=writes),
        trade_client=_Safe(),
        accounting_client=_Accounting(wallet_equity),
        quote_client=_Safe(),
        completed_bar_client=_Bars(bar_count),
        instrument_client=_Instruments(),
        runtime_lease=_Safe(),
        excursion_store=_ExcursionStore(checkpoint),
        entry_provenance_store=_Safe(),
        terminal_evidence_store=_Safe(),
        session_risk_store=_SessionStore(
            missing=missing_session,
            fail_save=fail_session_save,
        ),
        strategy_config=CryptoPerpStrategyConfig(),
        market_data_observation_hook=market_data_observation_hook,
        account_risk_observation_hook=account_risk_observation_hook,
        clock_ms=lambda: 1_800_000_000_000,
    )


def test_flat_runtime_refuses_new_entry_without_authoritative_session_ledger() -> None:
    executor = _executor(missing_session=True)

    with pytest.raises(RuntimeError, match="SESSION_RISK_LEDGER_REQUIRED_BEFORE_NEW_ENTRY"):
        executor.run_once()


def test_active_trade_management_continues_if_session_ledger_is_temporarily_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    executor = _executor(active_symbol="SOLUSDT", missing_session=True)

    def _runtime(bars_by_symbol, **kwargs):
        captured["bars"] = bars_by_symbol
        captured.update(kwargs)
        return SimpleNamespace(live_mainnet_order_routing_allowed=False)

    monkeypatch.setattr(product, "run_attributed_bybit_demo_trading_runtime", _runtime)
    result = executor.run_once()

    assert result.live_mainnet_order_routing_allowed is False
    assert captured["bars"] == {}
    assert captured["session_ledger"] is None
    assert captured["session_state"].opening_equity_usdt == Decimal("950")
    assert executor.instrument_client.requests == [("BTCUSDT", "ETHUSDT", "SOLUSDT")]


def test_flat_cycle_uses_all_completed_universe_bars_and_frozen_strategy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    executor = _executor(writes=True)

    def _runtime(bars_by_symbol, **kwargs):
        captured["bars"] = bars_by_symbol
        captured.update(kwargs)
        return SimpleNamespace(live_mainnet_order_routing_allowed=False)

    monkeypatch.setattr(product, "run_attributed_bybit_demo_trading_runtime", _runtime)
    executor.run_once()

    assert set(captured["bars"]) == {"BTCUSDT", "ETHUSDT"}
    assert all(len(bars) == 50 for bars in captured["bars"].values())
    assert captured["strategy_config"] is executor.strategy_config
    assert captured["cycle_policy"].writes_enabled is True
    assert captured["managed_policy"].trade_management.stop_ratchet_writes_enabled is True
    assert captured["managed_policy"].max_hold_close.writes_enabled is True
    assert captured["session_ledger"].opening_equity_usdt == Decimal("1000")
    assert captured["session_state"].current_equity_usdt == Decimal("950")
    assert captured["session_state"].peak_equity_usdt == Decimal("1000")
    assert executor.demo_order_writes_enabled is True
    assert executor.live_mainnet_order_routing_allowed is False


def test_authoritative_session_state_is_projected_to_account_risk_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = []
    executor = _executor(
        wallet_equity="950",
        account_risk_observation_hook=observations.append,
    )
    monkeypatch.setattr(
        product,
        "run_attributed_bybit_demo_trading_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(live_mainnet_order_routing_allowed=False),
    )

    executor.run_once()

    assert len(observations) == 1
    assert observations[0].current_equity_usdt == Decimal("950")
    assert observations[0].peak_equity_usdt == Decimal("1000")


def test_missing_session_ledger_does_not_publish_fake_account_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations = []
    executor = _executor(
        active_symbol="SOLUSDT",
        missing_session=True,
        account_risk_observation_hook=observations.append,
    )
    monkeypatch.setattr(
        product,
        "run_attributed_bybit_demo_trading_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(live_mainnet_order_routing_allowed=False),
    )

    executor.run_once()

    assert observations == []


def test_operational_health_reads_daily_pnl_and_flat_cash_from_durable_state() -> None:
    session_store = _SessionStore()
    accounting = _Accounting(equity="1000", wallet_usdt="950")
    composition = product.BybitProductComposition(
        config=_config(),
        startup_reconciler=_Safe(),
        cycle_executor=_executor(),
        operator_control=_Operator(),
        private_stream_monitor=_PrivateStream(),
        accounting_client=accounting,
        rest_health_recorder=product.BybitRestHealthRecorder(),
        market_data_health_recorder=product.BybitMarketDataHealthRecorder(),
        account_risk_health_recorder=product.BybitAccountRiskHealthRecorder(),
        reconciliation_health_recorder=product.BybitReconciliationHealthRecorder(),
        session_risk_store=session_store,
        cash_baseline_store=_CashBaselineStore(wallet_balance="950"),
        entry_oms=_EntryOms(),
        clock_ms=lambda: 1_800_000_000_000,
        monotonic_fn=lambda: 105.0,
    )

    report = composition.operational_health()

    assert "MEASUREMENT_UNAVAILABLE:daily_pnl" not in report.blockers
    assert "MEASUREMENT_UNAVAILABLE:cash_mismatch" not in report.blockers

    accounting.wallet_usdt = Decimal("949")
    mismatch = composition.operational_health()

    assert "MEASUREMENT_UNAVAILABLE:cash_mismatch" not in mismatch.blockers

    session_store.missing = True
    unavailable = composition.operational_health()

    assert "MEASUREMENT_UNAVAILABLE:daily_pnl" in unavailable.blockers
    assert "MEASUREMENT_UNAVAILABLE:cash_mismatch" in unavailable.blockers


def test_operational_health_defers_cash_measurement_while_trade_is_active() -> None:
    composition = product.BybitProductComposition(
        config=_config(),
        startup_reconciler=_Safe(),
        cycle_executor=_executor(active_symbol="BTCUSDT"),
        operator_control=_Operator(),
        private_stream_monitor=_PrivateStream(),
        accounting_client=_Accounting(equity="1000", wallet_usdt="950"),
        rest_health_recorder=product.BybitRestHealthRecorder(),
        market_data_health_recorder=product.BybitMarketDataHealthRecorder(),
        account_risk_health_recorder=product.BybitAccountRiskHealthRecorder(),
        reconciliation_health_recorder=product.BybitReconciliationHealthRecorder(),
        session_risk_store=_SessionStore(),
        cash_baseline_store=_CashBaselineStore(wallet_balance="950"),
        entry_oms=_EntryOms(),
        clock_ms=lambda: 1_800_000_000_000,
        monotonic_fn=lambda: 105.0,
    )

    report = composition.operational_health()

    assert "MEASUREMENT_UNAVAILABLE:cash_mismatch" in report.blockers


def test_flat_cycle_persists_wallet_high_water_before_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}
    executor = _executor(wallet_equity="1100")
    store = executor.session_risk_store

    def _runtime(_bars_by_symbol, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(live_mainnet_order_routing_allowed=False)

    monkeypatch.setattr(product, "run_attributed_bybit_demo_trading_runtime", _runtime)
    executor.run_once()

    assert store.save_calls == 1
    assert store.checkpoint.ledger.peak_equity_usdt == Decimal("1100")
    assert captured["session_state"].peak_equity_usdt == Decimal("1100")
    assert captured["session_ledger"].peak_equity_usdt == Decimal("1100")


def test_flat_cycle_blocks_entry_when_high_water_cannot_be_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_called = False
    executor = _executor(wallet_equity="1100", fail_session_save=True)

    def _runtime(*_args, **_kwargs):
        nonlocal runtime_called
        runtime_called = True
        return SimpleNamespace(live_mainnet_order_routing_allowed=False)

    monkeypatch.setattr(product, "run_attributed_bybit_demo_trading_runtime", _runtime)

    with pytest.raises(
        RuntimeError,
        match="SESSION_RISK_HIGH_WATER_PERSIST_FAILED_BEFORE_NEW_ENTRY",
    ):
        executor.run_once()

    assert runtime_called is False
    assert executor.completed_bar_client.calls == []


def test_active_trade_keeps_management_when_high_water_save_fails_then_retries_flat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures: list[dict[str, object]] = []
    executor = _executor(
        active_symbol="SOLUSDT",
        wallet_equity="1100",
        fail_session_save=True,
    )
    store = executor.session_risk_store

    def _runtime(bars_by_symbol, **kwargs):
        captures.append({"bars": bars_by_symbol, **kwargs})
        return SimpleNamespace(live_mainnet_order_routing_allowed=False)

    monkeypatch.setattr(product, "run_attributed_bybit_demo_trading_runtime", _runtime)

    first = executor.run_once()

    assert first.live_mainnet_order_routing_allowed is False
    assert captures[0]["bars"] == {}
    assert captures[0]["session_state"].peak_equity_usdt == Decimal("1100")
    assert store.checkpoint.ledger.peak_equity_usdt == Decimal("1000")
    assert store.save_calls >= 1

    executor.excursion_store.checkpoint = None
    executor.accounting_client.equity = Decimal("1000")
    store.fail_save = False

    executor.run_once()

    assert store.checkpoint.ledger.peak_equity_usdt == Decimal("1100")
    assert captures[1]["session_state"].peak_equity_usdt == Decimal("1100")


def test_complete_flat_universe_read_records_one_market_data_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observations: list[str] = []
    executor = _executor(
        market_data_observation_hook=lambda: observations.append("complete-universe")
    )
    monkeypatch.setattr(
        product,
        "run_attributed_bybit_demo_trading_runtime",
        lambda *_args, **_kwargs: SimpleNamespace(live_mainnet_order_routing_allowed=False),
    )

    executor.run_once()

    assert observations == ["complete-universe"]


def test_incomplete_market_history_blocks_before_market_data_observation() -> None:
    observations: list[str] = []
    executor = _executor(
        bar_count=49,
        market_data_observation_hook=lambda: observations.append("should-not-record"),
    )

    with pytest.raises(RuntimeError, match="completed bar count mismatch for BTCUSDT"):
        executor.run_once()

    assert observations == []
