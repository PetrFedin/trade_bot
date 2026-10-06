from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from app.domain.instrument import InstrumentSpec
from app.marketdata.bybit_repair import (
    BYBIT_LINEAR_VENUE,
    BYBIT_PROVIDER,
    BybitKlineRangeCapture,
)
from app.marketdata.continuity import OperationalContinuityCheckpoint
from app.marketdata.operational import OperationalBar
from app.qualification.replay_evidence import (
    ReplayContinuity,
    ReplayDatasetEvidence,
    ReplayEventEvidence,
    ReplayEvidenceProfile,
)

_BYBIT_PROVIDER_REPLAY_SCHEMA = "astra-bybit-provider-replay-v1"


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _canonical_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _strategy_input_payload(bar: OperationalBar, *, strategy_id: str) -> dict[str, object]:
    strategy_bar = bar.strategy_bar()
    return {
        "strategy_id": strategy_id,
        "source_bar_id": bar.bar_id,
        "symbol": strategy_bar.symbol,
        "timestamp": _aware(strategy_bar.timestamp, "strategy_bar.timestamp").isoformat(),
        "close": _canonical_decimal(strategy_bar.close),
    }


def _risk_market_input_payload(
    bar: OperationalBar,
    *,
    instrument_spec_revision: str,
) -> dict[str, object]:
    return {
        "source_bar_id": bar.bar_id,
        "symbol": bar.symbol,
        "market_timestamp": _aware(bar.close_time, "bar.close_time").isoformat(),
        "reference_price": _canonical_decimal(bar.close),
        "instrument_spec_revision": instrument_spec_revision,
    }


@dataclass(frozen=True)
class BybitProviderReplayBinding:
    sequence: int
    source_event_id: str
    bar_id: str
    raw_row_sha256: str
    normalized_event_sha256: str
    strategy_input_sha256: str
    risk_market_input_sha256: str
    replay_event_digest: str

    def payload(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "source_event_id": self.source_event_id,
            "bar_id": self.bar_id,
            "raw_row_sha256": self.raw_row_sha256,
            "normalized_event_sha256": self.normalized_event_sha256,
            "strategy_input_sha256": self.strategy_input_sha256,
            "risk_market_input_sha256": self.risk_market_input_sha256,
            "replay_event_digest": self.replay_event_digest,
        }


