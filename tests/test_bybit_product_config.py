from __future__ import annotations

import pytest

from app.runtime.bybit_product_config import BybitProductConfig, BybitProductConfigError


def _env(**overrides: str) -> dict[str, str]:
    values = {
        "ASTRA_ENV": "demo",
        "ASTRA_BROKER": "bybit",
        "BYBIT_API_KEY": "demo-key",
        "BYBIT_API_SECRET": "demo-secret",
        "DATABASE_URL": "postgresql://astra:secret@db.example/astra",
        "BYBIT_REST_URL": "https://api-demo.bybit.com",
        "BYBIT_PRIVATE_WS_URL": "wss://stream-demo.bybit.com/v5/private",
        "BYBIT_PUBLIC_WS_URL": "wss://stream.bybit.com/v5/public/linear",
        "TRADING_WRITES_ENABLED": "false",
        "MAINNET_ENABLED": "false",
    }
    values.update(overrides)
    return values


def test_product_config_accepts_only_qualified_demo_boundary() -> None:
    config = BybitProductConfig.from_env(_env())

    assert config.environment == "demo"
    assert config.broker == "bybit"
    assert config.live_mainnet_order_routing_allowed is False
    assert config.demo_order_writes_allowed is False
    assert config.redacted()["credentials_configured"] is True
    assert "demo-secret" not in repr(config)
    assert "demo-key" not in repr(config)


def test_demo_writes_require_explicit_enablement_but_never_enable_mainnet() -> None:
    config = BybitProductConfig.from_env(_env(TRADING_WRITES_ENABLED="true"))

    assert config.demo_order_writes_allowed is True
    assert config.live_mainnet_order_routing_allowed is False


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ASTRA_ENV", "live"),
        ("ASTRA_ENV", "mainnet"),
        ("MAINNET_ENABLED", "true"),
        ("BYBIT_REST_URL", "https://api.bybit.com"),
        ("BYBIT_PRIVATE_WS_URL", "wss://stream.bybit.com/v5/private"),
        ("BYBIT_PUBLIC_WS_URL", "wss://stream-testnet.bybit.com/v5/public/linear"),
    ],
)
def test_mainnet_or_unqualified_endpoint_config_is_hard_rejected(key: str, value: str) -> None:
    with pytest.raises(BybitProductConfigError):
        BybitProductConfig.from_env(_env(**{key: value}))


def test_non_postgres_authoritative_database_is_rejected() -> None:
    with pytest.raises(BybitProductConfigError, match="PostgreSQL"):
        BybitProductConfig.from_env(_env(DATABASE_URL="sqlite:///astra.db"))


def test_credentials_and_database_can_only_be_optional_for_offline_preflight() -> None:
    config = BybitProductConfig.from_env(
        _env(BYBIT_API_KEY="", BYBIT_API_SECRET="", DATABASE_URL=""),
        require_credentials=False,
        require_database=False,
    )

    assert config.redacted()["credentials_configured"] is False
    assert config.redacted()["database_configured"] is False


def test_required_credentials_fail_closed() -> None:
    with pytest.raises(BybitProductConfigError, match="BYBIT_API_KEY"):
        BybitProductConfig.from_env(_env(BYBIT_API_KEY=""))


def test_boolean_values_are_explicit() -> None:
    with pytest.raises(BybitProductConfigError, match="explicit boolean"):
        BybitProductConfig.from_env(_env(TRADING_WRITES_ENABLED="maybe"))


def test_poll_and_shutdown_limits_are_bounded() -> None:
    with pytest.raises(BybitProductConfigError, match="ASTRA_POLL_INTERVAL_MS"):
        BybitProductConfig.from_env(_env(ASTRA_POLL_INTERVAL_MS="10"))
    with pytest.raises(BybitProductConfigError, match="ASTRA_SHUTDOWN_GRACE_SECONDS"):
        BybitProductConfig.from_env(_env(ASTRA_SHUTDOWN_GRACE_SECONDS="1000"))
