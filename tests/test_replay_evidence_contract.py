from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.trading import Bar
from app.marketdata.historical import HistoricalDataset, canonical_dataset_sha256
from app.qualification.replay_evidence import (
    ReplayContinuity,
    ReplayDatasetEvidence,
    ReplayEventEvidence,
    ReplayEvidenceProfile,
    build_historical_replay_evidence,
)

START = datetime(2026, 10, 6, 9, 0, tzinfo=UTC)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def dataset(
    *,
    closes: tuple[str, ...] = ("100", "101", "102"),
    offsets: tuple[int, ...] = (0, 1, 2),
) -> HistoricalDataset:
    bars = tuple(
        Bar(
            symbol="BTCUSDT",
            timestamp=START + timedelta(minutes=offset),
            close=Decimal(close),
        )
        for close, offset in zip(closes, offsets, strict=True)
    )
    digest = canonical_dataset_sha256(bars)
    return HistoricalDataset(
        dataset_id=f"BTCUSDT:test:{digest[:16]}",
        symbol="BTCUSDT",
        bars=bars,
        canonical_sha256=digest,
        source_name="fixture.csv",
    )


def test_historical_replay_evidence_is_deterministic_and_truthful_about_missing_source_facts():
    value = dataset()

    first = build_historical_replay_evidence(
        value,
        expected_interval=timedelta(minutes=1),
        instrument_spec_revision=_sha("spec-v1"),
    )
    second = build_historical_replay_evidence(
        value,
        expected_interval=timedelta(minutes=1),
        instrument_spec_revision=_sha("spec-v1"),
    )

    assert first == second
    assert first.evidence_sha256 == second.evidence_sha256
    assert first.profile is ReplayEvidenceProfile.NORMALIZED_ONLY
    assert first.limitations == (
        "RAW_PROVIDER_EVENT_UNAVAILABLE",
        "SOURCE_RECEIVE_TIMESTAMP_UNAVAILABLE",
    )
    assert [event.continuity for event in first.events] == [
        ReplayContinuity.ROOT,
        ReplayContinuity.CONTIGUOUS,
        ReplayContinuity.CONTIGUOUS,
    ]
    assert all(event.receive_timestamp is None for event in first.events)
    assert all(event.raw_event_sha256 is None for event in first.events)


def test_gap_and_unspecified_interval_are_explicit_evidence_not_silent_assumptions():
    gapped = build_historical_replay_evidence(
        dataset(offsets=(0, 1, 3)),
        expected_interval=timedelta(minutes=1),
    )
    assert gapped.events[-1].continuity is ReplayContinuity.GAP
    assert gapped.events[-1].gap_seconds == Decimal("60")
    assert "CONTINUITY_GAP_PRESENT" in gapped.limitations
    assert "INSTRUMENT_SPEC_REVISION_UNAVAILABLE" in gapped.limitations

    unknown = build_historical_replay_evidence(dataset())
    assert unknown.events[1].continuity is ReplayContinuity.UNKNOWN
    assert "CONTINUITY_INTERVAL_UNSPECIFIED" in unknown.limitations


def test_evidence_digest_changes_when_normalized_market_data_changes():
    first = build_historical_replay_evidence(
        dataset(closes=("100", "101", "102")),
        expected_interval=timedelta(minutes=1),
    )
    changed = build_historical_replay_evidence(
        dataset(closes=("100", "101.01", "102")),
        expected_interval=timedelta(minutes=1),
    )

    assert first.dataset_sha256 != changed.dataset_sha256
    assert first.events[1].normalized_event_sha256 != changed.events[1].normalized_event_sha256
    assert first.evidence_sha256 != changed.evidence_sha256


def test_builder_rejects_dataset_identity_that_no_longer_matches_its_bars():
    value = dataset()
    tampered = HistoricalDataset(
        dataset_id=value.dataset_id,
        symbol=value.symbol,
        bars=value.bars,
        canonical_sha256="0" * 64,
        source_name=value.source_name,
    )

    with pytest.raises(ValueError, match="HISTORICAL_DATASET_DIGEST_MISMATCH"):
        build_historical_replay_evidence(tampered)


def test_provider_complete_profile_refuses_missing_raw_receive_or_spec_evidence():
    normalized = build_historical_replay_evidence(
        dataset(),
        expected_interval=timedelta(minutes=1),
    )

    invalid = ReplayDatasetEvidence(
        dataset_id=normalized.dataset_id,
        dataset_sha256=normalized.dataset_sha256,
        source_name=normalized.source_name,
        source_schema_version=normalized.source_schema_version,
        events=normalized.events,
        profile=ReplayEvidenceProfile.PROVIDER_COMPLETE,
        limitations=(),
    )

    with pytest.raises(ValueError, match="provider-complete evidence requires"):
        invalid.validate()


def test_provider_complete_profile_accepts_only_explicit_source_evidence():
    event = ReplayEventEvidence(
        sequence=1,
        dataset_id="provider-dataset-1",
        symbol="BTCUSDT",
        exchange_timestamp=START,
        receive_timestamp=START + timedelta(milliseconds=10),
        source_event_id="kline.1.BTCUSDT:1",
        raw_event_sha256=_sha("raw-provider-payload"),
        normalized_event_sha256=_sha("normalized-event"),
        instrument_spec_revision=_sha("instrument-spec"),
        continuity=ReplayContinuity.ROOT,
    )
    evidence = ReplayDatasetEvidence(
        dataset_id="provider-dataset-1",
        dataset_sha256=_sha("provider-dataset"),
        source_name="BYBIT",
        source_schema_version="provider-event-v1",
        events=(event,),
        profile=ReplayEvidenceProfile.PROVIDER_COMPLETE,
        limitations=(),
    )

    evidence.validate()
    assert len(event.digest) == 64
    assert len(evidence.evidence_sha256) == 64
    assert evidence.payload()["profile"] == "PROVIDER_COMPLETE"


