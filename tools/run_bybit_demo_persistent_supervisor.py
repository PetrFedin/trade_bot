from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

from app.execution.bybit_demo_account_reader import BybitDemoAccountingClient
from app.execution.bybit_demo_connected_preflight import (
    BybitDemoConnectedPreflightStatus,
    BybitDemoPreflightAccountClient,
    PostgresBybitDemoOperationalStateReader,
    run_bybit_demo_connected_preflight,
)
from app.execution.bybit_demo_managed_trade_poll import BybitDemoManagedTradePollPolicy
from app.execution.bybit_demo_max_hold_close import BybitDemoMaxHoldClosePolicy
from app.execution.bybit_demo_persistent_supervisor import (
    BybitDemoPersistentSupervisorResult,
    BybitDemoPersistentSupervisorStatus,
    run_bybit_demo_persistent_supervisor_cycle,
)
from app.execution.bybit_demo_postgres_bootstrap import verify_bybit_demo_postgres_schema
from app.execution.bybit_demo_postgres_excursion_store import PostgresBybitDemoExcursionStore
from app.execution.bybit_demo_postgres_runtime_lease import PostgresBybitDemoRuntimeLease
from app.execution.bybit_demo_postgres_session_risk_store import (
    PostgresBybitDemoSessionRiskLedgerStore,
)
from app.execution.bybit_demo_postgres_terminal_evidence_store import (
    PostgresBybitDemoTerminalEvidenceStore,
)
from app.execution.bybit_demo_session_risk_flatten import BybitDemoSessionRiskFlattenPolicy
from app.execution.bybit_demo_session_risk_runtime import (
    PostgresBybitDemoSessionRiskCommitter,
    PostgresBybitDemoSessionRiskObserver,
)
from app.execution.bybit_demo_stop_ratchet_client import BybitDemoStopRatchetClient
from app.execution.bybit_demo_trade_management_runtime import (
    BybitDemoTradeManagementRuntimePolicy,
)
from app.execution.bybit_demo_trading_credential_preflight import (
    BybitDemoTradingCredentialReadOnlyInspector,
    run_bybit_demo_trading_credential_preflight,
)
from app.marketdata.bybit_demo_completed_bars import BybitDemoCompletedBarClient
from app.marketdata.bybit_demo_instruments import BybitDemoInstrumentClient
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuoteClient
from app.strategy.crypto_perp import CryptoPerpStrategyConfig

_EXIT_OK = 0
_EXIT_BLOCKED = 2


@dataclass(frozen=True)
class _SupervisorDependencies:
    excursion_store: PostgresBybitDemoExcursionStore
    accounting_client: BybitDemoAccountingClient
    order_client: BybitDemoStopRatchetClient
    completed_bar_client: BybitDemoCompletedBarClient
    quote_client: BybitDemoMarketQuoteClient
    instrument_client: BybitDemoInstrumentClient
    runtime_lease: PostgresBybitDemoRuntimeLease
    terminal_evidence_store: PostgresBybitDemoTerminalEvidenceStore
    session_risk_committer: PostgresBybitDemoSessionRiskCommitter
    session_risk_observer: PostgresBybitDemoSessionRiskObserver
    managed_policy: BybitDemoManagedTradePollPolicy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run restart-safe management for an already-open Bybit Demo trade.",
    )
    parser.add_argument("--mode", choices=("once", "loop"), default="once")
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.interval_seconds <= 60:
        parser.error("--interval-seconds must be within [1, 60]")

    try:
        dependencies = _build_dependencies_from_environment()
    except Exception as exc:  # noqa: BLE001 - startup must expose only sanitized failure class.
        payload = _startup_failure_payload(exc)
        _emit(payload, output=args.output)
        return _EXIT_BLOCKED

    stop = Event()
    if args.mode == "loop":
        _install_signal_handlers(stop)

    last_payload: dict[str, Any] | None = None
    while True:
        try:
            result = _run_one_cycle(dependencies)
            payload = _result_payload(result)
        except Exception as exc:  # noqa: BLE001 - process must stop on unknown management state.
            payload = _cycle_failure_payload(exc)
            _emit(payload, output=args.output)
            return _EXIT_BLOCKED

        last_payload = payload
        _emit(payload, output=args.output)
        if result.status is BybitDemoPersistentSupervisorStatus.BLOCKED:
            return _EXIT_BLOCKED
        if args.mode == "once":
            return _EXIT_OK
        if stop.wait(args.interval_seconds):
            break

    if last_payload is None:
        last_payload = {
            "schema": "BYBIT_DEMO_PERSISTENT_SUPERVISOR_V1",
            "status": "STOPPED_BEFORE_FIRST_CYCLE",
            "blocked": False,
            "live_mainnet_order_routing_allowed": False,
        }
        _emit(last_payload, output=args.output)
    return _EXIT_OK


