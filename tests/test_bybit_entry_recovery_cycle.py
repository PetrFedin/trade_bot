from decimal import Decimal
from types import SimpleNamespace

import pytest

import app.execution.bybit_demo_cycle as cycle
from app.execution.bybit_demo_cycle import BybitDemoCyclePolicy, BybitDemoCycleStatus
from app.execution.bybit_entry_recovery import BybitEntryRecoveryReceipt
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan
from app.strategy.crypto_session_risk import CryptoSessionRiskState


def _plan() -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-21T12:00:00+00:00",
        reference_price=Decimal("100000"),
        notional_usdt=Decimal("1000"),
        reference_quantity=Decimal("0.01"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.01"),
        estimated_round_trip_cost_usdt=Decimal("1"),
        estimated_stop_loss_after_cost_usdt=Decimal("11"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.021"),
        expected_move_fraction=Decimal("0.05"),
        expected_net_edge_usd=Decimal("40"),
        quality_score=Decimal("0.95"),
    )


def _instrument() -> BybitInstrumentSpec:
    return BybitInstrumentSpec(
        symbol="BTCUSDT",
        status="Trading",
        contract_type="LinearPerpetual",
        base_coin="BTC",
        quote_coin="USDT",
        settle_coin="USDT",
        tick_size=Decimal("0.10"),
        min_order_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        min_notional_value=Decimal("5"),
        max_market_order_qty=Decimal("100"),
        max_leverage=Decimal("100"),
        funding_interval_minutes=480,
    )


def _patch_entry_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cycle,
        "plan_bybit_demo_entry",
        lambda *_args, **_kwargs: SimpleNamespace(
            eligible=True,
            order=SimpleNamespace(
                order_link_id="ASTRA-DEMO-E-RECOVERY-CYCLE",
                side="Buy",
                quantity=Decimal("0.01"),
            ),
            reasons=(),
        ),
    )
    monkeypatch.setattr(
        cycle,
        "revalidate_entry_at_actual_taker_fee",
        lambda *_args, **_kwargs: SimpleNamespace(
            eligible=True,
            reasons=(),
            modeled_round_trip_cost_usdt=Decimal("1.10"),
            modeled_stop_loss_after_cost_usdt=Decimal("11.10"),
            required_move_fraction=Decimal("0.0211"),
            modeled_expected_net_edge_usd=Decimal("38.90"),
        ),
    )
    monkeypatch.setattr(
        cycle,
        "evaluate_crypto_runner_admission",
        lambda *_args, **_kwargs: SimpleNamespace(eligible=False, reasons=("FIXED",)),
    )


class _Client:
    live_mainnet_order_routing_allowed = False
    entry_recovery_required = True

    def __init__(self, events: list[str], recovery_store=None) -> None:
        self.events = events
        self.entry_recovery_store = recovery_store

    def get_positions(self, *, settle_coin: str = "USDT"):
        return ()

    def get_fee_rate(self, *, symbol: str):
        return SimpleNamespace(
            symbol=symbol,
            taker_fee_rate=Decimal("0.00055"),
            maker_fee_rate=Decimal("0.0002"),
        )

    def place_market_order(self, _request):
        self.events.append("post")
        return SimpleNamespace(
            order_id="broker-1",
            order_link_id="ASTRA-DEMO-E-RECOVERY-CYCLE",
            accepted=True,
        )


class _Store:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True

    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self.events = events
        self.fail = fail
        self.envelopes = []

    def persist(self, envelope):
        self.events.append("persist")
        if self.fail:
            raise RuntimeError("database unavailable")
        envelope.validate()
        self.envelopes.append(envelope)
        return BybitEntryRecoveryReceipt(
            entry_order_link_id=envelope.entry_order_link_id,
            record_sha256="a" * 64,
            idempotent_existing_record=False,
        )


def _run(client: _Client):
    return cycle.execute_bybit_demo_trade_cycle(
        _plan(),
        instrument=_instrument(),
        strategy_config=CryptoPerpStrategyConfig(),
        session_state=CryptoSessionRiskState(
            opening_equity_usdt=Decimal("1000"),
            current_equity_usdt=Decimal("1000"),
            peak_equity_usdt=Decimal("1000"),
        ),
        client=client,
        cycle_policy=BybitDemoCyclePolicy(
            writes_enabled=True,
            reconciliation_attempts=1,
        ),
        sleeper=lambda _seconds: None,
    )


def test_canonical_cycle_persists_exact_fee_adjusted_envelope_before_entry_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entry_path(monkeypatch)
    events: list[str] = []
    store = _Store(events)
    result = _run(_Client(events, recovery_store=store))

    assert events == ["persist", "post"]
    assert result.status is BybitDemoCycleStatus.ENTRY_ACKED_FILL_UNRESOLVED
    assert len(store.envelopes) == 1
    envelope = store.envelopes[0]
    assert envelope.entry_order_link_id == "ASTRA-DEMO-E-RECOVERY-CYCLE"
    assert envelope.order_side == "Buy"
    assert envelope.approved_order_quantity == Decimal("0.01")
    assert envelope.trade_plan.reference_quantity == Decimal("0.01")
    assert envelope.trade_plan.estimated_round_trip_cost_usdt == Decimal("1.10")
    assert envelope.trade_plan.expected_net_edge_usd == Decimal("38.90")
    assert envelope.strategy_config.taker_fee_rate == Decimal("0.00055")
    assert envelope.instrument == _instrument()
    assert envelope.planned_exit_mode == "FIXED_20_TARGET"


def test_recovery_persistence_failure_blocks_entry_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entry_path(monkeypatch)
    events: list[str] = []
    store = _Store(events, fail=True)
    result = _run(_Client(events, recovery_store=store))

    assert events == ["persist"]
    assert result.status is BybitDemoCycleStatus.ENTRY_BLOCKED
    assert result.reasons == ("ENTRY_RECOVERY_ENVELOPE_PERSIST_FAILED:RuntimeError",)
    assert result.entry_ack is None


def test_canonical_recovery_requirement_blocks_missing_store_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_entry_path(monkeypatch)
    events: list[str] = []
    result = _run(_Client(events, recovery_store=None))

    assert events == []
    assert result.status is BybitDemoCycleStatus.ENTRY_BLOCKED
    assert result.reasons == ("ENTRY_RECOVERY_ENVELOPE_STORE_REQUIRED",)
    assert result.entry_ack is None
