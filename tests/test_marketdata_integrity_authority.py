from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.continuity import (
    OperationalContinuityCheckpoint,
    continuity_checkpoint_id,
)
from app.marketdata.integrity import (
    MarketDataIntegrityAction,
    MarketDataIntegrityAuthority,
    MarketDataIntegrityPolicy,
    MarketDataIntegrityScope,
    MarketDataIntegrityState,
    assess_market_data_integrity,
)
from app.marketdata.operational import OperationalBar

NOW = datetime(2026, 10, 6, 11, 0, tzinfo=UTC)
SCOPE = MarketDataIntegrityScope(
    provider="BYBIT",
    venue="BYBIT_LINEAR",
    symbol="BTCUSDT",
    interval_seconds=60,
)
POLICY = MarketDataIntegrityPolicy(
    policy_version="test-integrity-v1",
    evaluation_bars=5,
    maximum_age_seconds=Decimal("15"),
    maximum_final_receive_lag_seconds=Decimal("5"),
    maximum_close_jump_fraction=Decimal("0.25"),
)


def bar(
    index: int,
    *,
    close: str | None = None,
    receive_lag_seconds: str = "0.2",
    revision: int = 0,
    open_offset_seconds: int = 0,
) -> OperationalBar:
    close_time = NOW - timedelta(seconds=2) - timedelta(
        seconds=(2 - index) * 60
    )
    open_time = (
        close_time
        - timedelta(seconds=60)
        + timedelta(seconds=open_offset_seconds)
    )
    value = Decimal(close if close is not None else str(100 + index))
    return OperationalBar(
        provider=SCOPE.provider,
        venue=SCOPE.venue,
        symbol=SCOPE.symbol,
        interval_seconds=SCOPE.interval_seconds,
        open_time=open_time,
        close_time=close_time,
        source_timestamp=close_time,
        received_at=close_time + timedelta(
            seconds=float(receive_lag_seconds)
        ),
        source_event_id=f"kline.1.BTCUSDT:{index}",
        is_final=True,
        open=value,
        high=value + Decimal("1"),
        low=value - Decimal("1"),
        close=value,
        volume=Decimal("10"),
        revision=revision,
    )


def bars() -> tuple[OperationalBar, ...]:
    return (bar(0), bar(1), bar(2))


def checkpoint_for(
    value: OperationalBar,
    *,
    provider: str | None = None,
) -> OperationalContinuityCheckpoint:
    selected_provider = SCOPE.provider if provider is None else provider
    checkpoint_id = continuity_checkpoint_id(
        previous_checkpoint_id=None,
        provider=selected_provider,
        venue=SCOPE.venue,
        symbol=SCOPE.symbol,
        interval_seconds=SCOPE.interval_seconds,
        through_bar_id=value.bar_id,
        through_close_time=value.close_time,
        evidence_source="TEST_INTEGRITY",
    )
    return OperationalContinuityCheckpoint(
        checkpoint_id=checkpoint_id,
        previous_checkpoint_id=None,
        provider=selected_provider,
        venue=SCOPE.venue,
        symbol=SCOPE.symbol,
        interval_seconds=SCOPE.interval_seconds,
        through_bar_id=value.bar_id,
        through_close_time=value.close_time,
        established_at=NOW,
        evidence_source="TEST_INTEGRITY",
    )


def assess(
    values: tuple[OperationalBar, ...] | None = None,
    *,
    checkpoint: OperationalContinuityCheckpoint | None | object = object(),
    conflict_count: int = 0,
    policy: MarketDataIntegrityPolicy = POLICY,
    now: datetime = NOW,
):
    selected = bars() if values is None else values
    if checkpoint.__class__ is object:
        resolved_checkpoint = checkpoint_for(selected[-1])
    else:
        resolved_checkpoint = checkpoint
    return assess_market_data_integrity(
        scope=SCOPE,
        policy=policy,
        evaluated_at=now,
        bars=selected,
        checkpoint=resolved_checkpoint,
        conflict_count=conflict_count,
    )