@dataclass(frozen=True)
class BybitProviderReplayEvidence:
    replay: ReplayDatasetEvidence
    request_url: str
    raw_response_sha256: str
    response_received_at: datetime
    exchange_server_at: datetime
    instrument_spec_revision: str
    instrument_source_timestamp: datetime
    instrument_observed_timestamp: datetime
    continuity_checkpoint_id: str
    continuity_through_bar_id: str
    bindings: tuple[BybitProviderReplayBinding, ...]
    schema_version: str = _BYBIT_PROVIDER_REPLAY_SCHEMA

    def validate(self) -> None:
        if self.schema_version != _BYBIT_PROVIDER_REPLAY_SCHEMA:
            raise ValueError("Bybit provider replay schema mismatch")
        self.replay.validate()
        if self.replay.profile is not ReplayEvidenceProfile.PROVIDER_COMPLETE:
            raise ValueError("Bybit provider replay requires PROVIDER_COMPLETE evidence")
        if self.replay.limitations:
            raise ValueError("provider-complete Bybit replay cannot declare limitations")
        if not self.request_url.startswith("https://api.bybit.com/v5/market/kline?"):
            raise ValueError("unexpected Bybit provider replay request URL")
        if len(self.raw_response_sha256) != 64:
            raise ValueError("raw_response_sha256 must be a sha256 digest")
        if len(self.instrument_spec_revision) != 64:
            raise ValueError("instrument_spec_revision must be a sha256 digest")
        _aware(self.response_received_at, "response_received_at")
        _aware(self.exchange_server_at, "exchange_server_at")
        _aware(self.instrument_source_timestamp, "instrument_source_timestamp")
        _aware(self.instrument_observed_timestamp, "instrument_observed_timestamp")
        if not self.continuity_checkpoint_id.strip():
            raise ValueError("continuity_checkpoint_id is required")
        if not self.continuity_through_bar_id.strip():
            raise ValueError("continuity_through_bar_id is required")
        if len(self.bindings) != len(self.replay.events):
            raise ValueError("Bybit replay binding count mismatch")
        for binding, event in zip(self.bindings, self.replay.events, strict=True):
            if binding.sequence != event.sequence:
                raise ValueError("Bybit replay binding sequence mismatch")
            if binding.source_event_id != event.source_event_id:
                raise ValueError("Bybit replay binding source identity mismatch")
            if binding.raw_row_sha256 != event.raw_event_sha256:
                raise ValueError("Bybit replay raw-event digest mismatch")
            if binding.normalized_event_sha256 != event.normalized_event_sha256:
                raise ValueError("Bybit replay normalized-event digest mismatch")
            if binding.replay_event_digest != event.digest:
                raise ValueError("Bybit replay event digest mismatch")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "replay_evidence_sha256": self.replay.evidence_sha256,
            "dataset_id": self.replay.dataset_id,
            "dataset_sha256": self.replay.dataset_sha256,
            "request_url": self.request_url,
            "raw_response_sha256": self.raw_response_sha256,
            "response_received_at": _aware(
                self.response_received_at,
                "response_received_at",
            ).isoformat(),
            "exchange_server_at": _aware(
                self.exchange_server_at,
                "exchange_server_at",
            ).isoformat(),
            "instrument_spec_revision": self.instrument_spec_revision,
            "instrument_source_timestamp": _aware(
                self.instrument_source_timestamp,
                "instrument_source_timestamp",
            ).isoformat(),
            "instrument_observed_timestamp": _aware(
                self.instrument_observed_timestamp,
                "instrument_observed_timestamp",
            ).isoformat(),
            "continuity_checkpoint_id": self.continuity_checkpoint_id,
            "continuity_through_bar_id": self.continuity_through_bar_id,
            "bindings": [binding.payload() for binding in self.bindings],
        }

    @property
    def evidence_sha256(self) -> str:
        return _sha256(self.payload())


