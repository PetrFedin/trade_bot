from __future__ import annotations

from dataclasses import dataclass

from app.domain.instrument import InstrumentSpec, InstrumentStatus
from app.marketdata.bybit_public import BybitPublicLinearSubscription
from app.marketdata.bybit_repair import (
    BYBIT_LINEAR_VENUE,
    BYBIT_PROVIDER,
    BybitKlineRangeCapture,
)
from app.marketdata.continuity import OperationalContinuityCheckpoint
from app.qualification.adapter_conformance import (
    AdapterConformanceCheck,
    AdapterConformanceResult,
    evidence_sha256,
)
from app.qualification.bybit_provider_replay import BybitProviderReplayEvidence

_PROFILE_ID = "ASTRA_BYBIT_PUBLIC_MARKETDATA"
_PROFILE_VERSION = "1.0.0"
_SCOPE = "PUBLIC_MARKET_DATA_ONLY"
_LIMITATIONS = (
    "NO_ORDER_SUBMIT_CONFORMANCE",
    "NO_ORDER_STATUS_CONFORMANCE",
    "NO_FILL_MAPPING_CONFORMANCE",
    "NO_CANCEL_REPLACE_CONFORMANCE",
    "NO_PRIVATE_ACCOUNT_CONFORMANCE",
)


@dataclass(frozen=True)
class BybitPublicAdapterSubject:
    adapter_version: str
    environment: str = "mainnet"

    def validate(self) -> None:
        if not self.adapter_version.strip():
            raise ValueError("adapter_version is required")
        if not self.environment.strip():
            raise ValueError("environment is required")


def _check(check_id: str, passed: bool, evidence: object, reason: str) -> AdapterConformanceCheck:
    return AdapterConformanceCheck(
        check_id=check_id,
        passed=passed,
        evidence_sha256=evidence_sha256(evidence),
        reason=None if passed else reason,
    )


