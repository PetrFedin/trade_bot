from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.execution.bybit_demo_entry_provenance_store import BybitDemoEntryProvenanceReceipt
from app.execution.bybit_demo_runtime_lease import BybitDemoRuntimeLease
from app.execution.bybit_demo_trading_runtime import (
    BybitDemoTradingRuntimeStatus,
    run_bybit_demo_trading_runtime,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_session_risk import CryptoSessionRiskState


@dataclass(frozen=True)
class _SafeEntryResult:
    live_mainnet_order_routing_allowed: bool = False


class _SafeDependency:
    live_mainnet_order_routing_allowed = False


class _ExcursionStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def load(self):
        raise FileNotFoundError


class _Lease:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    automatic_stale_takeover_allowed = False

    def __init__(self) -> None:
        self.released = False

    def acquire(self) -> BybitDemoRuntimeLease:
        return BybitDemoRuntimeLease(
            owner_token="a" * 64,
            created_time_ms=1,
            process_id=1,
        )

    def release(self, *, owner_token: str) -> None:
        assert owner_token == "a" * 64
        self.released = True


class _ProvenanceStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True
    realized_pnl_storage_allowed = False

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.persisted: object | None = None

    def persist(self, provenance: object) -> BybitDemoEntryProvenanceReceipt:
        if self.fail:
            raise RuntimeError("provenance disk unavailable")
        self.persisted = provenance
        return BybitDemoEntryProvenanceReceipt(
            entry_order_link_id="ASTRA-DEMO-E-PROV",
            record_sha256="b" * 64,
            idempotent_existing_record=False,
        )


def _instrument() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.1"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("1000"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _session() -> CryptoSessionRiskState:
    return CryptoSessionRiskState(
        opening_equity_usdt=Decimal("1000"),
        current_equity_usdt=Decimal("1000"),
        peak_equity_usdt=Decimal("1000"),
    )


def _run(*, provenance_store: object | None, builder):
    lease = _Lease()
    result = run_bybit_demo_trading_runtime(
        {},
        instruments={"BTCUSDT": _instrument()},
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=_session(),
        now=datetime(2026, 8, 19, tzinfo=UTC),
        now_ms=1_000,
        client=_SafeDependency(),
        accounting_client=None,
        excursion_store=_ExcursionStore(),
        completed_bar_client=_SafeDependency(),
        quote_client=_SafeDependency(),
        runtime_lease=lease,
        entry_provenance_store=provenance_store,
        entry_executor=lambda *_args, **_kwargs: _SafeEntryResult(),
        build_entry_provenance=builder,
    )
    return result, lease


def test_entry_provenance_is_persisted_after_entry_cycle_without_changing_trade_status() -> None:
    provenance = object()
    store = _ProvenanceStore()

    result, lease = _run(
        provenance_store=store,
        builder=lambda _entry: provenance,
    )

    assert result.status is BybitDemoTradingRuntimeStatus.ENTRY_CYCLE_EXECUTED
    assert result.reasons == ()
    assert result.entry_result is not None
    assert result.entry_provenance is provenance
    assert result.entry_provenance_receipt is not None
    assert result.entry_provenance_persisted is True
    assert store.persisted is provenance
    assert lease.released is True
    assert result.next_entry_allowed is False


def test_provenance_build_failure_is_diagnostic_and_does_not_rewrite_entry_result() -> None:
    store = _ProvenanceStore()

    def _fail(_entry: object):
        raise RuntimeError("cannot build provenance")

    result, lease = _run(provenance_store=store, builder=_fail)

    assert result.status is BybitDemoTradingRuntimeStatus.ENTRY_CYCLE_EXECUTED
    assert result.reasons == ("ENTRY_PROVENANCE_BUILD_FAILED:RuntimeError",)
    assert result.entry_result is not None
    assert result.entry_provenance is None
    assert result.entry_provenance_persisted is False
    assert store.persisted is None
    assert lease.released is True


def test_provenance_persist_failure_preserves_built_record_for_observability() -> None:
    provenance = object()
    store = _ProvenanceStore(fail=True)

    result, lease = _run(
        provenance_store=store,
        builder=lambda _entry: provenance,
    )

    assert result.status is BybitDemoTradingRuntimeStatus.ENTRY_CYCLE_EXECUTED
    assert result.reasons == ("ENTRY_PROVENANCE_PERSIST_FAILED:RuntimeError",)
    assert result.entry_provenance is provenance
    assert result.entry_provenance_receipt is None
    assert result.entry_provenance_persisted is False
    assert lease.released is True


def test_no_provenance_store_skips_builder_entirely() -> None:
    called = False

    def _builder(_entry: object):
        nonlocal called
        called = True
        return object()

    result, _lease = _run(provenance_store=None, builder=_builder)

    assert result.status is BybitDemoTradingRuntimeStatus.ENTRY_CYCLE_EXECUTED
    assert called is False
    assert result.entry_provenance is None
    assert result.entry_provenance_persisted is False


def test_unsafe_provenance_store_is_rejected_before_runtime_lease() -> None:
    store = _ProvenanceStore()
    store.realized_pnl_storage_allowed = True

    with pytest.raises(ValueError, match="forbids realized PnL"):
        _run(provenance_store=store, builder=lambda _entry: object())
