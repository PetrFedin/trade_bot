from __future__ import annotations

import json
from decimal import Decimal

import pytest

from app.execution.bybit_demo import BybitDemoPosition
from app.execution.bybit_demo_excursion_store import JsonFileBybitDemoExcursionStore
from app.execution.bybit_demo_excursion_tracker import (
    observe_bybit_demo_trade_excursion,
    start_bybit_demo_trade_excursion,
)
from app.marketdata.bybit_demo_quotes import BybitDemoMarketQuote
from app.strategy.crypto_perp import CryptoSide, CryptoTradePlan


def _plan() -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-18T20:00:00+00:00",
        reference_price=Decimal("100"),
        notional_usdt=Decimal("200"),
        reference_quantity=Decimal("2"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.05"),
        estimated_round_trip_cost_usdt=Decimal("1"),
        estimated_stop_loss_after_cost_usdt=Decimal("11"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.105"),
        expected_move_fraction=Decimal("0.15"),
        expected_net_edge_usd=Decimal("29"),
        quality_score=Decimal("2"),
    )


def _position(*, size: str = "2", unrealised: str = "0") -> BybitDemoPosition:
    return BybitDemoPosition(
        symbol="BTCUSDT",
        side="Buy",
        size=Decimal(size),
        average_price=Decimal("100"),
        unrealised_pnl=Decimal(unrealised),
        liquidation_price=Decimal("50"),
    )


def _quote(mark: str, server_time_ms: int) -> BybitDemoMarketQuote:
    mark_price = Decimal(mark)
    return BybitDemoMarketQuote(
        symbol="BTCUSDT",
        last_price=mark_price,
        mark_price=mark_price,
        bid_price=mark_price - Decimal("0.01"),
        ask_price=mark_price + Decimal("0.01"),
        server_time_ms=server_time_ms,
        received_time_ms=server_time_ms + 100,
        age_ms=100,
    )


def _observed_state():
    state = start_bybit_demo_trade_excursion(_plan(), position=_position())
    return observe_bybit_demo_trade_excursion(
        state,
        position=_position(size="1", unrealised="10"),
        quote=_quote("110", 1_000),
    )


def test_excursion_store_round_trip_preserves_peak_and_partial_close(tmp_path) -> None:
    store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")
    state = _observed_state()

    checkpoint = store.initialize(
        entry_order_link_id="ASTRA-DEMO-E-EXCURSION",
        state=state,
    )
    loaded = store.load()

    assert loaded.revision == checkpoint.revision
    assert loaded.entry_order_link_id == "ASTRA-DEMO-E-EXCURSION"
    assert loaded.state.observation_count == 1
    assert loaded.state.observed_peak_favorable_r == Decimal("2")
    assert loaded.state.latest_giveback_from_peak_r == Decimal("0")
    assert loaded.state.partial_close_seen is True
    assert loaded.state.current_quantity == Decimal("1")
    assert loaded.state.live_mainnet_order_routing_allowed is False
    assert store.live_mainnet_order_routing_allowed is False
    assert store.order_writes_supported is False


def test_excursion_store_never_silently_initializes_missing_state(tmp_path) -> None:
    store = JsonFileBybitDemoExcursionStore(tmp_path / "missing.json")

    with pytest.raises(FileNotFoundError):
        store.load()


def test_excursion_store_optimistic_concurrency_rejects_stale_save(tmp_path) -> None:
    store = JsonFileBybitDemoExcursionStore(tmp_path / "excursion.json")
    initial = store.initialize(
        entry_order_link_id="ASTRA-DEMO-E-EXCURSION",
        state=start_bybit_demo_trade_excursion(_plan(), position=_position()),
    )
    updated_state = _observed_state()
    current = store.save(
        entry_order_link_id="ASTRA-DEMO-E-EXCURSION",
        state=updated_state,
        expected_revision=initial.revision,
    )

    with pytest.raises(RuntimeError, match="revision changed concurrently"):
        store.save(
            entry_order_link_id="ASTRA-DEMO-E-EXCURSION",
            state=updated_state,
            expected_revision=initial.revision,
        )

    assert store.load().revision == current.revision


def test_excursion_store_rejects_tampered_checkpoint(tmp_path) -> None:
    path = tmp_path / "excursion.json"
    store = JsonFileBybitDemoExcursionStore(path)
    store.initialize(
        entry_order_link_id="ASTRA-DEMO-E-EXCURSION",
        state=_observed_state(),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["state"]["observed_peak_favorable_r"] = "999"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        store.load()


def test_excursion_store_clear_requires_current_revision(tmp_path) -> None:
    path = tmp_path / "excursion.json"
    store = JsonFileBybitDemoExcursionStore(path)
    checkpoint = store.initialize(
        entry_order_link_id="ASTRA-DEMO-E-EXCURSION",
        state=_observed_state(),
    )

    with pytest.raises(ValueError, match="sha256 hex"):
        store.clear(expected_revision="bad")

    store.clear(expected_revision=checkpoint.revision)
    assert path.exists() is False
    with pytest.raises(FileNotFoundError):
        store.load()


def test_excursion_store_rejects_symlink_checkpoint(tmp_path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "excursion.json"
    link.symlink_to(target)
    store = JsonFileBybitDemoExcursionStore(link)

    with pytest.raises(ValueError, match="symlink"):
        store.load()
