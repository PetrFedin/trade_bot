from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.marketdata.bybit_opportunity_postgres import PostgresBybitOpportunityStore
from app.marketdata.bybit_opportunity_registry import build_bybit_opportunity_snapshot
from app.marketdata.bybit_research_universe import (
    BybitResearchInstrument,
    BybitResearchTicker,
    BybitResearchUniversePolicy,
    select_bybit_research_universe,
)
from tools.snapshot_bybit_opportunity_registry import (
    persist_snapshot_from_env,
    run_public_opportunity_snapshot,
    write_snapshot,
)

_DAY_MS = 86_400_000
_NOW = datetime(2026, 8, 23, 0, tzinfo=UTC)
_NOW_MS = int(_NOW.timestamp() * 1000)


def _instrument(symbol: str, *, days: int) -> BybitResearchInstrument:
    return BybitResearchInstrument(
        symbol=symbol,
        base_coin=symbol.removesuffix("USDT"),
        quote_coin="USDT",
        settle_coin="USDT",
        contract_type="LinearPerpetual",
        status="Trading",
        symbol_type="innovation",
        launch_time_ms=_NOW_MS - days * _DAY_MS,
        delivery_time_ms=0,
        is_pre_listing=False,
    )


def _ticker(symbol: str, index: int) -> BybitResearchTicker:
    return BybitResearchTicker(
        symbol=symbol,
        last_price=Decimal("100") + index,
        bid_price=Decimal("99.95") + index,
        ask_price=Decimal("100.05") + index,
        turnover_24h_usdt=Decimal("500000000") - index * Decimal("10000000"),
        volume_24h=Decimal("1000000") - index * Decimal("10000"),
        open_interest=Decimal("500000") - index * Decimal("1000"),
        open_interest_value_usdt=Decimal("100000000") - index * Decimal("2000000"),
        funding_rate=Decimal("0.0001") + index * Decimal("0.000001"),
        price_24h_fraction=Decimal("0.03") - index * Decimal("0.001"),
    )


def _inputs(count: int = 15) -> tuple[
    tuple[BybitResearchInstrument, ...],
    tuple[BybitResearchTicker, ...],
]:
    symbols = tuple(f"C{index:02d}USDT" for index in range(count))
    return (
        tuple(_instrument(symbol, days=1000 - index * 20) for index, symbol in enumerate(symbols)),
        tuple(_ticker(symbol, index) for index, symbol in enumerate(symbols)),
    )


def _policy() -> BybitResearchUniversePolicy:
    return BybitResearchUniversePolicy(
        top_n=10,
        minimum_listing_days=90,
        minimum_turnover_24h_usdt=Decimal("20000000"),
        minimum_open_interest_value_usdt=Decimal("5000000"),
        maximum_spread_bps=Decimal("50"),
        maximum_abs_funding_rate=Decimal("0.01"),
    )


def test_registry_top10_exactly_matches_qualified_selector_and_keeps_extended_watchlist() -> None:
    instruments, tickers = _inputs(15)
    direct = select_bybit_research_universe(
        instruments,
        tickers,
        observed_at_ms=_NOW_MS,
        host="api.bybit.eu",
        policy=_policy(),
    )
    snapshot = build_bybit_opportunity_snapshot(
        instruments,
        tickers,
        observed_at_ms=_NOW_MS,
        host="api.bybit.eu",
        universe_policy=_policy(),
        registry_limit=12,
    )

    assert snapshot.top10_complete is True
    assert snapshot.top10_symbols == tuple(item.symbol for item in direct.selected)
    assert len(snapshot.candidates) == 12
    assert snapshot.eligible_symbol_count == 15
    assert snapshot.registry_population_complete is False
    assert [item.rank for item in snapshot.candidates] == list(range(1, 13))
    assert all(item.signal_side == "UNASSIGNED" for item in snapshot.candidates)
    assert all(item.trade_actionable is False for item in snapshot.candidates)
    assert all(item.bybit_live_order_routing_allowed is False for item in snapshot.candidates)
    assert snapshot.snapshot_id == snapshot.snapshot_id
    assert len(snapshot.snapshot_id) == 64
    payload = snapshot.to_payload()
    assert payload["snapshot_id"] == snapshot.snapshot_id
    assert payload["trade_actionable"] is False
    assert payload["strategy_promotion_allowed"] is False
    assert payload["live_activation_allowed"] is False
    assert payload["bybit_live_order_routing_allowed"] is False
    assert payload["causal_claim_allowed"] is False
    assert payload["predictive_guarantee_allowed"] is False