def provider_event(**changes) -> ReplayEventEvidence:
    base = ReplayEventEvidence(
        sequence=1,
        dataset_id="provider-dataset-1",
        symbol="BTCUSDT",
        exchange_timestamp=START,
        receive_timestamp=START + timedelta(milliseconds=10),
        source_event_id="kline.1.BTCUSDT:1",
        raw_event_sha256=_sha("raw-provider-payload"),
        normalized_event_sha256=_sha("normalized-event"),
        instrument_spec_revision=_sha("instrument-spec"),
        continuity=ReplayContinuity.ROOT,
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"sequence": 0}, "sequence must be positive"),
        ({"dataset_id": " "}, "dataset_id is required"),
        ({"symbol": "btcusdt"}, "uppercase"),
        ({"exchange_timestamp": datetime(2026, 10, 6, 9, 0)}, "timezone-aware"),
        ({"receive_timestamp": datetime(2026, 10, 6, 9, 0)}, "timezone-aware"),
        ({"source_event_id": " "}, "cannot be blank"),
        ({"raw_event_sha256": "bad"}, "sha256"),
        ({"normalized_event_sha256": "bad"}, "sha256"),
        ({"instrument_spec_revision": "bad"}, "sha256"),
        ({"sequence": 2}, "ROOT continuity"),
        ({"continuity": ReplayContinuity.GAP}, "positive finite gap_seconds"),
        (
            {
                "continuity": ReplayContinuity.CONTIGUOUS,
                "gap_seconds": Decimal("1"),
            },
            "valid only for GAP",
        ),
    ],
)
def test_replay_event_fail_closed_validation(changes, match) -> None:
    with pytest.raises(ValueError, match=match):
        provider_event(**changes).validate()


def provider_dataset(**changes) -> ReplayDatasetEvidence:
    base = ReplayDatasetEvidence(
        dataset_id="provider-dataset-1",
        dataset_sha256=_sha("provider-dataset"),
        source_name="BYBIT",
        source_schema_version="provider-event-v1",
        events=(provider_event(),),
        profile=ReplayEvidenceProfile.PROVIDER_COMPLETE,
        limitations=(),
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"schema_version": "wrong"}, "schema mismatch"),
        ({"dataset_id": " "}, "dataset_id is required"),
        ({"dataset_sha256": "bad"}, "sha256"),
        ({"source_name": " "}, "source_name is required"),
        ({"source_schema_version": " "}, "source_schema_version is required"),
        ({"events": ()}, "at least one event"),
        (
            {"limitations": ("DUP", "DUP")},
            "limitations must be unique",
        ),
        (
            {"limitations": (" ",)},
            "limitations cannot be blank",
        ),
        (
            {
                "events": (
                    provider_event(dataset_id="different"),
                )
            },
            "dataset identity mismatch",
        ),
        (
            {"limitations": ("NOT_ALLOWED",)},
            "cannot declare limitations",
        ),
    ],
)
def test_replay_dataset_fail_closed_validation(changes, match) -> None:
    with pytest.raises(ValueError, match=match):
        provider_dataset(**changes).validate()


def test_replay_dataset_rejects_mixed_symbols_and_non_root_first_event() -> None:
    second = replace(
        provider_event(),
        sequence=2,
        symbol="ETHUSDT",
        continuity=ReplayContinuity.CONTIGUOUS,
        source_event_id="kline.1.ETHUSDT:2",
    )
    mixed = provider_dataset(events=(provider_event(), second))
    with pytest.raises(ValueError, match="cannot mix symbols"):
        mixed.validate()

    non_root = provider_dataset(
        events=(
            replace(
                provider_event(),
                continuity=ReplayContinuity.CONTIGUOUS,
            ),
        )
    )
    with pytest.raises(ValueError, match="first replay event must be ROOT"):
        non_root.validate()


def test_normalized_only_profile_requires_explicit_limitations() -> None:
    evidence = provider_dataset(
        profile=ReplayEvidenceProfile.NORMALIZED_ONLY,
        limitations=(),
    )
    with pytest.raises(ValueError, match="must declare its limitations"):
        evidence.validate()


def test_historical_builder_rejects_invalid_interval_and_spec_digest() -> None:
    with pytest.raises(ValueError, match="expected_interval must be positive"):
        build_historical_replay_evidence(
            dataset(),
            expected_interval=timedelta(0),
        )
    with pytest.raises(ValueError, match="instrument_spec_revision must be a sha256"):
        build_historical_replay_evidence(
            dataset(),
            instrument_spec_revision="bad",
        )


def test_historical_builder_marks_irregular_short_interval() -> None:
    evidence = build_historical_replay_evidence(
        dataset(offsets=(0, 2, 3)),
        expected_interval=timedelta(minutes=2),
    )
    assert evidence.events[-1].continuity is ReplayContinuity.IRREGULAR
    assert "CONTINUITY_IRREGULAR_INTERVAL" in evidence.limitations
