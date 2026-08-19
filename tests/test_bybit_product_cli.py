from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

import tools.bybit_product as bybit_product
from app.application.bybit_operator_control import (
    BybitOperatorAction,
    BybitOperatorMode,
    BybitOperatorSnapshot,
)
from app.runtime.bybit_product_service import (
    BybitProductServiceResult,
    BybitProductServiceStatus,
)

NOW = datetime(2026, 8, 19, 22, 0, tzinfo=UTC)


def _env(**overrides: str) -> dict[str, str]:
    values = {
        "ASTRA_ENV": "demo",
        "ASTRA_BROKER": "bybit",
        "ASTRA_SYMBOLS": "BTCUSDT,ETHUSDT,SOLUSDT",
        "BYBIT_API_KEY": "demo-key",
        "BYBIT_API_SECRET": "demo-secret",
        "DATABASE_URL": "postgresql://astra:secret@db.example/astra",
        "TRADING_WRITES_ENABLED": "false",
        "MAINNET_ENABLED": "false",
    }
    values.update(overrides)
    return values


def _service_result(
    status: BybitProductServiceStatus = BybitProductServiceStatus.STOPPED,
) -> BybitProductServiceResult:
    return BybitProductServiceResult(
        status=status,
        reasons=(status.value,),
        completed_cycles=3,
        startup=None,
        last_cycle_result=None,
        graceful_stop=status is BybitProductServiceStatus.STOPPED,
    )


class _FakeOperatorControl:
    live_mainnet_order_routing_allowed = False
    active_trade_safety_management_allowed = True

    def __init__(self) -> None:
        self.mode = BybitOperatorMode.PAUSED
        self.generation = 7
        self.last_operation: tuple[str, str, str] | None = None
        self.history_limit: int | None = None

    def _snapshot(
        self,
        *,
        actor: str = "SYSTEM",
        reason: str = "test state",
    ) -> BybitOperatorSnapshot:
        return BybitOperatorSnapshot(
            mode=self.mode,
            generation=self.generation,
            updated_at=NOW,
            updated_by=actor,
            reason=reason,
        )

    def inspect(self) -> BybitOperatorSnapshot:
        return self._snapshot()

    def history(self, *, limit: int = 100) -> tuple[BybitOperatorAction, ...]:
        self.history_limit = limit
        return (
            BybitOperatorAction(
                action_id="history-1",
                generation=7,
                from_mode=BybitOperatorMode.RUNNING,
                to_mode=BybitOperatorMode.PAUSED,
                actor="operator-a",
                reason="maintenance",
                occurred_at=NOW,
            ),
        )

    def _change(
        self,
        operation: str,
        mode: BybitOperatorMode,
        *,
        actor: str,
        reason: str,
    ) -> BybitOperatorSnapshot:
        self.last_operation = (operation, actor, reason)
        self.mode = mode
        self.generation += 1
        return self._snapshot(actor=actor, reason=reason)

    def pause(self, *, actor: str, reason: str) -> BybitOperatorSnapshot:
        return self._change("pause", BybitOperatorMode.PAUSED, actor=actor, reason=reason)

    def resume(self, *, actor: str, reason: str) -> BybitOperatorSnapshot:
        return self._change("resume", BybitOperatorMode.RUNNING, actor=actor, reason=reason)

    def enter_read_only(self, *, actor: str, reason: str) -> BybitOperatorSnapshot:
        return self._change(
            "read-only",
            BybitOperatorMode.READ_ONLY,
            actor=actor,
            reason=reason,
        )

    def kill(self, *, actor: str, reason: str) -> BybitOperatorSnapshot:
        return self._change("kill", BybitOperatorMode.KILLED, actor=actor, reason=reason)

    def clear_kill(self, *, actor: str, reason: str) -> BybitOperatorSnapshot:
        return self._change("clear-kill", BybitOperatorMode.PAUSED, actor=actor, reason=reason)


def test_run_command_uses_canonical_product_runner_and_emits_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = []

    def _run(config):
        seen.append(config.redacted())
        return _service_result()

    monkeypatch.setattr(bybit_product, "run_product", _run)

    exit_code = bybit_product.main(["run"], env=_env())

    assert exit_code == 0
    assert len(seen) == 1
    assert seen[0]["symbols"] == ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "STOPPED"
    assert payload["completed_cycles"] == 3
    assert payload["live_mainnet_order_routing_allowed"] is False