def test_registry_refuses_fake_top10_when_fewer_than_ten_symbols_are_eligible() -> None:
    instruments, tickers = _inputs(7)
    snapshot = build_bybit_opportunity_snapshot(
        instruments,
        tickers,
        observed_at_ms=_NOW_MS,
        host="api.bybit.com",
        universe_policy=_policy(),
        registry_limit=20,
    )
    assert snapshot.top10_complete is False
    assert len(snapshot.top10_symbols) == 7
    assert snapshot.blockers == ("INSUFFICIENT_ELIGIBLE_SYMBOLS_FOR_TOP10",)
    assert snapshot.registry_population_complete is True
    with pytest.raises(ValueError, match="within \[10, 50\]"):
        build_bybit_opportunity_snapshot(
            instruments,
            tickers,
            observed_at_ms=_NOW_MS,
            registry_limit=9,
        )


class _FakeUniverseClient:
    def __init__(
        self,
        instruments: tuple[BybitResearchInstrument, ...],
        tickers: tuple[BybitResearchTicker, ...],
    ) -> None:
        self._instruments = instruments
        self._tickers = tickers
        self.instrument_calls = 0
        self.ticker_calls = 0

    def fetch_instruments(self) -> tuple[BybitResearchInstrument, ...]:
        self.instrument_calls += 1
        return self._instruments

    def fetch_tickers(self) -> tuple[BybitResearchTicker, ...]:
        self.ticker_calls += 1
        return self._tickers


def test_public_snapshot_command_path_is_timestamped_atomic_and_never_needs_secrets(
    tmp_path: Path,
) -> None:
    instruments, tickers = _inputs(12)
    client = _FakeUniverseClient(instruments, tickers)
    snapshot = run_public_opportunity_snapshot(
        observed_at=_NOW,
        bybit_site="eu",
        registry_limit=12,
        universe_policy=_policy(),
        universe_client=client,
    )
    output = tmp_path / "snapshot.json"
    write_snapshot(snapshot, output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["observed_at_ms"] == _NOW_MS
    assert payload["host"] == "api.bybit.eu"
    assert payload["top10_symbols"] == list(snapshot.top10_symbols)
    assert payload["bybit_live_order_routing_allowed"] is False
    assert client.instrument_calls == 1
    assert client.ticker_calls == 1
    assert not (tmp_path / "snapshot.json.tmp").exists()


def test_postgres_boundary_is_storage_only_and_missing_dsn_fails_without_secret_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BYBIT_OPPORTUNITY_DATABASE_DSN", raising=False)
    instruments, tickers = _inputs(10)
    snapshot = build_bybit_opportunity_snapshot(
        instruments,
        tickers,
        observed_at_ms=_NOW_MS,
        universe_policy=_policy(),
    )
    with pytest.raises(RuntimeError, match="environment variable is missing"):
        persist_snapshot_from_env(snapshot)

    store = PostgresBybitOpportunityStore("postgresql://example.invalid/astra")
    assert store.live_mainnet_order_routing_allowed is False
    assert store.order_writes_supported is False
    assert not hasattr(store, "place_order")
    assert not hasattr(store, "cancel_order")


def test_v110_migration_is_append_only_and_cannot_mark_candidates_trade_actionable() -> None:
    sql = Path("migrations/v110/001_bybit_opportunity_registry.sql").read_text(
        encoding="utf-8"
    )
    assert "append-only" in sql
    assert "trade_actionable = false" in sql
    assert "strategy_promotion_allowed = false" in sql
    assert "bybit_live_order_routing_allowed = false" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "CREATE TABLE IF NOT EXISTS astra_bybit_opportunity_snapshot_v110" in sql
    assert "CREATE TABLE IF NOT EXISTS astra_bybit_opportunity_candidate_v110" in sql
