from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from time import time

from app.execution.bybit_demo_account_reader import BybitDemoAccountingClient
from app.execution.bybit_demo_attributed_runtime import (
    BybitDemoAttributedRuntimeResult,
    run_attributed_bybit_demo_trading_runtime,
)
from app.execution.bybit_demo_broker_truth import BybitDemoBrokerTruthClient
from app.execution.bybit_demo_cycle import BybitDemoCyclePolicy
from app.execution.bybit_demo_managed_trade_poll import BybitDemoManagedTradePollPolicy
from app.execution.bybit_demo_max_hold_close import BybitDemoMaxHoldClosePolicy
from app.execution.bybit_demo_session_risk_ledger import (
    start_bybit_demo_session_risk_ledger,
)
from app.execution.bybit_demo_stop_ratchet_client import BybitDemoStopRatchetClient
from app.execution.bybit_demo_trade_management_runtime import (
    BybitDemoTradeManagementRuntimePolicy,
)
from app.execution.bybit_postgres_evidence_state import (
    PostgresBybitDemoEntryProvenanceStore,
    PostgresBybitDemoSessionRiskLedgerStore,
    PostgresBybitDemoTerminalEvidenceStore,
)
from app.execution.bybit_postgres_runtime_state import (
    PostgresBybitDemoExcursionStore,
    PostgresBybitDemoRuntimeLease,
)
from app.execution.bybit_private_stream import BybitPrivateStreamMonitor
from app.execution.bybit_product_terminal_handoff import persist_product_terminal_state
from app.execution.bybit_startup_reconciliation import (
    BybitStartupReconciliationResult,
    reconcile_bybit_startup,
)
from app.marketdata.bybit_demo_completed_bars import BybitDemoCompletedBarClient
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuoteClient
from app.marketdata.bybit_instruments import BybitInstrumentClient
from app.marketdata.bybit_v5 import interval_milliseconds, last_completed_kline_end_ms
from app.runtime.bybit_product_config import BybitProductConfig
from app.runtime.bybit_product_service import (
    BybitProductServiceResult,
    run_bybit_product_service,
)
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState

ClockMs = Callable[[], int]


@dataclass(frozen=True)
class BybitProductStartupReconciler:
    broker: BybitDemoBrokerTruthClient
    checkpoint_store: PostgresBybitDemoExcursionStore
    live_mainnet_order_routing_allowed: bool = False

    def run(self) -> BybitStartupReconciliationResult:
        return reconcile_bybit_startup(
            broker=self.broker,
            checkpoint_store=self.checkpoint_store,
        )


