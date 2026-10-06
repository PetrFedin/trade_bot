from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from app.marketdata.bybit_repair import BybitKlineRangeCapture
from app.marketdata.continuity import OperationalContinuityCheckpoint
from app.qualification.adapter_conformance import AdapterConformanceReport
from app.qualification.bybit_provider_replay import BybitProviderReplayEvidence

_SCHEMA_VERSION = "astra-marketdata-integrity-v1"
_POLICY_VERSION = "1.0.0"


class MarketDataIntegrityStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    QUARANTINED = "QUARANTINED"
    BLOCKED = "BLOCKED"


class MarketDataSafetyAction(StrEnum):
    ALLOW_QUALIFIED_PAPER_INPUT = "ALLOW_QUALIFIED_PAPER_INPUT"
    READ_ONLY = "READ_ONLY"
    QUARANTINE_FEED = "QUARANTINE_FEED"
    BLOCK_QUALIFICATION = "BLOCK_QUALIFICATION"


@dataclass(frozen=True)
class MarketDataIntegrityPolicy:
    maximum_server_skew_seconds: Decimal
    maximum_receive_delay_seconds: Decimal
    maximum_final_bar_age_seconds: Decimal
    version: str = _POLICY_VERSION

    def validate(self) -> None:
        if not self.version.strip():
            raise ValueError("market-data integrity policy version is required")
        for name, value in (
            ("maximum_server_skew_seconds", self.maximum_server_skew_seconds),
            ("maximum_receive_delay_seconds", self.maximum_receive_delay_seconds),
            ("maximum_final_bar_age_seconds", self.maximum_final_bar_age_seconds),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")

    def payload(self) -> dict[str, str]:
        self.validate()
        return {
            "version": self.version,
            "maximum_server_skew_seconds": _decimal(self.maximum_server_skew_seconds),
            "maximum_receive_delay_seconds": _decimal(self.maximum_receive_delay_seconds),
            "maximum_final_bar_age_seconds": _decimal(self.maximum_final_bar_age_seconds),
        }


@dataclass(frozen=True)
class MarketDataIntegrityDecision:
    provider: str
    venue: str
    symbol: str
    interval_seconds: int
    policy: MarketDataIntegrityPolicy
    status: MarketDataIntegrityStatus
    action: MarketDataSafetyAction
    reasons: tuple[str, ...]
    observed_at: datetime
    provider_replay_sha256: str
    adapter_conformance_sha256: str
    continuity_checkpoint_id: str
    conflict_count: int
    server_skew_seconds: Decimal
    maximum_receive_delay_seconds: Decimal
    final_bar_age_seconds: Decimal
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("market-data integrity schema mismatch")
        for name, value in (
            ("provider", self.provider),
            ("venue", self.venue),
            ("symbol", self.symbol),
            ("continuity_checkpoint_id", self.continuity_checkpoint_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        if self.symbol != self.symbol.upper():
            raise ValueError("symbol must be uppercase")
        if self.interval_seconds < 1:
            raise ValueError("interval_seconds must be positive")
        self.policy.validate()
        _aware(self.observed_at, "observed_at")
        _digest(self.provider_replay_sha256, "provider_replay_sha256")
        _digest(self.adapter_conformance_sha256, "adapter_conformance_sha256")
        if self.conflict_count < 0:
            raise ValueError("conflict_count must be non-negative")
        for name, value in (
            ("server_skew_seconds", self.server_skew_seconds),
            ("maximum_receive_delay_seconds", self.maximum_receive_delay_seconds),
            ("final_bar_age_seconds", self.final_bar_age_seconds),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("market-data integrity reasons must be unique")
        if any(not reason.strip() for reason in self.reasons):
            raise ValueError("market-data integrity reasons cannot be blank")
        if self.status is MarketDataIntegrityStatus.HEALTHY:
            if self.reasons:
                raise ValueError("HEALTHY market data cannot carry failure reasons")
            if self.action is not MarketDataSafetyAction.ALLOW_QUALIFIED_PAPER_INPUT:
                raise ValueError("HEALTHY market data requires paper-input allow action")
        else:
            if not self.reasons:
                raise ValueError("non-HEALTHY market data requires reasons")
            if self.action is MarketDataSafetyAction.ALLOW_QUALIFIED_PAPER_INPUT:
                raise ValueError("unhealthy market data cannot allow qualified paper input")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "provider": self.provider,
            "venue": self.venue,
            "symbol": self.symbol,
            "interval_seconds": self.interval_seconds,
            "policy": self.policy.payload(),
            "status": self.status.value,
            "action": self.action.value,
            "reasons": list(self.reasons),
            "observed_at": _aware(self.observed_at, "observed_at").isoformat(),
            "provider_replay_sha256": self.provider_replay_sha256,
            "adapter_conformance_sha256": self.adapter_conformance_sha256,
            "continuity_checkpoint_id": self.continuity_checkpoint_id,
            "conflict_count": self.conflict_count,
            "server_skew_seconds": _decimal(self.server_skew_seconds),
            "maximum_receive_delay_seconds": _decimal(
                self.maximum_receive_delay_seconds
            ),
            "final_bar_age_seconds": _decimal(self.final_bar_age_seconds),
        }

    @property
    def evidence_sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.payload(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()


def evaluate_bybit_marketdata_integrity(
    *,
    capture: BybitKlineRangeCapture,
    continuity_checkpoint: OperationalContinuityCheckpoint,
    provider_replay: BybitProviderReplayEvidence,
    adapter_conformance: AdapterConformanceReport,
    policy: MarketDataIntegrityPolicy,
    observed_at: datetime,
    conflict_count: int = 0,
) -> MarketDataIntegrityDecision:
    """Create a deterministic feed-trust verdict without mutating trading authority."""

    policy.validate()
    now = _aware(observed_at, "observed_at")
    if conflict_count < 0:
        raise ValueError("conflict_count must be non-negative")

    structural_errors: list[str] = []
    for name, value in (
        ("capture", capture),
        ("continuity_checkpoint", continuity_checkpoint),
        ("provider_replay", provider_replay),
        ("adapter_conformance", adapter_conformance),
    ):
        try:
            value.validate()
        except Exception as exc:
            structural_errors.append(f"{name}:{type(exc).__name__}:{exc}")

    bars = capture.bars
    if not bars:
        structural_errors.append("capture:EMPTY")
        provider = "UNKNOWN"
        venue = "UNKNOWN"
        symbol = "UNKNOWN"
        interval_seconds = 1
        maximum_receive_delay = Decimal("0")
        final_bar_age = Decimal("0")
    else:
        provider = bars[0].provider
        venue = bars[0].venue
        symbol = bars[0].symbol
        interval_seconds = bars[0].interval_seconds
        maximum_receive_delay = max(
            _seconds(bar.received_at - bar.source_timestamp) for bar in bars
        )
        final_bar_age = _non_negative_age(now, bars[-1].close_time)

    server_skew = _absolute_seconds(
        capture.response_received_at - capture.server_at
    )

    reasons: set[str] = set()
    status = MarketDataIntegrityStatus.HEALTHY
    action = MarketDataSafetyAction.ALLOW_QUALIFIED_PAPER_INPUT

    if structural_errors:
        status = MarketDataIntegrityStatus.BLOCKED
        action = MarketDataSafetyAction.BLOCK_QUALIFICATION
        reasons.add("STRUCTURAL_EVIDENCE_INVALID")
    elif not adapter_conformance.qualified:
        status = MarketDataIntegrityStatus.BLOCKED
        action = MarketDataSafetyAction.BLOCK_QUALIFICATION
        reasons.add("ADAPTER_NOT_QUALIFIED")
    elif (
        continuity_checkpoint.through_bar_id != bars[-1].bar_id
        or continuity_checkpoint.through_close_time != bars[-1].close_time
        or provider_replay.continuity_checkpoint_id
        != continuity_checkpoint.checkpoint_id
    ):
        status = MarketDataIntegrityStatus.QUARANTINED
        action = MarketDataSafetyAction.QUARANTINE_FEED
        reasons.add("CONTINUITY_PROOF_MISMATCH")
    elif conflict_count > 0:
        status = MarketDataIntegrityStatus.QUARANTINED
        action = MarketDataSafetyAction.QUARANTINE_FEED
        reasons.add("MARKET_DATA_CONFLICT_PRESENT")
    elif final_bar_age > policy.maximum_final_bar_age_seconds:
        status = MarketDataIntegrityStatus.STALE
        action = MarketDataSafetyAction.READ_ONLY
        reasons.add("FINAL_BAR_STALE")
    else:
        if server_skew > policy.maximum_server_skew_seconds:
            reasons.add("PROVIDER_SERVER_SKEW_EXCEEDED")
        if maximum_receive_delay > policy.maximum_receive_delay_seconds:
            reasons.add("PROVIDER_EVENT_DELAY_EXCEEDED")
        if reasons:
            status = MarketDataIntegrityStatus.DEGRADED
            action = MarketDataSafetyAction.READ_ONLY

    decision = MarketDataIntegrityDecision(
        provider=provider,
        venue=venue,
        symbol=symbol,
        interval_seconds=interval_seconds,
        policy=policy,
        status=status,
        action=action,
        reasons=tuple(sorted(reasons)),
        observed_at=now,
        provider_replay_sha256=provider_replay.evidence_sha256,
        adapter_conformance_sha256=adapter_conformance.evidence_sha256,
        continuity_checkpoint_id=continuity_checkpoint.checkpoint_id,
        conflict_count=conflict_count,
        server_skew_seconds=server_skew,
        maximum_receive_delay_seconds=maximum_receive_delay,
        final_bar_age_seconds=final_bar_age,
    )
    decision.validate()
    return decision


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _seconds(delta) -> Decimal:  # type: ignore[no-untyped-def]
    value = Decimal(str(delta.total_seconds()))
    if value < 0:
        raise ValueError("provider event receive time cannot precede source time")
    return value


def _absolute_seconds(delta) -> Decimal:  # type: ignore[no-untyped-def]
    return abs(Decimal(str(delta.total_seconds())))


def _non_negative_age(now: datetime, timestamp: datetime) -> Decimal:
    current = _aware(now, "now")
    event = _aware(timestamp, "timestamp")
    if event > current:
        raise ValueError("market-data timestamp cannot be in the future")
    return Decimal(str((current - event).total_seconds()))


def _decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized
