from __future__ import annotations

import json
from decimal import Decimal

import pytest

import tools.bybit_product as bybit_product
from app.runtime.bybit_product_service import (
    BybitProductServiceResult,
    BybitProductServiceStatus,
)


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


def test_unknown_service_status_cannot_be_silently_mapped() -> None:
    with pytest.raises(ValueError, match="unsupported Bybit product service status"):
        bybit_product._service_exit_code("UNKNOWN")  # type: ignore[arg-type]
