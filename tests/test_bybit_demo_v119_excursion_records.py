from dataclasses import replace
from decimal import Decimal

import pytest

from app.execution.bybit_demo_v119_excursion_records import (
    BybitDemoExcursionStateV119,
    build_excursion_checkpoint_v119,
    canonical_json_v119,
    decode_excursion_state_v119,
    encode_excursion_state_v119,
    excursion_revision_v119,
)


def _state() -> BybitDemoExcursionStateV119:
    return BybitDemoExcursionStateV119(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=Decimal("100"),
        initial_quantity=Decimal("2"),
        stop_fraction=Decimal("0.05"),
        observation_count=2,
        latest_server_time_ms=1_700_000_000_000,
        latest_mark_price=Decimal("105.25"),
        latest_gross_r=Decimal("1.05"),
        observed_peak_favorable_r=Decimal("1.20"),
        observed_trough_r=Decimal("-0.30"),
        latest_giveback_from_peak_r=Decimal("0.15"),
        current_quantity=Decimal("1.5"),
        partial_close_seen=True,
        exchange_unrealised_pnl_usdt=Decimal("7.50"),
        projected_initial_quantity_gross_pnl_usdt=Decimal("10.50"),
        current_quantity_gross_pnl_usdt=Decimal("7.875"),
    )


def test_codec_round_trip_preserves_decimal_text_and_historical_revision_formula() -> None:
    state = _state()
    payload = encode_excursion_state_v119(state)

    assert payload["entry_price"] == "100"
    assert payload["observed_peak_favorable_r"] == "1.20"
    assert payload["exchange_unrealised_pnl_usdt"] == "7.50"
    assert decode_excursion_state_v119(payload) == state

    link = "ASTRA-DEMO-C2A4-EXAMPLE"
    revision = excursion_revision_v119(link, state)
    assert revision == "80334539a80928e41f63eb771c3cb9d4a2c87331264419fd80df8821038b97a6"

    canonical = canonical_json_v119(
        {
            "entry_order_link_id": link,
            "state": payload,
        }
    )
    assert canonical.startswith('{"entry_order_link_id":"ASTRA-DEMO-C2A4-EXAMPLE"')


def test_checkpoint_is_deterministic_and_fail_closed() -> None:
    state = _state()
    first = build_excursion_checkpoint_v119(
        entry_order_link_id="ASTRA-DEMO-C2A4-1",
        state=state,
    )
    second = build_excursion_checkpoint_v119(
        entry_order_link_id="ASTRA-DEMO-C2A4-1",
        state=state,
    )

    assert first == second
    assert first.diagnostics_only is True
    assert first.exit_threshold_retuning_allowed is False
    assert first.strategy_promotion_allowed is False
    assert first.live_mainnet_order_routing_allowed is False

    with pytest.raises(ValueError, match="ASTRA-DEMO"):
        build_excursion_checkpoint_v119(entry_order_link_id="LIVE-1", state=state)


def test_decode_rejects_unknown_missing_or_invalid_fields() -> None:
    payload = encode_excursion_state_v119(_state())

    with pytest.raises(ValueError, match="keys are invalid"):
        decode_excursion_state_v119({**payload, "hidden_future_outcome": "1"})

    missing = dict(payload)
    del missing["latest_gross_r"]
    with pytest.raises(ValueError, match="keys are invalid"):
        decode_excursion_state_v119(missing)

    with pytest.raises(ValueError, match="side"):
        decode_excursion_state_v119({**payload, "side": "Buy"})

    with pytest.raises(ValueError, match="symbol"):
        decode_excursion_state_v119({**payload, "symbol": "btcusdt"})

    with pytest.raises(ValueError, match="non-finite"):
        decode_excursion_state_v119({**payload, "latest_gross_r": "NaN"})


def test_state_rejects_unsafe_markers_and_invalid_numbers() -> None:
    state = _state()

    with pytest.raises(ValueError, match="strategy promotion"):
        replace(state, strategy_promotion_allowed=True).validate()
    with pytest.raises(ValueError, match="exit retuning"):
        replace(state, exit_threshold_retuning_allowed=True).validate()
    with pytest.raises(ValueError, match="live routing"):
        replace(state, live_mainnet_order_routing_allowed=True).validate()
    with pytest.raises(ValueError, match="diagnostics only"):
        replace(state, diagnostics_only=False).validate()
    with pytest.raises(ValueError, match="entry_price"):
        replace(state, entry_price=Decimal("0")).validate()
    with pytest.raises(ValueError, match="current_quantity"):
        replace(state, current_quantity=Decimal("-0.1")).validate()
    with pytest.raises(ValueError, match="observation_count"):
        replace(state, observation_count=-1).validate()
