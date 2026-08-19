from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from time import monotonic, time

from app.application.bybit_operator_control import PostgresBybitOperatorControl
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
    observe_bybit_demo_session_equity,
    start_bybit_demo_session_risk_ledger,
)
from app.execution.bybit_demo_stop_ratchet_client import BybitDemoStopRatchetClient
from app.execution.bybit_demo_trade_management_runtime import (
    BybitDemoTradeManagementRuntimePolicy,
)
from app.execution.bybit_observed_rest import (
    ObservedBybitDemoAccountingClient,
    ObservedBybitDemoBrokerTruthClient,
)
from app.execution.bybit_oms_entry_client import OmsAwareBybitDemoStopRatchetClient
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
    BybitStartupReconciliationStatus,
    reconcile_bybit_startup,
)
from app.marketdata.bybit_demo_completed_bars import BybitDemoCompletedBarClient
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuoteClient
from app.marketdata.bybit_entry_reference import (
    BybitEntryReferenceQuoteClient,
    BybitEntryReferenceStore,
)
from app.marketdata.bybit_instruments import BybitInstrumentClient
from app.marketdata.bybit_v5 import interval_milliseconds, last_completed_kline_end_ms
from app.observability.bybit_runtime_health import (
    BybitMarketDataHealthRecorder,
    BybitOperationalHealthReport,
    BybitReconciliationHealthRecorder,
    BybitRestHealthRecorder,
    build_bybit_operational_health,
    collect_bybit_operational_measurements,
)
from app.oms.bybit_entry import PostgresBybitEntryOms
from app.oms.store import OrderRecord, OrderState
from app.runtime.bybit_product_config import BybitProductConfig
from app.runtime.bybit_product_service import BybitProductServiceResult, run_bybit_product_service
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState

ClockMs = Callable[[], int]
MonotonicFn = Callable[[], float]
_AUTO_RECOVERABLE_ENTRY_STATES = frozenset(
    {OrderState.SUBMIT_STARTED, OrderState.UNCERTAIN, OrderState.RECONCILING}
)


