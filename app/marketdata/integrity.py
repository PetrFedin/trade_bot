from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from app.marketdata.continuity import (
    OperationalContinuityCheckpoint,
    OperationalContinuityStore,
)
from app.marketdata.operational import OperationalBar, OperationalMarketDataStore

_SCHEMA_VERSION = "astra-market-data-integrity-v1"


class MarketDataIntegrityState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    QUARANTINED = "QUARANTINED"


class MarketDataIntegrityAction(StrEnum):
    TRUST = "TRUST"
    READ_ONLY = "READ_ONLY"
    HALT_SCOPE = "HALT_SCOPE"


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MarketDataIntegrityScope:
    provider: str
    venue: str
    symbol: str
    interval_seconds: int

    def validate(self) -> None:
        for name, value in (
            ("provider", self.provider),
            ("venue", self.venue),
            ("symbol", self.symbol),
        ):
            if not value or value != value.strip().upper():
                raise ValueError(f"{name} must be non-empty normalized uppercase")
        if self.interval_seconds < 1:
            raise ValueError("interval_seconds must be positive")


@dataclass(frozen=True)
class MarketDataIntegrityPolicy:
    policy_version: str = "market-data-integrity-policy-v1"
    evaluation_bars: int = 5
    maximum_age_seconds: Decimal = Decimal("15")
    maximum_final_receive_lag_seconds: Decimal = Decimal("15")
    maximum_close_jump_fraction: Decimal = Decimal("0.25")

    def validate(self) -> None:
        if not self.policy_version.strip():
            raise ValueError("policy_version is required")
        if self.evaluation_bars < 1:
            raise ValueError("evaluation_bars must be positive")
        for name, value in (
            ("maximum_age_seconds", self.maximum_age_seconds),
            (
                "maximum_final_receive_lag_seconds",
                self.maximum_final_receive_lag_seconds,
            ),
            ("maximum_close_jump_fraction", self.maximum_close_jump_fraction),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")


@dataclass(frozen=True)
class MarketDataIntegrityAssessment:
    scope: MarketDataIntegrityScope
    policy_version: str
    evaluated_at: datetime
    state: MarketDataIntegrityState
    action: MarketDataIntegrityAction
    reasons: tuple[str, ...]
    bars_checked: int
    conflict_count: int
    checkpoint_id: str | None
    through_bar_id: str | None
    through_close_time: datetime | None
    market_data_age_seconds: Decimal
    latest_receive_lag_seconds: Decimal
    bar_content_hashes: tuple[str, ...]
    schema_version: str = _SCHEMA_VERSION

    @property
    def trusted(self) -> bool:
        return self.action is MarketDataIntegrityAction.TRUST

    def validate(self) -> None:
        self.scope.validate()
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("market-data integrity schema mismatch")
        if not self.policy_version.strip():
            raise ValueError("policy_version is required")
        _aware(self.evaluated_at, "evaluated_at")
        if self.bars_checked < 0 or self.conflict_count < 0:
            raise ValueError("integrity counters must be non-negative")
        for name, value in (
            ("market_data_age_seconds", self.market_data_age_seconds),
            ("latest_receive_lag_seconds", self.latest_receive_lag_seconds),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if len(self.bar_content_hashes) != self.bars_checked:
            raise ValueError("bar hash count must equal bars_checked")
        if len(set(self.reasons)) != len(self.reasons):
            raise ValueError("integrity reasons must be unique")
        if any(not reason.strip() for reason in self.reasons):
            raise ValueError("integrity reasons cannot be blank")
        if (self.checkpoint_id is None) != (self.through_bar_id is None):
            raise ValueError("checkpoint and through-bar identity must appear together")
        if self.through_close_time is not None:
            _aware(self.through_close_time, "through_close_time")

        expected_action = {
            MarketDataIntegrityState.HEALTHY: MarketDataIntegrityAction.TRUST,
            MarketDataIntegrityState.DEGRADED: MarketDataIntegrityAction.READ_ONLY,
            MarketDataIntegrityState.STALE: MarketDataIntegrityAction.READ_ONLY,
            MarketDataIntegrityState.QUARANTINED: MarketDataIntegrityAction.HALT_SCOPE,
        }[self.state]
        if self.action is not expected_action:
            raise ValueError("integrity state/action mapping is invalid")
        if self.state is MarketDataIntegrityState.HEALTHY and self.reasons:
            raise ValueError("HEALTHY integrity assessment cannot contain reasons")
        if self.state is not MarketDataIntegrityState.HEALTHY and not self.reasons:
            raise ValueError("non-healthy integrity assessment requires reasons")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "scope": {
                "provider": self.scope.provider,
                "venue": self.scope.venue,
                "symbol": self.scope.symbol,
                "interval_seconds": self.scope.interval_seconds,
            },
            "policy_version": self.policy_version,
            "evaluated_at": _aware(self.evaluated_at, "evaluated_at").isoformat(),
            "state": self.state.value,
            "action": self.action.value,
            "reasons": list(self.reasons),
            "bars_checked": self.bars_checked,
            "conflict_count": self.conflict_count,
            "checkpoint_id": self.checkpoint_id,
            "through_bar_id": self.through_bar_id,
            "through_close_time": (
                None
                if self.through_close_time is None
                else _aware(self.through_close_time, "through_close_time").isoformat()
            ),
            "market_data_age_seconds": _decimal_text(self.market_data_age_seconds),
            "latest_receive_lag_seconds": _decimal_text(
                self.latest_receive_lag_seconds
            ),
            "bar_content_hashes": list(self.bar_content_hashes),
        }

    @property
    def evidence_sha256(self) -> str:
        return _canonical_sha256(self.payload())


def _assessment(
    *,
    scope: MarketDataIntegrityScope,
    policy: MarketDataIntegrityPolicy,
    evaluated_at: datetime,
    state: MarketDataIntegrityState,
    reasons: set[str],
    bars: tuple[OperationalBar, ...],
    conflict_count: int,
    checkpoint: OperationalContinuityCheckpoint | None,
    age_seconds: Decimal,
    receive_lag_seconds: Decimal,
) -> MarketDataIntegrityAssessment:
    action = {
        MarketDataIntegrityState.HEALTHY: MarketDataIntegrityAction.TRUST,
        MarketDataIntegrityState.DEGRADED: MarketDataIntegrityAction.READ_ONLY,
        MarketDataIntegrityState.STALE: MarketDataIntegrityAction.READ_ONLY,
        MarketDataIntegrityState.QUARANTINED: MarketDataIntegrityAction.HALT_SCOPE,
    }[state]
    value = MarketDataIntegrityAssessment(
        scope=scope,
        policy_version=policy.policy_version,
        evaluated_at=evaluated_at,
        state=state,
        action=action,
        reasons=tuple(sorted(reasons)),
        bars_checked=len(bars),
        conflict_count=conflict_count,
        checkpoint_id=None if checkpoint is None else checkpoint.checkpoint_id,
        through_bar_id=None if checkpoint is None else checkpoint.through_bar_id,
        through_close_time=(
            None if checkpoint is None else checkpoint.through_close_time
        ),
        market_data_age_seconds=age_seconds,
        latest_receive_lag_seconds=receive_lag_seconds,
        bar_content_hashes=tuple(bar.content_hash for bar in bars),
    )
    value.validate()
    return value


def assess_market_data_integrity(
    *,
    scope: MarketDataIntegrityScope,
    policy: MarketDataIntegrityPolicy,
    evaluated_at: datetime,
    bars: tuple[OperationalBar, ...],
    checkpoint: OperationalContinuityCheckpoint | None,
    conflict_count: int,
) -> MarketDataIntegrityAssessment:
    scope.validate()
    policy.validate()
    current = _aware(evaluated_at, "evaluated_at")
    if conflict_count < 0:
        raise ValueError("conflict_count must be non-negative")

    reasons: set[str] = set()
    quarantine = False
    degraded = False
    stale = False
    age_seconds = Decimal("0")
    receive_lag_seconds = Decimal("0")

    if checkpoint is None:
        reasons.add("MARKET_DATA_CONTINUITY_MISSING")
        quarantine = True
    else:
        try:
            checkpoint.validate()
        except ValueError:
            reasons.add("MARKET_DATA_CONTINUITY_INVALID")
            quarantine = True
        if (
            checkpoint.provider != scope.provider
            or checkpoint.venue != scope.venue
            or checkpoint.symbol != scope.symbol
            or checkpoint.interval_seconds != scope.interval_seconds
        ):
            reasons.add("MARKET_DATA_CONTINUITY_SCOPE_MISMATCH")
            quarantine = True
        through = _aware(checkpoint.through_close_time, "through_close_time")
        if through > current:
            reasons.add("MARKET_DATA_CLOCK_CONFLICT")
            quarantine = True
        else:
            age_seconds = Decimal(str((current - through).total_seconds()))
            if age_seconds > policy.maximum_age_seconds:
                reasons.add("MARKET_DATA_STALE")
                stale = True

    if conflict_count:
        reasons.add("MARKET_DATA_CONFLICT_PRESENT")
        quarantine = True

    if not bars:
        reasons.add("MARKET_DATA_THROUGH_BAR_MISSING")
        quarantine = True
    else:
        previous: OperationalBar | None = None
        for bar in bars:
            try:
                bar.validate()
            except ValueError:
                reasons.add("MARKET_DATA_BAR_INVALID")
                quarantine = True
                continue
            if (
                bar.provider != scope.provider
                or bar.venue != scope.venue
                or bar.symbol != scope.symbol
                or bar.interval_seconds != scope.interval_seconds
            ):
                reasons.add("MARKET_DATA_BAR_SCOPE_MISMATCH")
                quarantine = True
            if not bar.is_final:
                reasons.add("MARKET_DATA_BAR_NOT_FINAL")
                quarantine = True
            if bar.close_time > current or bar.received_at > current:
                reasons.add("MARKET_DATA_CLOCK_CONFLICT")
                quarantine = True
            if previous is not None:
                if bar.open_time != previous.close_time:
                    reasons.add("MARKET_DATA_GAP_PRESENT")
                    quarantine = True
                if bar.close <= 0 or previous.close <= 0:
                    reasons.add("MARKET_DATA_BAR_INVALID")
                    quarantine = True
                else:
                    jump = abs(bar.close - previous.close) / previous.close
                    if jump > policy.maximum_close_jump_fraction:
                        reasons.add("MARKET_DATA_PRICE_JUMP_REVIEW_REQUIRED")
                        degraded = True
            if bar.revision > 0:
                reasons.add("MARKET_DATA_REVISION_PRESENT")
                degraded = True
            previous = bar

        latest = bars[-1]
        if latest.received_at >= latest.close_time:
            receive_lag_seconds = Decimal(
                str((latest.received_at - latest.close_time).total_seconds())
            )
            if receive_lag_seconds > policy.maximum_final_receive_lag_seconds:
                reasons.add("MARKET_DATA_RECEIVE_LAG_EXCEEDED")
                degraded = True
        else:
            reasons.add("MARKET_DATA_RECEIVED_BEFORE_CLOSE")
            quarantine = True

        if checkpoint is not None:
            if (
                latest.bar_id != checkpoint.through_bar_id
                or latest.close_time != checkpoint.through_close_time
            ):
                reasons.add("MARKET_DATA_CONTINUITY_MISMATCH")
                quarantine = True

    if quarantine:
        state = MarketDataIntegrityState.QUARANTINED
    elif stale:
        state = MarketDataIntegrityState.STALE
    elif degraded:
        state = MarketDataIntegrityState.DEGRADED
    else:
        state = MarketDataIntegrityState.HEALTHY

    return _assessment(
        scope=scope,
        policy=policy,
        evaluated_at=current,
        state=state,
        reasons=reasons,
        bars=bars,
        conflict_count=conflict_count,
        checkpoint=checkpoint,
        age_seconds=age_seconds,
        receive_lag_seconds=receive_lag_seconds,
    )


class MarketDataIntegrityAuthority:
    """Read-only authority that evaluates whether operational market state is trustworthy."""

    def __init__(
        self,
        *,
        scope: MarketDataIntegrityScope,
        marketdata: OperationalMarketDataStore,
        continuity: OperationalContinuityStore,
        policy: MarketDataIntegrityPolicy | None = None,
    ) -> None:
        resolved = MarketDataIntegrityPolicy() if policy is None else policy
        scope.validate()
        resolved.validate()
        self.scope = scope
        self.marketdata = marketdata
        self.continuity = continuity
        self.policy = resolved

    def evaluate(self, *, now: datetime) -> MarketDataIntegrityAssessment:
        current = _aware(now, "now")
        checkpoint: OperationalContinuityCheckpoint | None = None
        bars: tuple[OperationalBar, ...] = ()
        conflicts = 1
        try:
            conflicts = self.marketdata.conflict_count()
            if not isinstance(conflicts, int) or conflicts < 0:
                raise ValueError("market conflict count is invalid")
            checkpoint = self.continuity.latest(
                provider=self.scope.provider,
                venue=self.scope.venue,
                symbol=self.scope.symbol,
                interval_seconds=self.scope.interval_seconds,
            )
            if checkpoint is not None:
                checkpoint.validate()
                bars = self.marketdata.recent_bars(
                    provider=self.scope.provider,
                    venue=self.scope.venue,
                    symbol=self.scope.symbol,
                    interval_seconds=self.scope.interval_seconds,
                    through_close_time=checkpoint.through_close_time,
                    limit=self.policy.evaluation_bars,
                )
        except Exception:
            return _assessment(
                scope=self.scope,
                policy=self.policy,
                evaluated_at=current,
                state=MarketDataIntegrityState.QUARANTINED,
                reasons={"MARKET_DATA_AUTHORITY_UNAVAILABLE"},
                bars=(),
                conflict_count=1,
                checkpoint=None,
                age_seconds=Decimal("0"),
                receive_lag_seconds=Decimal("0"),
            )

        return assess_market_data_integrity(
            scope=self.scope,
            policy=self.policy,
            evaluated_at=current,
            bars=bars,
            checkpoint=checkpoint,
            conflict_count=conflicts,
        )