@dataclass
class BybitProductCycleExecutor:
    config: BybitProductConfig
    trade_client: BybitDemoStopRatchetClient
    accounting_client: BybitDemoAccountingClient
    quote_client: BybitDemoMarketQuoteClient
    completed_bar_client: BybitDemoCompletedBarClient
    instrument_client: BybitInstrumentClient
    runtime_lease: PostgresBybitDemoRuntimeLease
    excursion_store: PostgresBybitDemoExcursionStore
    entry_provenance_store: PostgresBybitDemoEntryProvenanceStore
    terminal_evidence_store: PostgresBybitDemoTerminalEvidenceStore
    session_risk_store: PostgresBybitDemoSessionRiskLedgerStore
    strategy_config: CryptoPerpStrategyConfig
    clock_ms: ClockMs = lambda: int(time() * 1000)
    live_mainnet_order_routing_allowed: bool = False

    @property
    def demo_order_writes_enabled(self) -> bool:
        return self.config.demo_order_writes_allowed

    def has_active_trade(self) -> bool:
        return self._active_checkpoint_hint() is not None

    def run_once(self) -> BybitDemoAttributedRuntimeResult:
        now_ms = self.clock_ms()
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise ValueError("Bybit product clock must return a non-negative integer")
        now = datetime.fromtimestamp(now_ms / 1000, tz=UTC)

        active_checkpoint = self._active_checkpoint_hint()
        symbols = list(self.config.symbols)
        if active_checkpoint is not None and active_checkpoint.state.symbol not in symbols:
            symbols.append(active_checkpoint.state.symbol)
        instruments = self.instrument_client.fetch_symbols(tuple(symbols))

        session_checkpoint = self._load_session_risk(active_checkpoint is not None)
        wallet = self.accounting_client.get_wallet_balance()
        if session_checkpoint is None:
            session_state = CryptoSessionRiskState(
                opening_equity_usdt=wallet.total_equity_usd,
                current_equity_usdt=wallet.total_equity_usd,
                peak_equity_usdt=wallet.total_equity_usd,
            )
            session_ledger = None
        else:
            session_state = session_checkpoint.ledger.to_session_risk_state(
                current_equity_usdt=wallet.total_equity_usd,
            )
            session_ledger = session_checkpoint.ledger

        bars_by_symbol = (
            {}
            if active_checkpoint is not None
            else self._completed_universe_bars(now_ms=now_ms)
        )
        writes_enabled = self.config.demo_order_writes_allowed
        managed_policy = BybitDemoManagedTradePollPolicy(
            trade_management=BybitDemoTradeManagementRuntimePolicy(
                stop_ratchet_writes_enabled=writes_enabled,
                interval=self.config.bar_interval,
            ),
            max_hold_close=BybitDemoMaxHoldClosePolicy(
                writes_enabled=writes_enabled,
            ),
        )
        terminal_handoff = partial(
            persist_product_terminal_state,
            session_risk_store=self.session_risk_store,
        )
        return run_attributed_bybit_demo_trading_runtime(
            bars_by_symbol,
            instruments=instruments,
            strategy_config=self.strategy_config,
            session_state=session_state,
            now=now,
            now_ms=now_ms,
            client=self.trade_client,
            accounting_client=self.accounting_client,
            excursion_store=self.excursion_store,
            completed_bar_client=self.completed_bar_client,
            quote_client=self.quote_client,
            runtime_lease=self.runtime_lease,
            terminal_evidence_store=self.terminal_evidence_store,
            entry_provenance_store=self.entry_provenance_store,
            managed_policy=managed_policy,
            terminal_handoff=terminal_handoff,
            cycle_policy=BybitDemoCyclePolicy(writes_enabled=writes_enabled),
            session_ledger=session_ledger,
        )

    def _active_checkpoint_hint(self):
        try:
            return self.excursion_store.load()
        except FileNotFoundError:
            return None

    def _load_session_risk(self, active_trade: bool):
        try:
            return self.session_risk_store.load_current()
        except FileNotFoundError:
            if active_trade:
                return None
            raise RuntimeError(
                "SESSION_RISK_LEDGER_REQUIRED_BEFORE_NEW_ENTRY; "
                "run explicit session bootstrap while broker state is flat"
            ) from None

    def _completed_universe_bars(self, *, now_ms: int):
        interval_ms = interval_milliseconds(self.config.bar_interval)
        end_ms = last_completed_kline_end_ms(
            now_ms=now_ms,
            interval=self.config.bar_interval,
        )
        last_start_ms = (end_ms // interval_ms) * interval_ms
        start_ms = last_start_ms - ((self.config.bar_lookback - 1) * interval_ms)
        if start_ms < 0:
            raise ValueError("Bybit product bar lookback starts before Unix epoch")
        result = {}
        for symbol in self.config.symbols:
            bars = self.completed_bar_client.fetch_completed_range(
                symbol=symbol,
                start_ms=start_ms,
                now_ms=now_ms,
                interval=self.config.bar_interval,
            )
            if len(bars) != self.config.bar_lookback:
                raise RuntimeError(
                    f"completed bar count mismatch for {symbol}:"
                    f"{len(bars)}!={self.config.bar_lookback}"
                )
            result[symbol] = bars
        return result


@dataclass(frozen=True)
class BybitProductComposition:
    config: BybitProductConfig
    startup_reconciler: BybitProductStartupReconciler
    cycle_executor: BybitProductCycleExecutor
    private_stream_monitor: BybitPrivateStreamMonitor
    live_mainnet_order_routing_allowed: bool = False

    def run(
        self,
        *,
        stop_requested: Callable[[], bool],
        max_cycles: int | None = None,
    ) -> BybitProductServiceResult:
        return run_bybit_product_service(
            config=self.config,
            startup_reconciler=self.startup_reconciler,
            cycle_executor=self.cycle_executor,
            private_stream_monitor=self.private_stream_monitor,
            stop_requested=stop_requested,
            max_cycles=max_cycles,
        )


def build_bybit_product_composition(
    config: BybitProductConfig,
    *,
    strategy_config: CryptoPerpStrategyConfig | None = None,
    clock_ms: ClockMs | None = None,
) -> BybitProductComposition:
    """Build the single canonical Bybit product composition using PostgreSQL authority."""

    config.validate(require_universe=True)
    active_strategy = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    active_strategy.validate()

    runtime_lease = PostgresBybitDemoRuntimeLease(config.database_url)
    excursion_store = PostgresBybitDemoExcursionStore(
        config.database_url,
        runtime_lease=runtime_lease,
    )
    trade_client = BybitDemoStopRatchetClient(
        api_key=config.api_key,
        api_secret=config.api_secret,
    )
    accounting_client = BybitDemoAccountingClient(
        api_key=config.api_key,
        api_secret=config.api_secret,
    )
    startup_broker = BybitDemoBrokerTruthClient(
        api_key=config.api_key,
        api_secret=config.api_secret,
    )
    private_stream = BybitPrivateStreamMonitor(
        api_key=config.api_key,
        api_secret=config.api_secret,
        url=config.private_ws_url,
    )
    cycle = BybitProductCycleExecutor(
        config=config,
        trade_client=trade_client,
        accounting_client=accounting_client,
        quote_client=BybitDemoMarketQuoteClient(),
        completed_bar_client=BybitDemoCompletedBarClient(),
        instrument_client=BybitInstrumentClient(),
        runtime_lease=runtime_lease,
        excursion_store=excursion_store,
        entry_provenance_store=PostgresBybitDemoEntryProvenanceStore(config.database_url),
        terminal_evidence_store=PostgresBybitDemoTerminalEvidenceStore(config.database_url),
        session_risk_store=PostgresBybitDemoSessionRiskLedgerStore(config.database_url),
        strategy_config=active_strategy,
        clock_ms=(lambda: int(time() * 1000)) if clock_ms is None else clock_ms,
    )
    return BybitProductComposition(
        config=config,
        startup_reconciler=BybitProductStartupReconciler(
            broker=startup_broker,
            checkpoint_store=excursion_store,
        ),
        cycle_executor=cycle,
        private_stream_monitor=private_stream,
    )


def bootstrap_bybit_product_session(config: BybitProductConfig) -> Decimal:
    """Explicitly initialize session risk only while reconciled broker/local state is flat."""

    config.validate()
    lease_store = PostgresBybitDemoRuntimeLease(config.database_url)
    excursion_store = PostgresBybitDemoExcursionStore(
        config.database_url,
        runtime_lease=lease_store,
    )
    broker = BybitDemoBrokerTruthClient(
        api_key=config.api_key,
        api_secret=config.api_secret,
    )
    accounting = BybitDemoAccountingClient(
        api_key=config.api_key,
        api_secret=config.api_secret,
    )
    session_store = PostgresBybitDemoSessionRiskLedgerStore(config.database_url)

    lease = lease_store.acquire()
    try:
        reconciled = reconcile_bybit_startup(
            broker=broker,
            checkpoint_store=excursion_store,
        )
        if not reconciled.next_entry_allowed:
            raise RuntimeError(
                "session bootstrap requires broker and local trading state to be fully flat"
            )
        wallet = accounting.get_wallet_balance()
        session_store.initialize(
            start_bybit_demo_session_risk_ledger(
                opening_equity_usdt=wallet.total_equity_usd,
            )
        )
        return wallet.total_equity_usd
    finally:
        lease_store.release(owner_token=lease.owner_token)