def test_run_requires_explicit_symbol_universe_before_composition(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def _run(_config):
        nonlocal called
        called = True
        return _service_result()

    monkeypatch.setattr(bybit_product, "run_product", _run)

    exit_code = bybit_product.main(["run"], env=_env(ASTRA_SYMBOLS=""))

    assert exit_code == 2
    assert called is False
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "CONFIG_REJECTED"
    assert "ASTRA_SYMBOLS" in payload["reason"]


def test_mainnet_configuration_is_rejected_before_runner(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def _run(_config):
        nonlocal called
        called = True
        return _service_result()

    monkeypatch.setattr(bybit_product, "run_product", _run)

    exit_code = bybit_product.main(["run"], env=_env(MAINNET_ENABLED="true"))

    assert exit_code == 2
    assert called is False
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "CONFIG_REJECTED"
    assert payload["live_mainnet_order_routing_allowed"] is False


def test_operational_service_failures_have_distinct_nonzero_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statuses = {
        BybitProductServiceStatus.STARTUP_BLOCKED: 20,
        BybitProductServiceStatus.STARTUP_FAILED: 21,
        BybitProductServiceStatus.CYCLE_FAILED: 22,
    }
    for status, expected_exit in statuses.items():
        monkeypatch.setattr(
            bybit_product,
            "run_product",
            lambda _config, status=status: _service_result(status),
        )
        assert bybit_product.main(["run"], env=_env()) == expected_exit


def test_bootstrap_session_does_not_require_symbol_universe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = []

    def _bootstrap(config):
        seen.append(config.redacted())
        return Decimal("1000.25")

    monkeypatch.setattr(bybit_product, "bootstrap_bybit_product_session", _bootstrap)

    exit_code = bybit_product.main(
        ["bootstrap-session"],
        env=_env(ASTRA_SYMBOLS=""),
    )

    assert exit_code == 0
    assert len(seen) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "live_mainnet_order_routing_allowed": False,
        "opening_equity_usdt": "1000.25",
        "status": "SESSION_BOOTSTRAPPED",
    }


def test_operator_status_is_available_without_exchange_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _FakeOperatorControl()
    seen_config = []

    def _control(config):
        seen_config.append(config)
        return fake

    monkeypatch.setattr(bybit_product, "_operator_control", _control)

    exit_code = bybit_product.main(
        ["operator", "status"],
        env=_env(BYBIT_API_KEY="", BYBIT_API_SECRET="", ASTRA_SYMBOLS=""),
    )

    assert exit_code == 0
    assert len(seen_config) == 1
    assert seen_config[0].api_key == ""
    assert seen_config[0].api_secret == ""
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "OPERATOR_STATE"
    assert payload["mode"] == "PAUSED"
    assert payload["new_entries_allowed"] is False
    assert payload["active_trade_safety_management_allowed"] is True
    assert payload["live_mainnet_order_routing_allowed"] is False


def test_operator_history_exposes_append_only_audit_without_exchange_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _FakeOperatorControl()
    monkeypatch.setattr(bybit_product, "_operator_control", lambda _config: fake)

    exit_code = bybit_product.main(
        ["operator", "history", "--limit", "5"],
        env=_env(BYBIT_API_KEY="", BYBIT_API_SECRET=""),
    )

    assert exit_code == 0
    assert fake.history_limit == 5
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "OPERATOR_HISTORY"
    assert payload["actions"] == [
        {
            "action_id": "history-1",
            "actor": "operator-a",
            "from_mode": "RUNNING",
            "generation": 7,
            "occurred_at": NOW.isoformat(),
            "reason": "maintenance",
            "to_mode": "PAUSED",
        }
    ]
    assert payload["live_mainnet_order_routing_allowed"] is False


@pytest.mark.parametrize(
    ("command", "expected_mode"),
    [
        ("pause", "PAUSED"),
        ("resume", "RUNNING"),
        ("read-only", "READ_ONLY"),
        ("kill", "KILLED"),
        ("clear-kill", "PAUSED"),
    ],
)
def test_operator_mutations_use_the_single_durable_control_store(
    command: str,
    expected_mode: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = _FakeOperatorControl()
    monkeypatch.setattr(bybit_product, "_operator_control", lambda _config: fake)

    exit_code = bybit_product.main(
        ["operator", command, "--actor", "operator-a", "--reason", "incident review"],
        env=_env(BYBIT_API_KEY="", BYBIT_API_SECRET=""),
    )

    assert exit_code == 0
    assert fake.last_operation == (command, "operator-a", "incident review")
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "OPERATOR_UPDATED"
    assert payload["mode"] == expected_mode
    assert payload["new_entries_allowed"] is (expected_mode == "RUNNING")
    assert payload["live_mainnet_order_routing_allowed"] is False


def test_operator_commands_still_reject_mainnet_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def _control(_config):
        nonlocal called
        called = True
        return _FakeOperatorControl()

    monkeypatch.setattr(bybit_product, "_operator_control", _control)

    exit_code = bybit_product.main(
        ["operator", "status"],
        env=_env(
            BYBIT_API_KEY="",
            BYBIT_API_SECRET="",
            MAINNET_ENABLED="true",
        ),
    )

    assert exit_code == 2
    assert called is False
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "CONFIG_REJECTED"
    assert payload["live_mainnet_order_routing_allowed"] is False


def test_unknown_service_status_cannot_be_silently_mapped() -> None:
    with pytest.raises(ValueError, match="unsupported Bybit product service status"):
        bybit_product._service_exit_code("UNKNOWN")  # type: ignore[arg-type]
