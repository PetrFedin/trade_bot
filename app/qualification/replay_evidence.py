from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.marketdata.historical import HistoricalDataset, canonical_dataset_sha256

_REPLAY_EVIDENCE_SCHEMA = "astra-replay-evidence-v1"


class ReplayEvidenceProfile(StrEnum):
    NORMALIZED_ONLY = "NORMALIZED_ONLY"
    PROVIDER_COMPLETE = "PROVIDER_COMPLETE"


class ReplayContinuity(StrEnum):
    ROOT = "ROOT"
    CONTIGUOUS = "CONTIGUOUS"
    GAP = "GAP"
    IRREGULAR = "IRREGULAR"
    UNKNOWN = "UNKNOWN"


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _hex_digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized


def _canonical_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True)
class ReplayEventEvidence:
    sequence: int
    dataset_id: str
    symbol: str
    exchange_timestamp: datetime
    receive_timestamp: datetime | None
    source_event_id: str | None
    raw_event_sha256: str | None
    normalized_event_sha256: str
    instrument_spec_revision: str | None
    continuity: ReplayContinuity
    gap_seconds: Decimal | None = None

    def validate(self) -> None:
        if self.sequence < 1:
            raise ValueError("sequence must be positive")
        if not self.dataset_id.strip():
            raise ValueError("dataset_id is required")
        if not self.symbol or self.symbol != self.symbol.upper():
            raise ValueError("symbol must be non-empty uppercase")
        _aware_utc(self.exchange_timestamp, "exchange_timestamp")
        if self.receive_timestamp is not None:
            _aware_utc(self.receive_timestamp, "receive_timestamp")
        if self.source_event_id is not None and not self.source_event_id.strip():
            raise ValueError("source_event_id cannot be blank")
        if self.raw_event_sha256 is not None:
            _hex_digest(self.raw_event_sha256, "raw_event_sha256")
        _hex_digest(self.normalized_event_sha256, "normalized_event_sha256")
        if self.instrument_spec_revision is not None:
            _hex_digest(self.instrument_spec_revision, "instrument_spec_revision")
        if self.continuity is ReplayContinuity.ROOT:
            if self.sequence != 1 or self.gap_seconds is not None:
                raise ValueError("ROOT continuity is valid only for the first event")
        elif self.continuity is ReplayContinuity.GAP:
            if (
                self.gap_seconds is None
                or not self.gap_seconds.is_finite()
                or self.gap_seconds <= 0
            ):
                raise ValueError("GAP continuity requires positive finite gap_seconds")
        elif self.gap_seconds is not None:
            raise ValueError("gap_seconds is valid only for GAP continuity")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "sequence": self.sequence,
            "dataset_id": self.dataset_id,
            "symbol": self.symbol,
            "exchange_timestamp": _aware_utc(
                self.exchange_timestamp, "exchange_timestamp"
            ).isoformat(),
            "receive_timestamp": (
                None
                if self.receive_timestamp is None
                else _aware_utc(self.receive_timestamp, "receive_timestamp").isoformat()
            ),
            "source_event_id": self.source_event_id,
            "raw_event_sha256": self.raw_event_sha256,
            "normalized_event_sha256": self.normalized_event_sha256,
            "instrument_spec_revision": self.instrument_spec_revision,
            "continuity": self.continuity.value,
            "gap_seconds": (
                None if self.gap_seconds is None else _canonical_decimal(self.gap_seconds)
            ),
        }

    @property
    def digest(self) -> str:
        return _sha256_bytes(_canonical_json(self.payload()).encode("utf-8"))


@dataclass(frozen=True)
class ReplayDatasetEvidence:
    dataset_id: str
    dataset_sha256: str
    source_name: str
    source_schema_version: str
    events: tuple[ReplayEventEvidence, ...]
    profile: ReplayEvidenceProfile
    limitations: tuple[str, ...]
    schema_version: str = _REPLAY_EVIDENCE_SCHEMA

    def validate(self) -> None:
        if self.schema_version != _REPLAY_EVIDENCE_SCHEMA:
            raise ValueError("replay evidence schema mismatch")
        if not self.dataset_id.strip():
            raise ValueError("dataset_id is required")
        _hex_digest(self.dataset_sha256, "dataset_sha256")
        if not self.source_name.strip():
            raise ValueError("source_name is required")
        if not self.source_schema_version.strip():
            raise ValueError("source_schema_version is required")
        if not self.events:
            raise ValueError("replay evidence requires at least one event")
        if len(set(self.limitations)) != len(self.limitations):
            raise ValueError("replay evidence limitations must be unique")
        if any(not item.strip() for item in self.limitations):
            raise ValueError("replay evidence limitations cannot be blank")

        symbol: str | None = None
        for expected_sequence, event in enumerate(self.events, start=1):
            event.validate()
            if event.sequence != expected_sequence:
                raise ValueError("replay event sequence must be contiguous")
            if event.dataset_id != self.dataset_id:
                raise ValueError("replay event dataset identity mismatch")
            if symbol is None:
                symbol = event.symbol
            elif event.symbol != symbol:
                raise ValueError("replay evidence cannot mix symbols")

        if self.events[0].continuity is not ReplayContinuity.ROOT:
            raise ValueError("first replay event must be ROOT")

        if self.profile is ReplayEvidenceProfile.PROVIDER_COMPLETE:
            if self.limitations:
                raise ValueError("provider-complete evidence cannot declare limitations")
            for event in self.events:
                if (
                    event.receive_timestamp is None
                    or event.source_event_id is None
                    or event.raw_event_sha256 is None
                    or event.instrument_spec_revision is None
                ):
                    raise ValueError(
                        "provider-complete evidence requires receive time, raw event, "
                        "source event id and instrument spec revision"
                    )
        elif not self.limitations:
            raise ValueError("normalized-only evidence must declare its limitations")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "dataset_sha256": self.dataset_sha256,
            "source_name": self.source_name,
            "source_schema_version": self.source_schema_version,
            "profile": self.profile.value,
            "limitations": list(self.limitations),
            "events": [event.payload() for event in self.events],
        }

    @property
    def evidence_sha256(self) -> str:
        return _sha256_bytes(_canonical_json(self.payload()).encode("utf-8"))


