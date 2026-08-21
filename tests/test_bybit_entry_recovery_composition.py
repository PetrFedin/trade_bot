import pytest

pytest.importorskip("psycopg")

from app.application.bybit_product_composition import build_bybit_product_composition
from app.runtime.bybit_product_config import BybitProductConfig


def _config() -> BybitProductConfig:
    return BybitProductConfig.from_env(
        {
            "ASTRA_ENV": "demo",
            "ASTRA_BROKER": "bybit",
            "ASTRA_SYMBOLS": "BTCUSDT",
            "ASTRA_BAR_INTERVAL": "5",
            "ASTRA_BAR_LOOKBACK": "50",
            "BYBIT_API_KEY": "key",
            "BYBIT_API_SECRET": "secret",
            "DATABASE_URL": "postgresql://astra:secret@db/astra",
            "TRADING_WRITES_ENABLED": "false",
            "MAINNET_ENABLED": "false",
        },
        require_universe=True,
    )


def test_canonical_bybit_composition_binds_immutable_entry_recovery_store() -> None:
    composition = build_bybit_product_composition(_config())
    client = composition.cycle_executor.trade_client

    assert client.entry_recovery_required is True
    assert client.entry_recovery_store is not None
    assert client.entry_recovery_store.immutable_records is True
    assert client.entry_recovery_store.order_writes_supported is False
    assert client.entry_recovery_store.live_mainnet_order_routing_allowed is False
