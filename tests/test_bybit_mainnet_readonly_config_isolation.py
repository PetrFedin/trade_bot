import pytest

from app.runtime.bybit_product_config import BybitProductConfig, BybitProductConfigError


def _demo_env() -> dict[str, str]:
    return {
        "ASTRA_ENV": "demo",
        "ASTRA_BROKER": "bybit",
        "BYBIT_API_KEY": "demo-key",
        "BYBIT_API_SECRET": "demo-secret",
        "DATABASE_URL": "postgresql://astra:test@127.0.0.1:5432/astra",
        "BYBIT_REST_URL": "https://api-demo.bybit.com",
        "BYBIT_PRIVATE_WS_URL": "wss://stream-demo.bybit.com/v5/private",
        "BYBIT_PUBLIC_WS_URL": "wss://stream.bybit.com/v5/public/linear",
        "TRADING_WRITES_ENABLED": "true",
        "MAINNET_ENABLED": "false",
        "BYBIT_MAINNET_READONLY_API_KEY": "real-readonly-key",
        "BYBIT_MAINNET_READONLY_API_SECRET": "real-readonly-secret",
        "BYBIT_MAINNET_READONLY_SITE": "nl",
    }


def test_mainnet_readonly_credentials_cannot_promote_canonical_demo_runtime() -> None:
    config = BybitProductConfig.from_env(_demo_env())

    assert config.environment == "demo"
    assert config.rest_url == "https://api-demo.bybit.com"
    assert config.private_ws_url == "wss://stream-demo.bybit.com/v5/private"
    assert config.demo_order_writes_allowed is True
    assert config.mainnet_enabled is False
    assert config.live_mainnet_order_routing_allowed is False
    assert config.api_key == "demo-key"
    assert config.api_secret == "demo-secret"


def test_canonical_runtime_still_rejects_mainnet_rest_endpoint() -> None:
    env = _demo_env()
    env["BYBIT_REST_URL"] = "https://api.bybit.nl"

    with pytest.raises(BybitProductConfigError, match="api-demo.bybit.com"):
        BybitProductConfig.from_env(env)


def test_canonical_runtime_still_rejects_mainnet_enable_flag() -> None:
    env = _demo_env()
    env["MAINNET_ENABLED"] = "true"

    with pytest.raises(BybitProductConfigError, match="must remain false"):
        BybitProductConfig.from_env(env)
