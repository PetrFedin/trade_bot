from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

import tools.refresh_bybit_mainnet_readonly_evidence_registry as refresh_module
from app.execution.bybit_mainnet_readonly import (
    BybitMainnetAccountInfo,
    BybitMainnetApiKeyInfo,
    BybitMainnetPosition,
    BybitMainnetWalletBalance,
)
from app.runtime.bybit_mainnet_readonly_probe import BybitMainnetReadOnlySnapshot
from app.strategy.crypto_readonly_account_context import (
    CryptoReadOnlyAccountAwareRegistrySnapshot,
)
from tools.refresh_bybit_mainnet_readonly_evidence_registry import (
    run_mainnet_readonly_account_aware_refresh,
)

_NOW = datetime(2026, 8, 23, 18, tzinfo=UTC)


def _snapshot(*, position_value: Decimal | None = Decimal("300")) -> BybitMainnetReadOnlySnapshot:
    snapshot = BybitMainnetReadOnlySnapshot(
        api_key=BybitMainnetApiKeyInfo(
            key_fingerprint_sha256="a" * 64,
            read_only=True,
            ip_bindings=("203.0.113.10",),
            key_type=1,
            note="trade-bot-readonly",
            permissions=("Contract:Position",),
        ),
        account=BybitMainnetAccountInfo(
            margin_mode="REGULAR_MARGIN",
            unified_margin_status=5,
            updated_time_ms=int(_NOW.timestamp() * 1000),
        ),
        wallet=BybitMainnetWalletBalance(
            total_equity_usd=Decimal("1000"),
            total_wallet_balance_usd=Decimal("950"),
            total_margin_balance_usd=Decimal("980"),
            total_available_balance_usd=Decimal("600"),
            total_perp_upl_usd=Decimal("30"),
            total_initial_margin_usd=Decimal("200"),
            total_maintenance_margin_usd=Decimal("50"),
            usdt_wallet_balance=Decimal("900"),
        ),
        positions=(
            BybitMainnetPosition(
                symbol="BTCUSDT",
                side="Buy",
                size=Decimal("0.01"),
                position_idx=0,
                average_price=Decimal("100000"),
                mark_price=Decimal("101000"),
                position_value=position_value,
                unrealised_pnl=Decimal("10"),
                liquidation_price=Decimal("50000"),
                leverage=Decimal("3"),
            ),
        ),
        api_host="api.bybit.eu",
    )
    snapshot.validate()
    return snapshot


def test_bridge_passes_conservative_readonly_sizing_into_existing_ranking(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    market = object()
    ranking = object()
    account_aware = object()

    def fake_refresh(**kwargs: Any):
        captured.update(kwargs)
        return market, ranking

    def fake_overlay(passed_ranking: Any, account: Any, *, observed_at: datetime):
        assert passed_ranking is ranking
        assert account.sizing_capital_usd_equivalent == Decimal("600")
        assert account.gross_position_value_usd == Decimal("300")
        assert observed_at == _NOW
        return account_aware

    monkeypatch.setattr(refresh_module, "run_live_evidence_refresh", fake_refresh)
    monkeypatch.setattr(
        refresh_module,
        "build_crypto_account_aware_registry_snapshot",
        fake_overlay,
    )

    result = run_mainnet_readonly_account_aware_refresh(
        evidence_report={"diagnostic": "TEST"},
        mainnet_snapshot=_snapshot(),
        observed_at=_NOW,
        bybit_site="eu",
        registry_limit=50,
        universe_client=object(),
        kline_client=object(),
        derivatives_client=object(),
    )

    assert result[0] is market
    assert result[1] is ranking
    assert result[3] is account_aware
    assert captured["observed_at"] == _NOW
    assert captured["bybit_site"] == "eu"
    assert captured["equity_usdt"] == Decimal("600")
    assert captured["equity_source"] == (
        "BYBIT_MAINNET_READONLY_AVAILABLE_CAPITAL_USD_EQUIVALENT"
    )
    assert captured["registry_limit"] == 50
    assert captured["universe_client"] is not None
    assert captured["kline_client"] is not None
    assert captured["derivatives_client"] is not None


def test_bridge_refuses_incomplete_position_exposure_before_market_ranking(monkeypatch) -> None:
    called = False

    def fake_refresh(**_kwargs: Any):
        nonlocal called
        called = True
        raise AssertionError("market ranking must not run with incomplete account exposure")

    monkeypatch.setattr(refresh_module, "run_live_evidence_refresh", fake_refresh)
    with pytest.raises(RuntimeError, match="position exposure is incomplete"):
        run_mainnet_readonly_account_aware_refresh(
            evidence_report={"diagnostic": "TEST"},
            mainnet_snapshot=_snapshot(position_value=None),
            observed_at=_NOW,
            bybit_site="eu",
        )
    assert called is False


def test_account_aware_snapshot_type_remains_non_trading_surface() -> None:
    assert not hasattr(CryptoReadOnlyAccountAwareRegistrySnapshot, "create_order")
    assert not hasattr(CryptoReadOnlyAccountAwareRegistrySnapshot, "amend_order")
    assert not hasattr(CryptoReadOnlyAccountAwareRegistrySnapshot, "cancel_order")
