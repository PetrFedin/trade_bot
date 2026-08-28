from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import tools.run_bybit_demo_persistent_supervisor as supervisor_cli
from app.execution.bybit_demo_persistent_supervisor import (
    BybitDemoPersistentSupervisorResult,
    BybitDemoPersistentSupervisorStatus,
)
from app.execution.bybit_demo_session_risk_runtime import BybitDemoSessionRiskObservation
from app.execution.bybit_demo_trading_runtime import BybitDemoTradingRuntimeStatus
from app.strategy.crypto_session_risk import CryptoSessionRiskState


def test_startup_failure_artifact_exposes_only_exception_type(monkeypatch, tmp_path) -> None:
    secret_marker = "postgresql://user:super-secret@example/db"

    def _fail():
        raise RuntimeError(f"cannot connect to {secret_marker}")

    monkeypatch.setattr(supervisor_cli, "_build_dependencies_from_environment", _fail)
    output = tmp_path / "supervisor.json"

    code = supervisor_cli.main(["--mode", "once", "--output", str(output)])

    assert code == 2
    text = output.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["status"] == "STARTUP_BLOCKED"
    assert payload["error_type"] == "RuntimeError"
    assert payload["demo_account_identity_verified"] is False
    assert secret_marker not in text
    assert "super-secret" not in text


def test_main_marks_successful_cycle_as_same_account_verified(monkeypatch, tmp_path) -> None:
    dependencies = SimpleNamespace(same_account_verified=True)
    result = BybitDemoPersistentSupervisorResult(
        status=BybitDemoPersistentSupervisorStatus.IDLE_NO_ACTIVE_TRADE,
        reasons=(),
        active_symbol=None,
        runtime=None,
        session_risk=None,
        new_entry_attempted=False,
    )
    monkeypatch.setattr(
        supervisor_cli,
        "_build_dependencies_from_environment",
        lambda: dependencies,
    )
    monkeypatch.setattr(supervisor_cli, "_run_one_cycle", lambda _deps: result)
    output = tmp_path / "supervisor.json"

    code = supervisor_cli.main(["--mode", "once", "--output", str(output)])

    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "IDLE_NO_ACTIVE_TRADE"
    assert payload["demo_account_identity_verified"] is True
    assert payload["new_entry_attempted"] is False


def test_status_payload_does_not_expose_equity_or_durable_revision() -> None:
    revision = "a" * 64
    observation = BybitDemoSessionRiskObservation(
        ledger_revision_sha256=revision,
        outcome_count=3,
        session_state=CryptoSessionRiskState(
            opening_equity_usdt=Decimal("1000"),
            current_equity_usdt=Decimal("947.321"),
            peak_equity_usdt=Decimal("1102.456"),
            realized_pnl_usdt=Decimal("-17.4"),
            execution_cost_usdt=Decimal("7.1"),
            consecutive_losses=2,
        ),
        high_water_advanced=False,
    )
    runtime = SimpleNamespace(status=BybitDemoTradingRuntimeStatus.ACTIVE_TRADE_POLLED)
    result = BybitDemoPersistentSupervisorResult(
        status=BybitDemoPersistentSupervisorStatus.ACTIVE_TRADE_CYCLE,
        reasons=("OPEN_POSITION_MANAGED",),
        active_symbol="BTCUSDT",
        runtime=runtime,  # type: ignore[arg-type]
        session_risk=observation,
        new_entry_attempted=False,
    )

    payload = supervisor_cli._result_payload(result)
    text = json.dumps(payload, sort_keys=True)

    assert payload["active_symbol"] == "BTCUSDT"
    assert payload["reconciled_terminal_outcome_count"] == 3
    assert "947.321" not in text
    assert "1102.456" not in text
    assert "1000" not in text
    assert revision not in text
