"""Acceptance coverage for #143 parts A and B: spec snapshot and normalization.

Test names carry the acceptance number from the issue so the mapping is checkable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.instrument import (
    DEFAULT_MAXIMUM_SPEC_AGE,
    InstrumentNormalizationError,
    InstrumentSpec,
    InstrumentStatus,
    NormalizedOrderEconomics,
    RejectionReason,
    normalize_order,
)
from app.domain.trading import Side

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def spec(**overrides) -> InstrumentSpec:
    base = {
        "venue": "bybit",
        "environment": "mainnet",
        "category": "linear",
        "symbol": "BTCUSDT",
        "status": InstrumentStatus.TRADING,
        "base_coin": "BTC",
        "quote_coin": "USDT",
        "settle_coin": "USDT",
        "tick_size": Decimal("0.10"),
        "minimum_price": Decimal("0.10"),
        "maximum_price": Decimal("1999999.80"),
        "quantity_step": Decimal("0.001"),
        "minimum_quantity": Decimal("0.001"),
        "maximum_quantity": Decimal("1500"),
        "minimum_notional": Decimal("5"),
        "source_timestamp": NOW,
        "observed_timestamp": NOW,
    }
    base.update(overrides)
    built = InstrumentSpec(**base)
    built.validate()
    return built


def normalize(instrument: InstrumentSpec, **kwargs) -> NormalizedOrderEconomics:
    call = {
        "side": Side.BUY,
        "quantity": Decimal("0.01"),
        "limit_price": Decimal("70000"),
        "observed_at": NOW,
    }
    call.update(kwargs)
    return normalize_order(instrument, **call)


# --- acceptance 1: off-tick price -------------------------------------------------


def test_acceptance_1_buy_price_rounds_down_to_tick() -> None:
    """A buy must never pay more than asked to reach a permitted tick."""
    result = normalize(spec(), side=Side.BUY, limit_price=Decimal("70000.17"))
    assert result.limit_price == Decimal("70000.10")
    assert result.price_was_moved is True


def test_acceptance_1_sell_price_rounds_up_to_tick() -> None:
    """A sell must never accept less than asked to reach a permitted tick."""
    result = normalize(spec(), side=Side.SELL, limit_price=Decimal("70000.17"))
    assert result.limit_price == Decimal("70000.20")


def test_acceptance_1_on_tick_price_is_untouched() -> None:
    result = normalize(spec(), limit_price=Decimal("70000.10"))
    assert result.limit_price == Decimal("70000.10")
    assert result.price_was_moved is False


def test_acceptance_1_normalization_is_deterministic() -> None:
    instrument = spec()
    first = normalize(instrument, limit_price=Decimal("70000.17"))
    second = normalize(instrument, limit_price=Decimal("70000.17"))
    assert first == second


# --- acceptance 2: off-step quantity ----------------------------------------------


def test_acceptance_2_quantity_never_rounds_up() -> None:
    """Rounding a quantity upward would buy more than risk admitted."""
    result = normalize(spec(), quantity=Decimal("0.0019"))
    assert result.quantity == Decimal("0.001")
    assert result.quantity < Decimal("0.0019")
    assert result.quantity_was_reduced is True


def test_acceptance_2_on_step_quantity_is_untouched() -> None:
    result = normalize(spec(), quantity=Decimal("0.010"))
    assert result.quantity == Decimal("0.010")
    assert result.quantity_was_reduced is False


def test_acceptance_2_notional_uses_the_normalized_values() -> None:
    """Risk must see the economics that will be sent, not the ones that were asked for."""
    result = normalize(spec(), quantity=Decimal("0.0019"), limit_price=Decimal("70000.17"))
    assert result.notional == result.quantity * result.limit_price


# --- acceptance 3: below minimum quantity or notional -----------------------------


def test_acceptance_3_quantity_rounding_to_zero_is_rejected() -> None:
    with pytest.raises(InstrumentNormalizationError) as caught:
        normalize(spec(), quantity=Decimal("0.0004"))
    assert caught.value.reason is RejectionReason.QUANTITY_ROUNDS_TO_ZERO


def test_acceptance_3_below_minimum_quantity_is_rejected_not_raised_to_minimum() -> None:
    """Resizing up to the minimum would send an order nobody admitted."""
    with pytest.raises(InstrumentNormalizationError) as caught:
        normalize(spec(minimum_quantity=Decimal("0.01")), quantity=Decimal("0.005"))
    assert caught.value.reason is RejectionReason.BELOW_MINIMUM_QUANTITY


def test_acceptance_3_below_minimum_notional_is_rejected() -> None:
    with pytest.raises(InstrumentNormalizationError) as caught:
        normalize(spec(), quantity=Decimal("0.001"), limit_price=Decimal("1000"))
    assert caught.value.reason is RejectionReason.BELOW_MINIMUM_NOTIONAL


def test_acceptance_3_rejection_happens_before_any_economics_exist() -> None:
    """A rejected order must yield nothing that could be mistaken for an approval."""
    with pytest.raises(InstrumentNormalizationError):
        normalize(spec(), quantity=Decimal("0.0004"))


# --- acceptance 4: above maximum quantity -----------------------------------------


def test_acceptance_4_above_maximum_quantity_is_rejected_not_clipped() -> None:
    """Clipping silently would change the position without re-admission."""
    with pytest.raises(InstrumentNormalizationError) as caught:
        normalize(spec(), quantity=Decimal("2000"))
    assert caught.value.reason is RejectionReason.ABOVE_MAXIMUM_QUANTITY


# --- acceptance 5: instrument status ----------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        InstrumentStatus.PRE_LAUNCH,
        InstrumentStatus.SETTLING,
        InstrumentStatus.DELIVERING,
        InstrumentStatus.CLOSED,
        InstrumentStatus.UNKNOWN,
    ],
)
def test_acceptance_5_non_trading_status_blocks_a_new_entry(status: InstrumentStatus) -> None:
    with pytest.raises(InstrumentNormalizationError) as caught:
        normalize(spec(status=status))
    assert caught.value.reason is RejectionReason.NOT_TRADABLE


def test_acceptance_5_trading_status_is_permitted() -> None:
    assert normalize(spec()).quantity > 0


# --- acceptance 6: stale specification --------------------------------------------


def test_acceptance_6_stale_spec_blocks_new_risk() -> None:
    stale = spec(observed_timestamp=NOW - DEFAULT_MAXIMUM_SPEC_AGE - timedelta(seconds=1))
    with pytest.raises(InstrumentNormalizationError) as caught:
        normalize(stale, observed_at=NOW)
    assert caught.value.reason is RejectionReason.SPEC_STALE


def test_acceptance_6_fresh_spec_is_permitted() -> None:
    fresh = spec(observed_timestamp=NOW - timedelta(minutes=1))
    assert normalize(fresh, observed_at=NOW).quantity > 0


def test_acceptance_6_age_is_measured_against_observation_not_source() -> None:
    instrument = spec(
        source_timestamp=NOW - timedelta(days=5), observed_timestamp=NOW - timedelta(minutes=1)
    )
    assert instrument.is_stale_at(NOW) is False


# --- acceptance 7 foundation: revision identity -----------------------------------


def test_revision_is_stable_across_observation_times() -> None:
    """Two snapshots of unchanged rules must share a revision, or every poll re-admits."""
    first = spec(observed_timestamp=NOW)
    second = spec(observed_timestamp=NOW - timedelta(minutes=5))
    assert first.revision == second.revision


def test_revision_changes_when_a_rule_changes() -> None:
    """A venue that moves a tick size must invalidate what was approved under the old one."""
    assert spec().revision != spec(tick_size=Decimal("0.50")).revision


def test_revision_changes_when_status_changes() -> None:
    assert spec().revision != spec(status=InstrumentStatus.SETTLING).revision


def test_economics_carry_the_revision_that_permitted_them() -> None:
    instrument = spec()
    assert normalize(instrument).spec_revision == instrument.revision


# --- acceptance 9 foundation: fail closed on the unknown --------------------------


def test_acceptance_9_unrecognised_status_fails_closed() -> None:
    """An unmapped venue status must not be read as permission."""
    assert InstrumentStatus.UNKNOWN is not InstrumentStatus.TRADING
    with pytest.raises(InstrumentNormalizationError):
        normalize(spec(status=InstrumentStatus.UNKNOWN))


def test_symbol_mismatch_is_refused() -> None:
    with pytest.raises(InstrumentNormalizationError) as caught:
        normalize(spec(), symbol="ETHUSDT")
    assert caught.value.reason is RejectionReason.SYMBOL_MISMATCH


# --- price range ------------------------------------------------------------------


def test_price_below_venue_minimum_is_rejected() -> None:
    with pytest.raises(InstrumentNormalizationError) as caught:
        normalize(spec(), limit_price=Decimal("0.05"))
    assert caught.value.reason is RejectionReason.PRICE_BELOW_MINIMUM


def test_price_above_venue_maximum_is_rejected() -> None:
    with pytest.raises(InstrumentNormalizationError) as caught:
        normalize(spec(), limit_price=Decimal("2000000"))
    assert caught.value.reason is RejectionReason.PRICE_ABOVE_MAXIMUM


def test_non_positive_inputs_are_rejected() -> None:
    for quantity in (Decimal("0"), Decimal("-1")):
        with pytest.raises(InstrumentNormalizationError) as caught:
            normalize(spec(), quantity=quantity)
        assert caught.value.reason is RejectionReason.NON_POSITIVE_QUANTITY
    with pytest.raises(InstrumentNormalizationError) as caught:
        normalize(spec(), limit_price=Decimal("-1"))
    assert caught.value.reason is RejectionReason.NON_POSITIVE_PRICE


# --- specification validation -----------------------------------------------------


def test_spec_rejects_a_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        spec(observed_timestamp=datetime(2026, 9, 19, 12, 0))


def test_spec_rejects_inverted_bounds() -> None:
    with pytest.raises(ValueError, match="minimum_quantity"):
        spec(minimum_quantity=Decimal("10"), maximum_quantity=Decimal("1"))
    with pytest.raises(ValueError, match="minimum_price"):
        spec(minimum_price=Decimal("100"), maximum_price=Decimal("1"))


def test_spec_rejects_a_lowercase_symbol() -> None:
    with pytest.raises(ValueError, match="uppercase"):
        spec(symbol="btcusdt")


def test_spec_rejects_a_non_positive_tick() -> None:
    with pytest.raises(ValueError, match="tick_size"):
        spec(tick_size=Decimal("0"))