@dataclass(frozen=True)
class BybitProductStartupReconciler:
    broker: BybitDemoBrokerTruthClient
    checkpoint_store: PostgresBybitDemoExcursionStore
    entry_oms: PostgresBybitEntryOms
    reconciliation_health: BybitReconciliationHealthRecorder | None = None
    clock_ms: ClockMs = lambda: int(time() * 1000)
    monotonic_fn: MonotonicFn = monotonic
    live_mainnet_order_routing_allowed: bool = False

    def run(self) -> BybitStartupReconciliationResult:
        try:
            unresolved_entries = self.entry_oms.unresolved_entry_submissions()
        except Exception as exc:
            return self._record_reconciliation(
                _blocked_startup(f"STARTUP_BYBIT_OMS_READ_FAILED:{type(exc).__name__}")
            )

        blockers: list[str] = []
        order_truth_complete = True
        for record in unresolved_entries:
            truth_complete, entry_blockers = self._reconcile_unresolved_entry(record)
            order_truth_complete = order_truth_complete and truth_complete
            blockers.extend(entry_blockers)

        base = reconcile_bybit_startup(
            broker=self.broker,
            checkpoint_store=self.checkpoint_store,
        )
        if not blockers:
            return self._record_reconciliation(base)
        return self._record_reconciliation(
            replace(
                base,
                status=BybitStartupReconciliationStatus.BLOCKED,
                reasons=_unique_reasons(base.reasons + tuple(blockers)),
                next_entry_allowed=False,
                broker_truth_complete=base.broker_truth_complete and order_truth_complete,
            )
        )

    def _record_reconciliation(
        self,
        result: BybitStartupReconciliationResult,
    ) -> BybitStartupReconciliationResult:
        if self.reconciliation_health is None:
            return result
        observed = Decimal(str(self.monotonic_fn()))
        self.reconciliation_health.record(result, observed_monotonic=observed)
        return result

    def _reconcile_unresolved_entry(self, record: OrderRecord) -> tuple[bool, tuple[str, ...]]:
        if record.state not in _AUTO_RECOVERABLE_ENTRY_STATES:
            return False, (
                f"BYBIT_OMS_ENTRY_REQUIRES_MANUAL_RECOVERY:{record.intent_id}:{record.state.value}",
            )
        try:
            truth = self.broker.get_order_by_link_id(
                symbol=record.symbol,
                order_link_id=record.client_order_id,
                expected_side="Buy" if record.side.value == "BUY" else "Sell",
                expected_quantity=record.quantity,
            )
        except Exception as exc:
            return False, (
                f"BYBIT_OMS_ENTRY_BROKER_READ_FAILED:{record.intent_id}:{type(exc).__name__}",
            )
        if truth is None:
            return False, (
                f"BYBIT_OMS_ENTRY_NOT_FOUND_BY_ORDER_LINK_ID:{record.intent_id}",
            )

        occurred_at = _utc_from_ms(self.clock_ms())
        try:
            if truth.safely_rejected_without_execution:
                self.entry_oms.resolve_rejected_without_execution(
                    record.intent_id,
                    broker_order_id=truth.order_id,
                    cumulative_executed_quantity=truth.cumulative_executed_quantity,
                    occurred_at=occurred_at,
                )
                return True, ()
            self.entry_oms.mark_lifecycle_reconciliation_required(
                record.intent_id,
                broker_order_id=truth.order_id,
                broker_status=truth.status,
                cumulative_executed_quantity=truth.cumulative_executed_quantity,
                occurred_at=occurred_at,
            )
        except Exception as exc:
            return True, (
                f"BYBIT_OMS_ENTRY_RECOVERY_PERSIST_FAILED:{record.intent_id}:{type(exc).__name__}",
            )
        return True, (
            f"BYBIT_ENTRY_LIFECYCLE_RECONCILIATION_REQUIRED:{record.intent_id}:"
            f"{truth.status}:cumExecQty={truth.cumulative_executed_quantity}",
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
    market_data_observation_hook: Callable[[], None] | None = None
    clock_ms: ClockMs = lambda: int(time() * 1000)
    live_mainnet_order_routing_allowed: bool = False
    _session_peak_equity_hint_usdt: Decimal | None = field(
        default=None,
        init=False,
        repr=False,
    )

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
        active_trade = active_checkpoint is not None
        symbols = list(self.config.symbols)
        if active_checkpoint is not None and active_checkpoint.state.symbol not in symbols:
            symbols.append(active_checkpoint.state.symbol)
        instruments = self.instrument_client.fetch_symbols(tuple(symbols))

        session_checkpoint = self._load_session_risk(active_trade)
        wallet = self.accounting_client.get_wallet_balance()
        if session_checkpoint is None:
            session_state = CryptoSessionRiskState(
                opening_equity_usdt=wallet.total_equity_usd,
                current_equity_usdt=wallet.total_equity_usd,
                peak_equity_usdt=wallet.total_equity_usd,
            )
            session_ledger = None
        else:
            session_ledger = self._session_ledger_for_wallet(
                session_checkpoint,
                current_equity_usdt=wallet.total_equity_usd,
                active_trade=active_trade,
            )
            session_state = session_ledger.to_session_risk_state(
                current_equity_usdt=wallet.total_equity_usd,
            )

        bars_by_symbol = (
            {} if active_trade else self._completed_universe_bars(now_ms=now_ms)
        )
        writes_enabled = self.config.demo_order_writes_allowed
        managed_policy = BybitDemoManagedTradePollPolicy(
            trade_management=BybitDemoTradeManagementRuntimePolicy(
                stop_ratchet_writes_enabled=writes_enabled,
                interval=self.config.bar_interval,
            ),
            max_hold_close=BybitDemoMaxHoldClosePolicy(writes_enabled=writes_enabled),
        )
        terminal_handoff = partial(
            persist_product_terminal_state,
            session_risk_store=self.session_risk_store,
        )
        try:
            result = run_attributed_bybit_demo_trading_runtime(
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
        finally:
            if active_trade and session_checkpoint is not None:
                self._persist_active_session_peak_best_effort()
        return result

    def _session_ledger_for_wallet(
        self,
        checkpoint,
        *,
        current_equity_usdt: Decimal,
        active_trade: bool,
    ):
        ledger = checkpoint.ledger
        if self._session_peak_equity_hint_usdt is not None:
            ledger = observe_bybit_demo_session_equity(
                ledger,
                current_equity_usdt=self._session_peak_equity_hint_usdt,
            )
        ledger = observe_bybit_demo_session_equity(
            ledger,
            current_equity_usdt=current_equity_usdt,
        )
        self._remember_session_peak(ledger.effective_peak_equity_usdt)
        if ledger == checkpoint.ledger:
            return ledger
        if active_trade:
            try:
                saved = self.session_risk_store.save(
                    ledger,
                    expected_revision=checkpoint.revision,
                )
            except Exception:  # noqa: BLE001 - active protection must continue on DB write failure.
                return ledger
        else:
            try:
                saved = self.session_risk_store.save(
                    ledger,
                    expected_revision=checkpoint.revision,
                )
            except Exception as exc:
                raise RuntimeError(
                    "SESSION_RISK_HIGH_WATER_PERSIST_FAILED_BEFORE_NEW_ENTRY"
                ) from exc
        self._remember_session_peak(saved.ledger.effective_peak_equity_usdt)
        return saved.ledger

    def _persist_active_session_peak_best_effort(self) -> None:
        peak = self._session_peak_equity_hint_usdt
        if peak is None:
            return
        try:
            current = self.session_risk_store.load_current()
        except Exception:  # noqa: BLE001 - protection already ran; retry next cycle.
            return
        try:
            updated = observe_bybit_demo_session_equity(
                current.ledger,
                current_equity_usdt=peak,
            )
        except Exception:  # noqa: BLE001 - malformed state must not replace protection result.
            return
        if updated == current.ledger:
            return
        try:
            saved = self.session_risk_store.save(
                updated,
                expected_revision=current.revision,
            )
        except Exception:  # noqa: BLE001 - process hint preserves the peak until retry.
            return
        self._remember_session_peak(saved.ledger.effective_peak_equity_usdt)

    def _remember_session_peak(self, peak_equity_usdt: Decimal) -> None:
        if not peak_equity_usdt.is_finite() or peak_equity_usdt <= 0:
            raise ValueError("session peak equity hint must be positive and finite")
        if (
            self._session_peak_equity_hint_usdt is None
            or peak_equity_usdt > self._session_peak_equity_hint_usdt
        ):
            self._session_peak_equity_hint_usdt = peak_equity_usdt

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
        self._observe_market_data()
        return result

    def _observe_market_data(self) -> None:
        if self.market_data_observation_hook is None:
            return
        try:
            self.market_data_observation_hook()
        except Exception:  # noqa: BLE001 - telemetry cannot replace valid market data.
            return


@dataclass(frozen=True)
class BybitProductComposition:
    config: BybitProductConfig
    startup_reconciler: BybitProductStartupReconciler
    cycle_executor: BybitProductCycleExecutor
    operator_control: PostgresBybitOperatorControl
    private_stream_monitor: BybitPrivateStreamMonitor
    rest_health_recorder: BybitRestHealthRecorder
    market_data_health_recorder: BybitMarketDataHealthRecorder
    reconciliation_health_recorder: BybitReconciliationHealthRecorder
    entry_oms: PostgresBybitEntryOms
    monotonic_fn: MonotonicFn = monotonic
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
            operator_control=self.operator_control,
            private_stream_monitor=self.private_stream_monitor,
            stop_requested=stop_requested,
            max_cycles=max_cycles,
        )

    def operational_health(self) -> BybitOperationalHealthReport:
        now = Decimal(str(self.monotonic_fn()))
        try:
            unresolved_entries: int | None = self.entry_oms.count_unresolved_entry_submissions()
        except Exception:
            unresolved_entries = None
        try:
            operator = self.operator_control.inspect()
        except Exception:
            operator = None
        try:
            stream = self.private_stream_monitor.snapshot()
        except Exception:
            stream = None
        measurements = collect_bybit_operational_measurements(
            now_monotonic=now,
            market_data=self.market_data_health_recorder.snapshot(now_monotonic=now),
            rest=self.rest_health_recorder.snapshot(),
            reconciliation=self.reconciliation_health_recorder.snapshot(now_monotonic=now),
            private_stream=stream,
            unresolved_entry_submissions=unresolved_entries,
            operator=operator,
        )
        return build_bybit_operational_health(measurements)


def build_bybit_product_composition(
    config: BybitProductConfig,
    *,
    strategy_config: CryptoPerpStrategyConfig | None = None,
    clock_ms: ClockMs | None = None,
    monotonic_fn: MonotonicFn = monotonic,
) -> BybitProductComposition:
    config.validate(require_universe=True)
    active_strategy = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    active_strategy.validate()
    active_clock = (lambda: int(time() * 1000)) if clock_ms is None else clock_ms

    runtime_lease = PostgresBybitDemoRuntimeLease(config.database_url)
    excursion_store = PostgresBybitDemoExcursionStore(
        config.database_url,
        runtime_lease=runtime_lease,
    )
    entry_oms = PostgresBybitEntryOms(config.database_url)
    operator_control = PostgresBybitOperatorControl(config.database_url)
    entry_reference_store = BybitEntryReferenceStore()
    rest_health = BybitRestHealthRecorder()
    market_data_health = BybitMarketDataHealthRecorder()
    reconciliation_health = BybitReconciliationHealthRecorder()

    def observe_market_data() -> None:
        market_data_health.record_success(
            observed_monotonic=Decimal(str(monotonic_fn()))
        )

    trade_client = OmsAwareBybitDemoStopRatchetClient(
        api_key=config.api_key,
        api_secret=config.api_secret,
        rest_health_sink=rest_health,
        entry_oms=entry_oms,
        entry_reference_store=entry_reference_store,
    )
    accounting_client = ObservedBybitDemoAccountingClient(
        api_key=config.api_key,
        api_secret=config.api_secret,
        rest_health_sink=rest_health,
    )
    startup_broker = ObservedBybitDemoBrokerTruthClient(
        api_key=config.api_key,
        api_secret=config.api_secret,
        rest_health_sink=rest_health,
    )
    private_stream = BybitPrivateStreamMonitor(
        api_key=config.api_key,
        api_secret=config.api_secret,
        url=config.private_ws_url,
        monotonic_fn=monotonic_fn,
    )
    cycle = BybitProductCycleExecutor(
        config=config,
        trade_client=trade_client,
        accounting_client=accounting_client,
        quote_client=BybitEntryReferenceQuoteClient(
            reference_store=entry_reference_store,
            observation_hook=observe_market_data,
        ),
        completed_bar_client=BybitDemoCompletedBarClient(),
        instrument_client=BybitInstrumentClient(),
        runtime_lease=runtime_lease,
        excursion_store=excursion_store,
        entry_provenance_store=PostgresBybitDemoEntryProvenanceStore(config.database_url),
        terminal_evidence_store=PostgresBybitDemoTerminalEvidenceStore(config.database_url),
        session_risk_store=PostgresBybitDemoSessionRiskLedgerStore(config.database_url),
        strategy_config=active_strategy,
        market_data_observation_hook=observe_market_data,
        clock_ms=active_clock,
    )
    return BybitProductComposition(
        config=config,
        startup_reconciler=BybitProductStartupReconciler(
            broker=startup_broker,
            checkpoint_store=excursion_store,
            entry_oms=entry_oms,
            reconciliation_health=reconciliation_health,
            clock_ms=active_clock,
            monotonic_fn=monotonic_fn,
        ),
        cycle_executor=cycle,
        operator_control=operator_control,
        private_stream_monitor=private_stream,
        rest_health_recorder=rest_health,
        market_data_health_recorder=market_data_health,
        reconciliation_health_recorder=reconciliation_health,
        entry_oms=entry_oms,
        monotonic_fn=monotonic_fn,
    )


def bootstrap_bybit_product_session(config: BybitProductConfig) -> Decimal:
    config.validate()
    lease_store = PostgresBybitDemoRuntimeLease(config.database_url)
    excursion_store = PostgresBybitDemoExcursionStore(
        config.database_url,
        runtime_lease=lease_store,
    )
    entry_oms = PostgresBybitEntryOms(config.database_url)
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
        reconciled = BybitProductStartupReconciler(
            broker=broker,
            checkpoint_store=excursion_store,
            entry_oms=entry_oms,
        ).run()
        if not reconciled.next_entry_allowed:
            raise RuntimeError(
                "session bootstrap requires fully reconciled flat broker/local/OMS state"
            )
        wallet = accounting.get_wallet_balance()
        session_store.initialize(
            start_bybit_demo_session_risk_ledger(
                opening_equity_usdt=wallet.total_equity_usd
            )
        )
        return wallet.total_equity_usd
    finally:
        lease_store.release(owner_token=lease.owner_token)


def _blocked_startup(reason: str) -> BybitStartupReconciliationResult:
    return BybitStartupReconciliationResult(
        status=BybitStartupReconciliationStatus.BLOCKED,
        reasons=(reason,),
        checkpoint=None,
        active_positions=(),
        open_orders=(),
        next_entry_allowed=False,
        management_allowed=False,
        terminal_recovery_required=False,
        broker_truth_complete=False,
    )


def _unique_reasons(reasons: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reasons))


def _utc_from_ms(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Bybit startup reconciliation clock must be non-negative integer ms")
    return datetime.fromtimestamp(value / 1000, tz=UTC)