def qualify_bybit_public_marketdata_adapter(
    *,
    subject: BybitPublicAdapterSubject,
    subscription: BybitPublicLinearSubscription,
    instrument_spec: InstrumentSpec,
    capture: BybitKlineRangeCapture,
    continuity_checkpoint: OperationalContinuityCheckpoint,
    replay_evidence: BybitProviderReplayEvidence,
) -> AdapterConformanceResult:
    """Evaluate the currently implemented Bybit public market-data adapter surface.

    This profile intentionally does not claim conformance for private/order APIs that
    ASTRA does not yet implement.
    """

    subject.validate()
    subscription.validate()
    instrument_spec.validate()
    capture.validate()
    continuity_checkpoint.validate()
    replay_evidence.validate()

    bars = capture.bars
    first = bars[0]
    last = bars[-1]

    checks = (
        _check(
            "BYBIT-PMD-001-SUBSCRIPTION-IDENTITY",
            first.symbol == subscription.symbol
            and all(bar.symbol == subscription.symbol for bar in bars)
            and all(bar.interval_seconds == subscription.interval_seconds for bar in bars),
            {
                "subscription_topic": subscription.topic,
                "subscription_symbol": subscription.symbol,
                "subscription_interval_seconds": subscription.interval_seconds,
                "bar_symbols": [bar.symbol for bar in bars],
                "bar_intervals": [bar.interval_seconds for bar in bars],
            },
            "provider bars disagree with subscribed symbol or interval",
        ),
        _check(
            "BYBIT-PMD-002-INSTRUMENT-SPEC",
            instrument_spec.venue.lower() == "bybit"
            and instrument_spec.environment == subject.environment
            and instrument_spec.category == "linear"
            and instrument_spec.symbol == subscription.symbol
            and instrument_spec.status is InstrumentStatus.TRADING,
            {
                "venue": instrument_spec.venue,
                "environment": instrument_spec.environment,
                "category": instrument_spec.category,
                "symbol": instrument_spec.symbol,
                "status": instrument_spec.status.value,
                "revision": instrument_spec.revision,
            },
            "instrument specification is not a tradable matching Bybit linear spec",
        ),
        _check(
            "BYBIT-PMD-003-RAW-CAPTURE-INTEGRITY",
            replay_evidence.raw_response_sha256 == capture.response_sha256
            and replay_evidence.request_url == capture.request_url
            and len(capture.raw_rows) == len(capture.bars),
            {
                "capture_response_sha256": capture.response_sha256,
                "replay_response_sha256": replay_evidence.raw_response_sha256,
                "capture_request_url": capture.request_url,
                "replay_request_url": replay_evidence.request_url,
                "raw_rows": len(capture.raw_rows),
                "bars": len(capture.bars),
            },
            "exact Bybit response bytes or request identity are not bound to replay evidence",
        ),
        _check(
            "BYBIT-PMD-004-CLOCK-BINDING",
            replay_evidence.response_received_at == capture.response_received_at
            and replay_evidence.exchange_server_at == capture.server_at
            and all(
                event.receive_timestamp == capture.response_received_at
                for event in replay_evidence.replay.events
            ),
            {
                "capture_received_at": capture.response_received_at.isoformat(),
                "replay_received_at": replay_evidence.response_received_at.isoformat(),
                "capture_server_at": capture.server_at.isoformat(),
                "replay_server_at": replay_evidence.exchange_server_at.isoformat(),
            },
            "provider receive/exchange clocks are not preserved consistently",
        ),
        _check(
            "BYBIT-PMD-005-NORMALIZED-EVENT-BINDING",
            len(replay_evidence.bindings) == len(bars)
            and all(
                binding.source_event_id == bar.source_event_id
                and binding.bar_id == bar.bar_id
                and binding.normalized_event_sha256 == bar.content_hash
                for binding, bar in zip(replay_evidence.bindings, bars, strict=True)
            ),
            {
                "bindings": [binding.payload() for binding in replay_evidence.bindings],
                "bar_ids": [bar.bar_id for bar in bars],
                "bar_content_hashes": [bar.content_hash for bar in bars],
            },
            "normalized provider events are not exactly bound to replay records",
        ),
        _check(
            "BYBIT-PMD-006-CONTINUITY-PROOF",
            continuity_checkpoint.provider == BYBIT_PROVIDER
            and continuity_checkpoint.venue == BYBIT_LINEAR_VENUE
            and continuity_checkpoint.symbol == subscription.symbol
            and continuity_checkpoint.interval_seconds == subscription.interval_seconds
            and continuity_checkpoint.through_bar_id == last.bar_id
            and continuity_checkpoint.through_close_time == last.close_time
            and replay_evidence.continuity_checkpoint_id == continuity_checkpoint.checkpoint_id,
            {
                "checkpoint": continuity_checkpoint.payload(),
                "replay_checkpoint_id": replay_evidence.continuity_checkpoint_id,
                "last_bar_id": last.bar_id,
                "last_close_time": last.close_time.isoformat(),
            },
            "continuity checkpoint does not prove the replay high-water event",
        ),
        _check(
            "BYBIT-PMD-007-PROVIDER-COMPLETE-REPLAY",
            replay_evidence.replay.profile.value == "PROVIDER_COMPLETE"
            and not replay_evidence.replay.limitations
            and replay_evidence.instrument_spec_revision == instrument_spec.revision,
            {
                "profile": replay_evidence.replay.profile.value,
                "limitations": list(replay_evidence.replay.limitations),
                "replay_instrument_revision": replay_evidence.instrument_spec_revision,
                "instrument_revision": instrument_spec.revision,
            },
            "replay evidence is incomplete or bound to a different instrument revision",
        ),
        _check(
            "BYBIT-PMD-008-DOWNSTREAM-INPUT-BINDING",
            all(
                len(binding.strategy_input_sha256) == 64
                and len(binding.risk_market_input_sha256) == 64
                for binding in replay_evidence.bindings
            ),
            {
                "strategy_input_sha256": [
                    binding.strategy_input_sha256 for binding in replay_evidence.bindings
                ],
                "risk_market_input_sha256": [
                    binding.risk_market_input_sha256 for binding in replay_evidence.bindings
                ],
            },
            "strategy/risk market-input bindings are incomplete",
        ),
    )

    result = AdapterConformanceResult(
        profile_id=_PROFILE_ID,
        profile_version=_PROFILE_VERSION,
        provider=BYBIT_PROVIDER,
        subject="app.marketdata.bybit_public+bybit_repair+bybit_instruments",
        subject_version=subject.adapter_version,
        scope=_SCOPE,
        environment=subject.environment,
        checks=checks,
        limitations=_LIMITATIONS,
    )
    result.validate()
    return result
