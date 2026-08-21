import pytest

pytest.importorskip("psycopg")

from app.application.bybit_recovery_startup import RecoveryAwareBybitProductStartupReconciler
from app.application.bybit_product_composition import build_bybit_product_composition
from app.oms.bybit_entry_recovery_candidates import PostgresBybitEntryRecoveryCandidateReader
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


def test_canonical_bybit_composition_uses_recovery_aware_startup_with_shared_authority() -> None:
    composition = build_bybit_product_composition(_config())
    startup = composition.startup_reconciler
    client = composition.cycle_executor.trade_client

    assert isinstance(startup, RecoveryAwareBybitProductStartupReconciler)
    assert isinstance(startup.candidate_reader, PostgresBybitEntryRecoveryCandidateReader)
    assert startup.entry_oms is composition.entry_oms
    assert startup.candidate_reader.entry_oms is composition.entry_oms
    assert startup.recovery_store is client.entry_recovery_store
    assert startup.runtime_lease is composition.cycle_executor.runtime_lease
    assert startup.excursion_store is composition.cycle_executor.excursion_store
    assert startup.recovery_client is client
    assert startup.reconciliation_health is composition.reconciliation_health_recorder
    assert startup.live_mainnet_order_routing_allowed is False