def _normalized_bar_sha256(
    *,
    dataset: HistoricalDataset,
    timestamp: datetime,
    close: Decimal,
) -> str:
    payload = {
        "schema_version": dataset.schema_version,
        "dataset_id": dataset.dataset_id,
        "symbol": dataset.symbol,
        "exchange_timestamp": _aware_utc(timestamp, "timestamp").isoformat(),
        "close": _canonical_decimal(close),
    }
    return _sha256_bytes(_canonical_json(payload).encode("utf-8"))


def build_historical_replay_evidence(
    dataset: HistoricalDataset,
    *,
    expected_interval: timedelta | None = None,
    instrument_spec_revision: str | None = None,
) -> ReplayDatasetEvidence:
    """Bind a normalized historical dataset to deterministic replay evidence.

    Current HistoricalDataset objects do not contain raw provider payloads or their original
    receive timestamps. This builder therefore marks that evidence as NORMALIZED_ONLY instead
    of inventing fields that are not present in the source. A future provider-complete replay
    source must supply those facts explicitly.
    """

    if not dataset.bars:
        raise ValueError("historical replay dataset is empty")
    if canonical_dataset_sha256(dataset.bars) != dataset.canonical_sha256:
        raise ValueError("HISTORICAL_DATASET_DIGEST_MISMATCH")
    if expected_interval is not None and expected_interval <= timedelta(0):
        raise ValueError("expected_interval must be positive")
    if instrument_spec_revision is not None:
        instrument_spec_revision = _hex_digest(
            instrument_spec_revision, "instrument_spec_revision"
        )

    events: list[ReplayEventEvidence] = []
    limitations = {
        "RAW_PROVIDER_EVENT_UNAVAILABLE",
        "SOURCE_RECEIVE_TIMESTAMP_UNAVAILABLE",
    }
    if instrument_spec_revision is None:
        limitations.add("INSTRUMENT_SPEC_REVISION_UNAVAILABLE")
    if expected_interval is None:
        limitations.add("CONTINUITY_INTERVAL_UNSPECIFIED")

    previous_timestamp: datetime | None = None
    for sequence, bar in enumerate(dataset.bars, start=1):
        bar.validate()
        current = _aware_utc(bar.timestamp, "bar.timestamp")
        continuity = ReplayContinuity.ROOT
        gap_seconds: Decimal | None = None
        if previous_timestamp is not None:
            if expected_interval is None:
                continuity = ReplayContinuity.UNKNOWN
            else:
                delta = current - previous_timestamp
                if delta == expected_interval:
                    continuity = ReplayContinuity.CONTIGUOUS
                elif delta > expected_interval:
                    continuity = ReplayContinuity.GAP
                    gap_seconds = Decimal(str((delta - expected_interval).total_seconds()))
                    limitations.add("CONTINUITY_GAP_PRESENT")
                else:
                    continuity = ReplayContinuity.IRREGULAR
                    limitations.add("CONTINUITY_IRREGULAR_INTERVAL")
        events.append(
            ReplayEventEvidence(
                sequence=sequence,
                dataset_id=dataset.dataset_id,
                symbol=bar.symbol,
                exchange_timestamp=current,
                receive_timestamp=None,
                source_event_id=None,
                raw_event_sha256=None,
                normalized_event_sha256=_normalized_bar_sha256(
                    dataset=dataset,
                    timestamp=current,
                    close=bar.close,
                ),
                instrument_spec_revision=instrument_spec_revision,
                continuity=continuity,
                gap_seconds=gap_seconds,
            )
        )
        previous_timestamp = current

    evidence = ReplayDatasetEvidence(
        dataset_id=dataset.dataset_id,
        dataset_sha256=dataset.canonical_sha256,
        source_name=dataset.source_name,
        source_schema_version=dataset.schema_version,
        events=tuple(events),
        profile=ReplayEvidenceProfile.NORMALIZED_ONLY,
        limitations=tuple(sorted(limitations)),
    )
    evidence.validate()
    return evidence