def _build_dependencies_from_environment() -> _SupervisorDependencies:
    dsn = _required_env("BYBIT_DEMO_DATABASE_DSN")
    trading_key = _required_env("BYBIT_DEMO_TRADING_API_KEY")
    trading_secret = _required_env("BYBIT_DEMO_TRADING_API_SECRET")
    readonly_key = _required_env("BYBIT_DEMO_READONLY_API_KEY")
    readonly_secret = _required_env("BYBIT_DEMO_READONLY_API_SECRET")
    mainnet_readonly_fingerprint = _required_env(
        "BYBIT_MAINNET_READONLY_API_KEY_SHA256"
    )

    schema = verify_bybit_demo_postgres_schema(dsn)
    if not schema.passed:
        raise RuntimeError("Bybit Demo PostgreSQL v119-v123 schema is not verified")

    read_preflight_client = BybitDemoPreflightAccountClient(
        api_key=readonly_key,
        api_secret=readonly_secret,
    )
    connected = run_bybit_demo_connected_preflight(
        read_preflight_client,
        PostgresBybitDemoOperationalStateReader(dsn),
    )
    if connected.status is BybitDemoConnectedPreflightStatus.BLOCKED:
        detail = ",".join(connected.reasons) or "CONNECTED_PREFLIGHT_BLOCKED"
        raise RuntimeError(f"Bybit Demo connected preflight blocked:{detail}")

    inspector = BybitDemoTradingCredentialReadOnlyInspector(
        api_key=trading_key,
        api_secret=trading_secret,
    )
    credential = run_bybit_demo_trading_credential_preflight(
        inspector,
        demo_readonly_api_key_sha256=hashlib.sha256(
            readonly_key.encode("utf-8")
        ).hexdigest(),
        mainnet_readonly_api_key_sha256=mainnet_readonly_fingerprint,
    )
    if not credential.passed:
        detail = ",".join(credential.reasons) or "TRADING_CREDENTIAL_BLOCKED"
        raise RuntimeError(f"Bybit Demo trading credential preflight blocked:{detail}")

    session_store = PostgresBybitDemoSessionRiskLedgerStore(dsn)
    session_store.load_active()

    return _SupervisorDependencies(
        excursion_store=PostgresBybitDemoExcursionStore(dsn),
        accounting_client=BybitDemoAccountingClient(
            api_key=readonly_key,
            api_secret=readonly_secret,
        ),
        order_client=BybitDemoStopRatchetClient(
            api_key=trading_key,
            api_secret=trading_secret,
        ),
        completed_bar_client=BybitDemoCompletedBarClient(),
        quote_client=BybitDemoMarketQuoteClient(),
        instrument_client=BybitDemoInstrumentClient(),
        runtime_lease=PostgresBybitDemoRuntimeLease(dsn),
        terminal_evidence_store=PostgresBybitDemoTerminalEvidenceStore(dsn),
        session_risk_committer=PostgresBybitDemoSessionRiskCommitter(session_store),
        session_risk_observer=PostgresBybitDemoSessionRiskObserver(session_store),
        managed_policy=BybitDemoManagedTradePollPolicy(
            trade_management=BybitDemoTradeManagementRuntimePolicy(
                stop_ratchet_writes_enabled=True,
            ),
            max_hold_close=BybitDemoMaxHoldClosePolicy(writes_enabled=True),
            session_risk_flatten=BybitDemoSessionRiskFlattenPolicy(
                writes_enabled=True,
            ),
        ),
    )


