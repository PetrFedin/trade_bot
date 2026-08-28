from __future__ import annotations

from types import SimpleNamespace

import pytest

import tools.run_bybit_demo_operator_approved_entry as entry_cli
import tools.run_bybit_demo_persistent_supervisor as supervisor_cli


def test_operator_entry_account_mismatch_blocks_before_order_client(monkeypatch) -> None:
    environment = entry_cli._OperationalEnvironment(
        demo_database_dsn="postgresql://demo",
        opportunity_database_dsn="postgresql://opportunity",
        trading_api_key="trading-key",
        trading_api_secret="trading-secret",
        readonly_api_key="readonly-key",
        readonly_api_secret="readonly-secret",
        mainnet_readonly_api_key_sha256="a" * 64,
    )
    monkeypatch.setattr(
        entry_cli,
        "verify_bybit_demo_postgres_schema",
        lambda _dsn: SimpleNamespace(passed=True),
    )
    monkeypatch.setattr(
        entry_cli,
        "BybitDemoTradingCredentialReadOnlyInspector",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        entry_cli,
        "run_bybit_demo_trading_credential_preflight",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True),
    )
    monkeypatch.setattr(
        entry_cli,
        "BybitDemoAccountIdentityInspector",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        entry_cli,
        "require_same_bybit_demo_account",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("account mismatch")),
    )
    order_client_called = False

    def _order_client(**_kwargs):
        nonlocal order_client_called
        order_client_called = True
        raise AssertionError("order client must not be composed after account mismatch")

    monkeypatch.setattr(entry_cli, "OmsAwareBybitDemoStopRatchetClient", _order_client)

    with pytest.raises(RuntimeError, match="account mismatch"):
        entry_cli._build_dependencies(environment)

    assert order_client_called is False


def test_persistent_supervisor_account_mismatch_blocks_before_order_client(monkeypatch) -> None:
    for name, value in (
        ("BYBIT_DEMO_DATABASE_DSN", "postgresql://demo"),
        ("BYBIT_DEMO_TRADING_API_KEY", "trading-key"),
        ("BYBIT_DEMO_TRADING_API_SECRET", "trading-secret"),
        ("BYBIT_DEMO_READONLY_API_KEY", "readonly-key"),
        ("BYBIT_DEMO_READONLY_API_SECRET", "readonly-secret"),
        ("BYBIT_MAINNET_READONLY_API_KEY_SHA256", "b" * 64),
    ):
        monkeypatch.setenv(name, value)

    monkeypatch.setattr(
        supervisor_cli,
        "verify_bybit_demo_postgres_schema",
        lambda _dsn: SimpleNamespace(passed=True),
    )
    monkeypatch.setattr(
        supervisor_cli,
        "BybitDemoPreflightAccountClient",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        supervisor_cli,
        "PostgresBybitDemoOperationalStateReader",
        lambda _dsn: object(),
    )
    monkeypatch.setattr(
        supervisor_cli,
        "run_bybit_demo_connected_preflight",
        lambda *_args: SimpleNamespace(status="READY", reasons=()),
    )
    monkeypatch.setattr(
        supervisor_cli,
        "BybitDemoTradingCredentialReadOnlyInspector",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        supervisor_cli,
        "run_bybit_demo_trading_credential_preflight",
        lambda *_args, **_kwargs: SimpleNamespace(passed=True, reasons=()),
    )
    monkeypatch.setattr(
        supervisor_cli,
        "BybitDemoAccountIdentityInspector",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        supervisor_cli,
        "require_same_bybit_demo_account",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("account mismatch")),
    )
    order_client_called = False

    def _order_client(**_kwargs):
        nonlocal order_client_called
        order_client_called = True
        raise AssertionError("order client must not be composed after account mismatch")

    monkeypatch.setattr(supervisor_cli, "BybitDemoStopRatchetClient", _order_client)

    with pytest.raises(RuntimeError, match="account mismatch"):
        supervisor_cli._build_dependencies_from_environment()

    assert order_client_called is False
