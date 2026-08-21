from decimal import Decimal

import pytest

from app.execution.bybit_entry_recovery import (
    BybitEntryRecoveryEnvelope,
    decode_entry_recovery_envelope,
    encode_entry_recovery_envelope,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan


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
        estimated_round_trip_cost_usdt=Decimal("1.20"),
        estimated_stop_loss_after_cost_usdt=Decimal("11.20"),
        target_net_profit_usd=Decimal("20"),
        required_move_fraction=Decimal("0.0212"),
        expected_move_fraction=Decimal("0.035"),
        expected_net_edge_usd=Decimal("13.8"),
        quality_score=Decimal("0.91"),
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


def _envelope() -> BybitEntryRecoveryEnvelope:
    config = CryptoPerpStrategyConfig(taker_fee_rate=Decimal("0.00055"))
    return BybitEntryRecoveryEnvelope(
        entry_order_link_id="ASTRA-DEMO-E-RECOVERY-0001",
        order_side="Buy",
        approved_order_quantity=Decimal("0.01"),
        trade_plan=_plan(),
        instrument=_instrument(),
        strategy_config=config,
        planned_exit_mode="FIXED_20_TARGET",
    )


def test_entry_recovery_envelope_roundtrips_every_frozen_fact() -> None:
    envelope = _envelope()

    canonical, record_sha = encode_entry_recovery_envelope(envelope)
    record = decode_entry_recovery_envelope(canonical, expected_sha256=record_sha)

    assert record.envelope == envelope
    assert record.record_sha256 == record_sha
    assert len(record_sha) == 64
    assert record.envelope.strategy_config.taker_fee_rate == Decimal("0.00055")
    assert record.envelope.instrument == _instrument()
    assert record.envelope.trade_plan == _plan()
    assert record.envelope.live_mainnet_order_routing_allowed is False


def test_entry_recovery_envelope_checksum_and_canonical_payload_fail_closed() -> None:
    canonical, record_sha = encode_entry_recovery_envelope(_envelope())

    with pytest.raises(ValueError, match="checksum mismatch"):
        decode_entry_recovery_envelope(canonical, expected_sha256="0" * 64)

    noncanonical = canonical.replace(",", ", ", 1)
    with pytest.raises(ValueError):
        decode_entry_recovery_envelope(noncanonical)

    assert record_sha != "0" * 64


def test_entry_recovery_envelope_rejects_side_quantity_and_live_capability_drift() -> None:
    envelope = _envelope()

    with pytest.raises(ValueError, match="order side does not match"):
        BybitEntryRecoveryEnvelope(
            **{**envelope.__dict__, "order_side": "Sell"}
        ).validate()

    with pytest.raises(ValueError, match="approved quantity does not match"):
        BybitEntryRecoveryEnvelope(
            **{**envelope.__dict__, "approved_order_quantity": Decimal("0.02")}
        ).validate()

    with pytest.raises(ValueError, match="cannot grant live routing"):
        BybitEntryRecoveryEnvelope(
            **{**envelope.__dict__, "live_mainnet_order_routing_allowed": True}
        ).validate()
