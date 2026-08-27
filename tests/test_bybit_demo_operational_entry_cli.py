from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import tools.run_bybit_demo_operator_approved_entry as cli
from app.execution.bybit_demo_control_plane import (
    BybitDemoControlDecision,
    BybitDemoControlMode,
)
from app.execution.bybit_demo_operational_entry import BybitDemoOperationalEntryStatus
from app.execution.bybit_demo_session_risk_ledger import BybitDemoSessionRiskLedger


def _fake_evidence(status: BybitDemoOperationalEntryStatus):
    return SimpleNamespace(
        status=status,
        to_payload=lambda: {
            "schema": "BYBIT_DEMO_OPERATIONAL_ENTRY_EVIDENCE_V1",
            "status": status.value,
            "live_mainnet_order_routing_allowed": False,
        },
    )


def test_main_writes_allowlisted_success_evidence(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli, "_environment_from_process", lambda: object())
    monkeypatch.setattr(cli, "_build_dependencies", lambda _environment: object())
    monkeypatch.setattr(
        cli,
        "_run_once",
        lambda _inputs, _environment, _dependencies: _fake_evidence(
            BybitDemoOperationalEntryStatus.ENTRY_CYCLE_COMPLETE
        ),
    )
    output = tmp_path / "entry.json"

    code = cli.main(
        [
            "--evidence-rank",
            "1",
            "--symbol",
            "BTCUSDT",
            "--confirm",
            "APPROVE_BYBIT_DEMO_EXECUTION",
            "--output",
            str(output),
        ]
    )

    assert code == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "ENTRY_CYCLE_COMPLETE"
    assert payload["live_mainnet_order_routing_allowed"] is False


def test_startup_failure_never_leaks_exception_text(monkeypatch, tmp_path) -> None:
    secret_marker = "postgresql://operator:super-secret@example.invalid/demo"
    monkeypatch.setattr(cli, "_environment_from_process", lambda: object())

    def _fail(_environment):
        raise RuntimeError(f"cannot connect to {secret_marker}")

    monkeypatch.setattr(cli, "_build_dependencies", _fail)
    output = tmp_path / "entry.json"

    code = cli.main(
        [
            "--evidence-rank",
            "1",
            "--symbol",
            "BTCUSDT",
            "--confirm",
            "APPROVE_BYBIT_DEMO_EXECUTION",
            "--output",
            str(output),
        ]
    )

    assert code == 2
    text = output.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["status"] == "STARTUP_BLOCKED"
    assert payload["error_type"] == "RuntimeError"
    assert payload["automatic_arm_allowed"] is False
    assert payload["ranked_fallback_allowed"] is False
    assert payload["same_invocation_additional_entry_allowed"] is False
    assert secret_marker not in text
    assert "super-secret" not in text


def test_run_once_pins_existing_arm_before_creating_approval(monkeypatch) -> None:
    events: list[str] = []
    ledger = BybitDemoSessionRiskLedger(
        opening_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1000"),
    )
    checkpoint = SimpleNamespace(ledger=ledger)
    source = SimpleNamespace(
        review_row={"symbol": "BTCUSDT"},
        bars=(object(),),
    )
    fixed_egress = object()
    evidence = object()

    class _ControlPlane:
        fixed_egress_required = True
        live_mainnet_order_routing_allowed = False
        order_writes_supported = False
        order_submission_supported = False
        immutable_records = True

        def read_decision(self, *, now: datetime):
            events.append("arm-read")
            return BybitDemoControlDecision(
                mode=BybitDemoControlMode.ARMED_NEW_ENTRIES,
                reasons=(),
                new_entry_allowed=True,
                latest_event_id="a" * 64,
                latest_event_kind="ARM_NEW_ENTRIES",
                armed_until=now + timedelta(seconds=90),
            )

    control_plane = _ControlPlane()
    dependencies = SimpleNamespace(
        accounting_client=SimpleNamespace(
            get_wallet_balance=lambda: SimpleNamespace(
                total_equity_usd=Decimal("1000")
            )
        ),
        session_store=SimpleNamespace(load_active=lambda: checkpoint),
        instrument_client=SimpleNamespace(
            fetch_symbols=lambda symbols: {symbols[0]: object()}
        ),
        fixed_egress_preflight_client=object(),
        control_plane=control_plane,
        order_client=object(),
        authorization_store=object(),
        provenance_store=object(),
        excursion_store=object(),
        completed_bar_client=object(),
        quote_client=object(),
        runtime_lease=object(),
        session_risk_committer=object(),
        terminal_evidence_store=object(),
        managed_policy=object(),
    )
    environment = cli._OperationalEnvironment(
        demo_database_dsn="postgresql://demo",
        opportunity_database_dsn="postgresql://opportunity",
        trading_api_key="trading-key",
        trading_api_secret="trading-secret",
        readonly_api_key="readonly-key",
        readonly_api_secret="readonly-secret",
        mainnet_readonly_api_key_sha256="b" * 64,
    )
    inputs = cli._OperationalInputs(
        evidence_rank=1,
        symbol="BTCUSDT",
        confirmation_phrase="APPROVE_BYBIT_DEMO_EXECUTION",
        research_site="global",
    )

    monkeypatch.setattr(cli, "PostgresCryptoLiveOpportunityReader", lambda _dsn: object())
    monkeypatch.setattr(cli, "BybitPublicKlineClient", lambda **_kwargs: object())
    monkeypatch.setattr(
        cli,
        "resolve_bybit_demo_operator_approval_source",
        lambda *_args, **_kwargs: source,
    )
    monkeypatch.setattr(
        cli,
        "PostgresBybitDemoOperationalStateReader",
        lambda _dsn: object(),
    )

    def _fixed_preflight(*_args):
        events.append("fixed-egress")
        return fixed_egress

    def _require_fixed(_result):
        events.append("fixed-egress-required")

    def _create_approval(*_args, **_kwargs):
        events.append("approval")
        return SimpleNamespace(source_evidence_rank=1, symbol="BTCUSDT")

    captured: dict[str, object] = {}

    def _run_operational(*_args, **kwargs):
        events.append("composer")
        captured.update(kwargs)
        return evidence

    monkeypatch.setattr(cli, "run_bybit_demo_fixed_egress_connected_preflight", _fixed_preflight)
    monkeypatch.setattr(cli, "require_fixed_egress_ready_for_arm", _require_fixed)
    monkeypatch.setattr(cli, "create_bybit_demo_operator_approval", _create_approval)
    monkeypatch.setattr(cli, "run_protected_bybit_demo_operational_entry", _run_operational)

    result = cli._run_once(inputs, environment, dependencies)

    assert result is evidence
    assert events.index("fixed-egress") < events.index("arm-read")
    assert events.index("arm-read") < events.index("approval")
    assert events.index("approval") < events.index("composer")
    pinned = captured["new_entry_control_plane"]
    assert pinned.pinned_event_id == "a" * 64
    cycle_policy = captured["cycle_policy"]
    assert cycle_policy.writes_enabled is True
    assert cycle_policy.require_entry_recovery_envelope is True
    assert captured["session_ledger"] is ledger