def test_healthy_market_state_is_trusted_and_evidence_is_deterministic() -> None:
    first = assess()
    second = assess()

    assert first.state is MarketDataIntegrityState.HEALTHY
    assert first.action is MarketDataIntegrityAction.TRUST
    assert first.trusted
    assert first.reasons == ()
    assert first.bars_checked == 3
    assert first.market_data_age_seconds == Decimal("2.0")
    assert first.latest_receive_lag_seconds == Decimal("0.2")
    assert len(first.evidence_sha256) == 64
    assert first.evidence_sha256 == second.evidence_sha256


def test_stale_high_water_forces_read_only() -> None:
    stale_now = NOW + timedelta(seconds=20)
    result = assess(now=stale_now)

    assert result.state is MarketDataIntegrityState.STALE
    assert result.action is MarketDataIntegrityAction.READ_ONLY
    assert not result.trusted
    assert result.reasons == ("MARKET_DATA_STALE",)


@pytest.mark.parametrize(
    ("values", "expected_reason", "evaluated_at"),
    [
        (
            (bar(0), bar(1), bar(2, receive_lag_seconds="10")),
            "MARKET_DATA_RECEIVE_LAG_EXCEEDED",
            NOW + timedelta(seconds=10),
        ),
        (
            (bar(0), bar(1), bar(2, revision=1)),
            "MARKET_DATA_REVISION_PRESENT",
            NOW,
        ),
        (
            (bar(0, close="100"), bar(1, close="101"), bar(2, close="150")),
            "MARKET_DATA_PRICE_JUMP_REVIEW_REQUIRED",
            NOW,
        ),
    ],
)
def test_suspicious_but_structurally_valid_state_becomes_read_only(
    values,
    expected_reason,
    evaluated_at,
) -> None:
    result = assess(values, now=evaluated_at)

    assert result.state is MarketDataIntegrityState.DEGRADED
    assert result.action is MarketDataIntegrityAction.READ_ONLY
    assert expected_reason in result.reasons
    assert not result.trusted


def test_durable_conflict_quarantines_the_scope() -> None:
    result = assess(conflict_count=1)

    assert result.state is MarketDataIntegrityState.QUARANTINED
    assert result.action is MarketDataIntegrityAction.HALT_SCOPE
    assert result.reasons == ("MARKET_DATA_CONFLICT_PRESENT",)


def test_gap_quarantines_even_when_each_individual_bar_is_valid() -> None:
    middle = bar(1)
    shifted_middle = replace(
        middle,
        open_time=middle.open_time + timedelta(seconds=1),
        close_time=middle.close_time + timedelta(seconds=1),
        source_timestamp=middle.source_timestamp + timedelta(seconds=1),
        received_at=middle.received_at + timedelta(seconds=1),
    )
    shifted_middle.validate()
    values = (bar(0), shifted_middle, bar(2))
    result = assess(values)

    assert result.state is MarketDataIntegrityState.QUARANTINED
    assert result.action is MarketDataIntegrityAction.HALT_SCOPE
    assert "MARKET_DATA_GAP_PRESENT" in result.reasons


def test_checkpoint_mismatch_quarantines_the_high_water() -> None:
    values = bars()
    result = assess(
        values,
        checkpoint=checkpoint_for(values[-2]),
    )

    assert result.state is MarketDataIntegrityState.QUARANTINED
    assert "MARKET_DATA_CONTINUITY_MISMATCH" in result.reasons


def test_missing_continuity_fails_closed_without_inventing_a_bar_reason() -> None:
    result = assess((), checkpoint=None)

    assert result.state is MarketDataIntegrityState.QUARANTINED
    assert result.reasons == ("MARKET_DATA_CONTINUITY_MISSING",)
    assert result.bars_checked == 0
    assert result.checkpoint_id is None


def test_future_high_water_is_a_clock_conflict_and_halt_scope() -> None:
    value = bar(2)
    future_checkpoint = replace(
        checkpoint_for(value),
        through_close_time=NOW + timedelta(seconds=1),
        established_at=NOW + timedelta(seconds=1),
    )

    result = assess((value,), checkpoint=future_checkpoint)

    assert result.state is MarketDataIntegrityState.QUARANTINED
    assert "MARKET_DATA_CLOCK_CONFLICT" in result.reasons