def _run_one_cycle(dependencies: _SupervisorDependencies) -> BybitDemoPersistentSupervisorResult:
    instruments = {}
    try:
        checkpoint = dependencies.excursion_store.load()
    except FileNotFoundError:
        checkpoint = None
    if checkpoint is not None:
        symbol = checkpoint.state.symbol
        instruments = dependencies.instrument_client.fetch_symbols((symbol,))

    now = datetime.now(UTC)
    return run_bybit_demo_persistent_supervisor_cycle(
        instruments=instruments,
        strategy_config=CryptoPerpStrategyConfig(),
        now=now,
        now_ms=int(now.timestamp() * 1000),
        client=dependencies.order_client,
        accounting_client=dependencies.accounting_client,
        excursion_store=dependencies.excursion_store,
        completed_bar_client=dependencies.completed_bar_client,
        quote_client=dependencies.quote_client,
        runtime_lease=dependencies.runtime_lease,
        terminal_evidence_store=dependencies.terminal_evidence_store,
        session_risk_committer=dependencies.session_risk_committer,
        session_risk_observer=dependencies.session_risk_observer,
        managed_policy=dependencies.managed_policy,
    )


def _result_payload(result: BybitDemoPersistentSupervisorResult) -> dict[str, Any]:
    risk_action = None
    high_water_advanced = None
    outcome_count = None
    if result.session_risk is not None:
        high_water_advanced = result.session_risk.high_water_advanced
        outcome_count = result.session_risk.outcome_count
        from app.strategy.crypto_session_risk import evaluate_crypto_session_risk

        risk_action = evaluate_crypto_session_risk(
            result.session_risk.session_state
        ).action.value
    runtime_status = None if result.runtime is None else result.runtime.status.value
    return {
        "schema": "BYBIT_DEMO_PERSISTENT_SUPERVISOR_V1",
        "status": result.status.value,
        "blocked": result.status is BybitDemoPersistentSupervisorStatus.BLOCKED,
        "reasons": list(result.reasons),
        "active_symbol": result.active_symbol,
        "runtime_status": runtime_status,
        "session_risk_action": risk_action,
        "session_high_water_advanced": high_water_advanced,
        "reconciled_terminal_outcome_count": outcome_count,
        "new_entry_attempted": result.new_entry_attempted,
        "autonomous_entry_allowed": result.autonomous_entry_allowed,
        "operator_approval_bypass_allowed": result.operator_approval_bypass_allowed,
        "same_invocation_additional_entry_allowed": (
            result.same_invocation_additional_entry_allowed
        ),
        "demo_only": result.demo_only,
        "live_mainnet_order_routing_allowed": result.live_mainnet_order_routing_allowed,
    }


def _startup_failure_payload(exc: Exception) -> dict[str, Any]:
    return {
        "schema": "BYBIT_DEMO_PERSISTENT_SUPERVISOR_V1",
        "status": "STARTUP_BLOCKED",
        "blocked": True,
        "error_type": type(exc).__name__,
        "new_entry_attempted": False,
        "autonomous_entry_allowed": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _cycle_failure_payload(exc: Exception) -> dict[str, Any]:
    return {
        "schema": "BYBIT_DEMO_PERSISTENT_SUPERVISOR_V1",
        "status": "CYCLE_BLOCKED",
        "blocked": True,
        "error_type": type(exc).__name__,
        "new_entry_attempted": False,
        "autonomous_entry_allowed": False,
        "live_mainnet_order_routing_allowed": False,
    }


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is missing:{name}")
    return value


def _install_signal_handlers(stop: Event) -> None:
    def _handle(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def _emit(payload: dict[str, Any], *, output: Path | None) -> None:
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    print(text, flush=True)
    if output is None:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    os.replace(temporary, output)


if __name__ == "__main__":
    sys.exit(main())
