"""Causal-history regression fixtures for the research-only signal inspector."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tools.inspect_signal_lookahead import (
    CloseBar,
    LookaheadAuditError,
    inspect_lookahead,
)


def _bars(count=8):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        CloseBar(start + timedelta(days=i), Decimal("100") + Decimal(i))
        for i in range(count)
    ]


def _causal(history):
    return tuple(
        "BUY" if i > 0 and history[i].close > history[i - 1].close else "HOLD"
        for i in range(len(history))
    )


def _future_leak(history):
    return tuple(
        "BUY" if i + 1 < len(history)
        and history[i + 1].close > history[i].close else "HOLD"
        for i in range(len(history))
    )


def test_causal_signal_has_no_observed_prefix_difference():
    result = inspect_lookahead(_bars(), _causal)
    assert result["verdict"] == "NO_HORIZON_DIFFERENCES_IN_THIS_SAMPLE"
    assert result["mismatch_count"] == 0
    assert result["evaluations"] == 10
    assert result["signal_observations"] == 8
    assert len(result["dataset_sha256"]) == 64
    assert result["no_lookahead_proven"] is False
    assert result["historical_profitability_proven"] is False
    assert result["live_trading_allowed"] is False
    assert result["strategy_promotion_allowed"] is False


def test_future_bar_leak_is_detected():
    result = inspect_lookahead(_bars(), _future_leak)
    assert result["verdict"] == "HISTORY_HORIZON_SENSITIVITY_DETECTED"
    assert result["mismatch_count"] > 0
    assert result["affected_bar_count"] > 0
    assert result["examples"]
    assert result["examples"][0]["full_signal"] == "BUY"
    assert result["examples"][0]["prefix_signal"] == "HOLD"


def test_warmup_masks_only_preregistered_prefix_bars():
    result = inspect_lookahead(_bars(), _future_leak, warmup_bars=2)
    assert result["warmup_bars"] == 2
    assert result["affected_bar_count"] == 5
    assert result["mismatch_count"] == 5


def test_callback_nondeterminism_rejected():
    calls = [0]

    def changing(history):
        calls[0] += 1
        return ("BUY" if calls[0] % 2 else "SELL",) * len(history)

    with pytest.raises(LookaheadAuditError, match="nondeterministic"):
        inspect_lookahead(_bars(), changing)


def test_mutating_frozen_bar_fails_without_permission_to_modify():
    def tries_to_mutate(history):
        history[0].close = Decimal("999")
        return ("HOLD",) * len(history)

    bars = _bars()
    with pytest.raises(LookaheadAuditError, match="callback failed"):
        inspect_lookahead(bars, tries_to_mutate)
    assert bars[0].close == Decimal("100")


@pytest.mark.parametrize("strategy", [
    lambda history: ["BUY"] * (len(history) - 1),
    lambda history: ["WAIT"] * len(history),
    lambda history: "BUY",
])
def test_invalid_signal_contract_is_rejected(strategy):
    with pytest.raises(LookaheadAuditError, match="signal callback"):
        inspect_lookahead(_bars(), strategy)


@pytest.mark.parametrize("bad", [
    [CloseBar(datetime(2026, 1, 1), Decimal("100"))] * 3,
    _bars(2),
    _bars(513),
    [
        CloseBar(datetime(2026, 1, 3, tzinfo=UTC), Decimal("100")),
        CloseBar(datetime(2026, 1, 2, tzinfo=UTC), Decimal("100")),
        CloseBar(datetime(2026, 1, 1, tzinfo=UTC), Decimal("100")),
    ],
])
def test_malformed_chronology_or_size_rejected(bad):
    with pytest.raises(LookaheadAuditError):
        inspect_lookahead(bad, _causal)


@pytest.mark.parametrize("bad_price", [
    Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), Decimal("0"),
])
def test_invalid_decimal_price_rejected(bad_price):
    bars = _bars()
    bars[2] = CloseBar(bars[2].timestamp, bad_price)
    with pytest.raises(LookaheadAuditError, match="finite positive"):
        inspect_lookahead(bars, _causal)


def test_invalid_warmup_rejected():
    with pytest.raises(LookaheadAuditError, match="invalid warmup"):
        inspect_lookahead(_bars(), _causal, warmup_bars=8)


def test_independent_full_pass_rejects_hidden_horizon_randomness():
    bars = _bars()
    result = inspect_lookahead(bars, _causal)
    assert result["signal_implementation_identity_verified"] is False
    assert result["no_lookahead_proven"] is False