def test_scope_mismatch_is_never_treated_as_a_healthy_feed() -> None:
    values = bars()
    wrong = replace(values[-1], symbol="ETHUSDT")

    result = assess((*values[:-1], wrong), checkpoint=checkpoint_for(wrong))

    assert result.state is MarketDataIntegrityState.QUARANTINED
    assert "MARKET_DATA_BAR_SCOPE_MISMATCH" in result.reasons


def test_evidence_digest_changes_with_market_content_and_policy() -> None:
    original = assess()
    changed_market = assess(
        (bar(0), bar(1), bar(2, close="103")),
    )
    changed_policy = assess(
        policy=replace(POLICY, policy_version="test-integrity-v2"),
    )

    assert original.evidence_sha256 != changed_market.evidence_sha256
    assert original.evidence_sha256 != changed_policy.evidence_sha256


class MarketStore:
    def __init__(
        self,
        values: tuple[OperationalBar, ...],
        *,
        conflicts: int = 0,
        fail: bool = False,
    ) -> None:
        self.values = values
        self.conflicts = conflicts
        self.fail = fail

    def conflict_count(self) -> int:
        if self.fail:
            raise RuntimeError("market store unavailable")
        return self.conflicts

    def recent_bars(self, **kwargs):
        assert kwargs["provider"] == SCOPE.provider
        assert kwargs["venue"] == SCOPE.venue
        assert kwargs["symbol"] == SCOPE.symbol
        assert kwargs["interval_seconds"] == SCOPE.interval_seconds
        assert kwargs["limit"] == POLICY.evaluation_bars
        return self.values[-kwargs["limit"] :]


class ContinuityStore:
    def __init__(
        self,
        checkpoint: OperationalContinuityCheckpoint | None,
        *,
        fail: bool = False,
    ) -> None:
        self.checkpoint = checkpoint
        self.fail = fail

    def latest(self, **kwargs):
        if self.fail:
            raise RuntimeError("continuity store unavailable")
        assert kwargs == {
            "provider": SCOPE.provider,
            "venue": SCOPE.venue,
            "symbol": SCOPE.symbol,
            "interval_seconds": SCOPE.interval_seconds,
        }
        return self.checkpoint


def authority(
    *,
    market_fail: bool = False,
    continuity_fail: bool = False,
) -> MarketDataIntegrityAuthority:
    values = bars()
    return MarketDataIntegrityAuthority(
        scope=SCOPE,
        marketdata=MarketStore(values, fail=market_fail),
        continuity=ContinuityStore(
            checkpoint_for(values[-1]),
            fail=continuity_fail,
        ),
        policy=POLICY,
    )


def test_authority_reads_canonical_stores_and_returns_healthy_evidence() -> None:
    result = authority().evaluate(now=NOW)

    assert result.state is MarketDataIntegrityState.HEALTHY
    assert result.trusted
    assert result.through_bar_id == bars()[-1].bar_id


@pytest.mark.parametrize(
    ("market_fail", "continuity_fail"),
    [(True, False), (False, True)],
)
def test_authority_unavailability_is_fail_closed(
    market_fail,
    continuity_fail,
) -> None:
    result = authority(
        market_fail=market_fail,
        continuity_fail=continuity_fail,
    ).evaluate(now=NOW)

    assert result.state is MarketDataIntegrityState.QUARANTINED
    assert result.action is MarketDataIntegrityAction.HALT_SCOPE
    assert result.reasons == ("MARKET_DATA_AUTHORITY_UNAVAILABLE",)
    assert result.conflict_count == 1


def test_integrity_policy_rejects_invalid_thresholds() -> None:
    with pytest.raises(ValueError, match="evaluation_bars"):
        replace(POLICY, evaluation_bars=0).validate()
    with pytest.raises(ValueError, match="maximum_age_seconds"):
        replace(POLICY, maximum_age_seconds=Decimal("-1")).validate()
    with pytest.raises(ValueError, match="policy_version"):
        replace(POLICY, policy_version=" ").validate()
