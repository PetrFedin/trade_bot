from __future__ import annotations

import json
from decimal import Decimal

import pytest

import tools.bybit_product as bybit_product
from app.execution.bybit_demo_cash_reconciliation import BybitDemoCashBaseline


def _env(**overrides: str) -> dict[str, str]:
    values = {
        "ASTRA_ENV": "demo",
        "ASTRA_BROKER": "bybit",
        "ASTRA_SYMBOLS": "",
        "BYBIT_API_KEY": "demo-key",
        "BYBIT_API_SECRET": "demo-secret",
        "DATABASE_URL": "postgresql://astra:secret@db.example/astra",
        "TRADING_WRITES_ENABLED": "false",
        "MAINNET_ENABLED": "false",
    }
    values.update(overrides)
    return values


def test_bootstrap_cash_does_not_require_symbol_universe_and_emits_exact_baseline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen = []
    baseline = BybitDemoCashBaseline(
        currency="USDT",
        wallet_balance_usdt=Decimal("1000.25"),
        cumulative_all_in_pnl_usdt=Decimal("12.50"),
        session_revision="a" * 64,
        created_time_ms=1_700_000_000_000,
    )

    def _bootstrap(config):
        seen.append(config.redacted())
        return baseline

    monkeypatch.setattr(bybit_product, "bootstrap_bybit_product_cash_baseline", _bootstrap)

    exit_code = bybit_product.main(["bootstrap-cash"], env=_env())

    assert exit_code == 0
    assert len(seen) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "status": "CASH_BASELINE_BOOTSTRAPPED",
        "currency": "USDT",
        "wallet_balance_usdt": "1000.25",
        "cumulative_all_in_pnl_usdt": "12.50",
        "session_revision": "a" * 64,
        "created_time_ms": 1_700_000_000_000,
        "live_mainnet_order_routing_allowed": False,
    }


def test_bootstrap_cash_requires_exchange_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def _bootstrap(_config):
        nonlocal called
        called = True
        raise AssertionError("must not be called")

    monkeypatch.setattr(bybit_product, "bootstrap_bybit_product_cash_baseline", _bootstrap)

    exit_code = bybit_product.main(
        ["bootstrap-cash"],
        env=_env(BYBIT_API_KEY="", BYBIT_API_SECRET=""),
    )

    assert exit_code == 2
    assert called is False
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "CONFIG_REJECTED"
    assert payload["live_mainnet_order_routing_allowed"] is False


def test_bootstrap_cash_rejects_mainnet_before_baseline_write(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def _bootstrap(_config):
        nonlocal called
        called = True
        raise AssertionError("must not be called")

    monkeypatch.setattr(bybit_product, "bootstrap_bybit_product_cash_baseline", _bootstrap)

    exit_code = bybit_product.main(
        ["bootstrap-cash"],
        env=_env(MAINNET_ENABLED="true"),
    )

    assert exit_code == 2
    assert called is False
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "CONFIG_REJECTED"
    assert payload["live_mainnet_order_routing_allowed"] is False