def build_bybit_provider_complete_replay(
    *,
    capture: BybitKlineRangeCapture,
    instrument_spec: InstrumentSpec,
    continuity_checkpoint: OperationalContinuityCheckpoint,
    strategy_id: str,
) -> BybitProviderReplayEvidence:
    """Bind exact Bybit provider bytes to normalized and downstream replay inputs."""

    capture.validate()
    instrument_spec.validate()
    continuity_checkpoint.validate()
    if not strategy_id.strip():
        raise ValueError("strategy_id is required")

    bars = capture.bars
    symbol = bars[0].symbol
    interval_seconds = bars[0].interval_seconds
    if any(bar.provider != BYBIT_PROVIDER for bar in bars):
        raise ValueError("Bybit provider replay contains a non-Bybit provider")
    if any(bar.venue != BYBIT_LINEAR_VENUE for bar in bars):
        raise ValueError("Bybit provider replay contains an unexpected venue")
    if any(bar.symbol != symbol for bar in bars):
        raise ValueError("Bybit provider replay cannot mix symbols")
    if any(bar.interval_seconds != interval_seconds for bar in bars):
        raise ValueError("Bybit provider replay cannot mix intervals")
    if instrument_spec.symbol != symbol:
        raise ValueError("instrument specification symbol mismatch")
    if instrument_spec.venue.lower() != "bybit":
        raise ValueError("instrument specification is not a Bybit specification")
    if continuity_checkpoint.provider != BYBIT_PROVIDER:
        raise ValueError("continuity checkpoint provider mismatch")
    if continuity_checkpoint.venue != BYBIT_LINEAR_VENUE:
        raise ValueError("continuity checkpoint venue mismatch")
    if continuity_checkpoint.symbol != symbol:
        raise ValueError("continuity checkpoint symbol mismatch")
    if continuity_checkpoint.interval_seconds != interval_seconds:
        raise ValueError("continuity checkpoint interval mismatch")
    if continuity_checkpoint.through_bar_id != bars[-1].bar_id:
        raise ValueError("continuity checkpoint does not prove the captured high-water bar")
    if continuity_checkpoint.through_close_time != bars[-1].close_time:
        raise ValueError("continuity checkpoint close time mismatch")

    raw_row_sha256 = capture.raw_row_sha256
    dataset_material = {
        "provider": BYBIT_PROVIDER,
        "venue": BYBIT_LINEAR_VENUE,
        "symbol": symbol,
        "interval_seconds": interval_seconds,
        "request_url": capture.request_url,
        "raw_response_sha256": capture.response_sha256,
        "instrument_spec_revision": instrument_spec.revision,
        "normalized_event_sha256": [bar.content_hash for bar in bars],
    }
    dataset_sha256 = _sha256(dataset_material)
    dataset_id = (
        f"BYBIT:{symbol}:{bars[0].open_time.isoformat()}:"
        f"{bars[-1].close_time.isoformat()}:{dataset_sha256[:16]}"
    )

    replay_events: list[ReplayEventEvidence] = []
    bindings: list[BybitProviderReplayBinding] = []
    for index, (bar, raw_sha) in enumerate(
        zip(bars, raw_row_sha256, strict=True),
        start=1,
    ):
        event = ReplayEventEvidence(
            sequence=index,
            dataset_id=dataset_id,
            symbol=symbol,
            exchange_timestamp=bar.source_timestamp,
            receive_timestamp=capture.response_received_at,
            source_event_id=bar.source_event_id,
            raw_event_sha256=raw_sha,
            normalized_event_sha256=bar.content_hash,
            instrument_spec_revision=instrument_spec.revision,
            continuity=(
                ReplayContinuity.ROOT
                if index == 1
                else ReplayContinuity.CONTIGUOUS
            ),
        )
        strategy_input_sha256 = _sha256(
            _strategy_input_payload(bar, strategy_id=strategy_id)
        )
        risk_market_input_sha256 = _sha256(
            _risk_market_input_payload(
                bar,
                instrument_spec_revision=instrument_spec.revision,
            )
        )
        replay_events.append(event)
        bindings.append(
            BybitProviderReplayBinding(
                sequence=index,
                source_event_id=bar.source_event_id,
                bar_id=bar.bar_id,
                raw_row_sha256=raw_sha,
                normalized_event_sha256=bar.content_hash,
                strategy_input_sha256=strategy_input_sha256,
                risk_market_input_sha256=risk_market_input_sha256,
                replay_event_digest=event.digest,
            )
        )

    replay = ReplayDatasetEvidence(
        dataset_id=dataset_id,
        dataset_sha256=dataset_sha256,
        source_name="BYBIT_V5_MARKET_KLINE_GET",
        source_schema_version="bybit-v5-kline-rest-v1",
        events=tuple(replay_events),
        profile=ReplayEvidenceProfile.PROVIDER_COMPLETE,
        limitations=(),
    )
    evidence = BybitProviderReplayEvidence(
        replay=replay,
        request_url=capture.request_url,
        raw_response_sha256=capture.response_sha256,
        response_received_at=capture.response_received_at,
        exchange_server_at=capture.server_at,
        instrument_spec_revision=instrument_spec.revision,
        instrument_source_timestamp=instrument_spec.source_timestamp,
        instrument_observed_timestamp=instrument_spec.observed_timestamp,
        continuity_checkpoint_id=continuity_checkpoint.checkpoint_id,
        continuity_through_bar_id=continuity_checkpoint.through_bar_id,
        bindings=tuple(bindings),
    )
    evidence.validate()
    return evidence
