from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

_WINDOWS = (5, 15, 60)
_CONNECTED_STATES = frozenset({"CONNECTED", "HEARTBEAT"})
_ALLOWED_COVERAGE_REASONS = frozenset(
    {
        "NO_ELIGIBLE_SUBSCRIPTION",
        "START_STATUS_MISSING",
        "START_STATUS_STALE",
        "START_STATUS_NOT_CONNECTED",
        "NON_CONNECTED_STATUS_IN_WINDOW",
        "DISCONNECT_IN_WINDOW",
        "STATUS_GAP_IN_WINDOW",
        "END_STATUS_MISSING",
        "END_STATUS_STALE",
        "END_STATUS_NOT_CONNECTED",
    }
)


@dataclass(frozen=True)
class LiquidationPoint:
    event_id: str
    event_time: datetime
    liquidated_position_side: str
    estimated_notional_usdt: Decimal

    def validate(self) -> None:
        _validate_sha(self.event_id, "liquidation event")
        _utc(self.event_time)
        if self.liquidated_position_side not in {"LONG", "SHORT"}:
            raise ValueError("liquidation point side must be LONG or SHORT")
        value = self.estimated_notional_usdt
        if not value.is_finite() or value <= 0:
            raise ValueError("liquidation point notional must be positive and finite")


@dataclass(frozen=True)
class LiquidationStatusPoint:
    observed_at: datetime
    state: str

    def validate(self) -> None:
        _utc(self.observed_at)
        if self.state not in {
            "CONNECTING",
            "CONNECTED",
            "HEARTBEAT",
            "DISCONNECTED",
            "STOPPED",
        }:
            raise ValueError("liquidation status point has invalid state")


@dataclass(frozen=True)
class ProspectiveLiquidationWindow:
    window_minutes: int
    window_start_at: datetime
    window_end_at: datetime
    event_count: int | None
    long_liquidation_count: int | None
    short_liquidation_count: int | None
    long_estimated_notional_usdt: Decimal | None
    short_estimated_notional_usdt: Decimal | None
    total_estimated_notional_usdt: Decimal | None
    long_minus_short_estimated_notional_usdt: Decimal | None
    normalized_long_minus_short_imbalance: Decimal | None
    largest_event_estimated_notional_usdt: Decimal | None
    first_event_at: datetime | None
    last_event_at: datetime | None
    known_zero: bool

    def validate(self) -> None:
        if self.window_minutes not in _WINDOWS:
            raise ValueError("liquidation context window is unsupported")
        start = _utc(self.window_start_at)
        end = _utc(self.window_end_at)
        if end - start != timedelta(minutes=self.window_minutes):
            raise ValueError("liquidation context window duration is inconsistent")
        metrics = (
            self.event_count,
            self.long_liquidation_count,
            self.short_liquidation_count,
            self.long_estimated_notional_usdt,
            self.short_estimated_notional_usdt,
            self.total_estimated_notional_usdt,
            self.long_minus_short_estimated_notional_usdt,
            self.normalized_long_minus_short_imbalance,
            self.largest_event_estimated_notional_usdt,
        )
        if self.event_count is None:
            if any(value is not None for value in metrics[1:]):
                raise ValueError("uncovered liquidation window cannot carry metrics")
            if self.first_event_at is not None or self.last_event_at is not None:
                raise ValueError("uncovered liquidation window cannot carry timestamps")
            if self.known_zero:
                raise ValueError("uncovered liquidation window cannot be a known zero")
            return
        if any(value is None for value in metrics[1:]):
            raise ValueError("covered liquidation window requires complete metrics")
        long_count = _required_int(self.long_liquidation_count)
        short_count = _required_int(self.short_liquidation_count)
        if self.event_count < 0 or long_count < 0 or short_count < 0:
            raise ValueError("liquidation window counts cannot be negative")
        if self.event_count != long_count + short_count:
            raise ValueError("liquidation window event counts do not reconcile")
        long_notional = _required_decimal(self.long_estimated_notional_usdt)
        short_notional = _required_decimal(self.short_estimated_notional_usdt)
        total = _required_decimal(self.total_estimated_notional_usdt)
        signed = _required_decimal(self.long_minus_short_estimated_notional_usdt)
        imbalance = _required_decimal(self.normalized_long_minus_short_imbalance)
        largest = _required_decimal(self.largest_event_estimated_notional_usdt)
        if min(long_notional, short_notional, total, largest) < 0:
            raise ValueError("liquidation window notionals cannot be negative")
        if total != long_notional + short_notional:
            raise ValueError("liquidation window total notional does not reconcile")
        if signed != long_notional - short_notional:
            raise ValueError("liquidation window signed notional does not reconcile")
        if not Decimal("-1") <= imbalance <= Decimal("1"):
            raise ValueError("liquidation window imbalance must be within [-1, 1]")
        if self.event_count == 0:
            zero_values = (
                long_count,
                short_count,
                long_notional,
                short_notional,
                total,
                signed,
                imbalance,
                largest,
            )
            if any(value != 0 for value in zero_values):
                raise ValueError("known-zero liquidation window must have zero metrics")
            if self.first_event_at is not None or self.last_event_at is not None:
                raise ValueError("known-zero liquidation window cannot carry timestamps")
            if not self.known_zero:
                raise ValueError("covered zero-event window must be marked known_zero")
            return
        if self.known_zero:
            raise ValueError("non-empty liquidation window cannot be marked known_zero")
        if total <= 0 or largest <= 0:
            raise ValueError("non-empty liquidation window must have positive notional")
        if imbalance != signed / total:
            raise ValueError("liquidation window imbalance does not reconcile")
        if self.first_event_at is None or self.last_event_at is None:
            raise ValueError("non-empty liquidation window requires timestamps")
        first = _utc(self.first_event_at)
        last = _utc(self.last_event_at)
        if not start <= first <= last < end:
            raise ValueError("liquidation window event timestamps are out of bounds")

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "window_minutes": self.window_minutes,
            "window_start_at": _utc(self.window_start_at).isoformat(),
            "window_end_at": _utc(self.window_end_at).isoformat(),
            "event_count": self.event_count,
            "long_liquidation_count": self.long_liquidation_count,
            "short_liquidation_count": self.short_liquidation_count,
            "long_estimated_notional_usdt": _decimal_text(
                self.long_estimated_notional_usdt
            ),
            "short_estimated_notional_usdt": _decimal_text(
                self.short_estimated_notional_usdt
            ),
            "total_estimated_notional_usdt": _decimal_text(
                self.total_estimated_notional_usdt
            ),
            "long_minus_short_estimated_notional_usdt": _decimal_text(
                self.long_minus_short_estimated_notional_usdt
            ),
            "normalized_long_minus_short_imbalance": _decimal_text(
                self.normalized_long_minus_short_imbalance
            ),
            "largest_event_estimated_notional_usdt": _decimal_text(
                self.largest_event_estimated_notional_usdt
            ),
            "first_event_at": _time_text(self.first_event_at),
            "last_event_at": _time_text(self.last_event_at),
            "known_zero": self.known_zero,
        }


@dataclass(frozen=True)
class ProspectiveLiquidationContext:
    seed_id: str
    source_snapshot_id: str
    symbol: str
    side: str
    signal_available_at: datetime
    coverage_window_start_at: datetime
    coverage_subscription_id: str | None
    coverage_qualified: bool
    coverage_reason_codes: tuple[str, ...]
    coverage_start_status_at: datetime | None
    coverage_end_status_at: datetime | None
    maximum_status_age_seconds: int
    evaluated_at: datetime
    windows: tuple[ProspectiveLiquidationWindow, ...]
    prospective: bool = True
    liquidation_feature_used_for_source_ranking: bool = False
    parameter_retuning_performed: bool = False
    trade_actionable: bool = False
    strategy_promotion_allowed: bool = False
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False
    bybit_live_order_routing_allowed: bool = False

    def validate(self) -> None:
        _validate_sha(self.seed_id, "shadow seed")
        _validate_sha(self.source_snapshot_id, "source snapshot")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("liquidation context symbol is invalid")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("liquidation context side is invalid")
        signal = _utc(self.signal_available_at)
        start = _utc(self.coverage_window_start_at)
        evaluated = _utc(self.evaluated_at)
        if start != signal - timedelta(minutes=60):
            raise ValueError("liquidation context must cover 60 minutes before signal")
        if evaluated < signal + timedelta(seconds=60):
            raise ValueError("liquidation context evaluation is too early")
        if not 20 <= self.maximum_status_age_seconds <= 300:
            raise ValueError("liquidation context status-age bound is invalid")
        if len(set(self.coverage_reason_codes)) != len(self.coverage_reason_codes):
            raise ValueError("liquidation coverage reasons must be unique")
        if any(
            reason not in _ALLOWED_COVERAGE_REASONS
            for reason in self.coverage_reason_codes
        ):
            raise ValueError("liquidation coverage reason is unsupported")
        if self.coverage_qualified:
            if self.coverage_subscription_id is None:
                raise ValueError("qualified context requires subscription identity")
            _validate_sha(
                self.coverage_subscription_id,
                "liquidation subscription",
            )
            if self.coverage_reason_codes:
                raise ValueError("qualified context cannot carry coverage blockers")
            if self.coverage_start_status_at is None:
                raise ValueError("qualified context requires start coverage status")
            if self.coverage_end_status_at is None:
                raise ValueError("qualified context requires end coverage status")
            _utc(self.coverage_start_status_at)
            _utc(self.coverage_end_status_at)
            if any(item.event_count is None for item in self.windows):
                raise ValueError("qualified context requires liquidation metrics")
        else:
            if not self.coverage_reason_codes:
                raise ValueError("unqualified context requires coverage blockers")
            if any(item.event_count is not None for item in self.windows):
                raise ValueError("unqualified context cannot carry liquidation metrics")
        if tuple(item.window_minutes for item in self.windows) != _WINDOWS:
            raise ValueError("liquidation context windows must be 5m/15m/60m")
        for item in self.windows:
            item.validate()
            if _utc(item.window_end_at) != signal:
                raise ValueError("liquidation context windows must end at signal")
        if (
            not self.prospective
            or self.liquidation_feature_used_for_source_ranking
            or self.parameter_retuning_performed
            or self.trade_actionable
            or self.strategy_promotion_allowed
            or self.demo_activation_allowed
            or self.live_activation_allowed
            or self.bybit_live_order_routing_allowed
        ):
            raise ValueError("prospective liquidation context cannot activate trading")

    @property
    def context_id(self) -> str:
        canonical = json.dumps(
            self.to_payload(include_context_id=False),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def to_payload(self, *, include_context_id: bool = True) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "schema": "BYBIT_PROSPECTIVE_LIQUIDATION_CONTEXT_V117",
            "seed_id": self.seed_id,
            "source_snapshot_id": self.source_snapshot_id,
            "symbol": self.symbol,
            "side": self.side,
            "signal_available_at": _utc(self.signal_available_at).isoformat(),
            "coverage_window_start_at": _utc(
                self.coverage_window_start_at
            ).isoformat(),
            "coverage_subscription_id": self.coverage_subscription_id,
            "coverage_qualified": self.coverage_qualified,
            "coverage_reason_codes": list(self.coverage_reason_codes),
            "coverage_start_status_at": _time_text(
                self.coverage_start_status_at
            ),
            "coverage_end_status_at": _time_text(self.coverage_end_status_at),
            "maximum_status_age_seconds": self.maximum_status_age_seconds,
            "evaluated_at": _utc(self.evaluated_at).isoformat(),
            "windows": [item.to_payload() for item in self.windows],
            "prospective": self.prospective,
            "liquidation_feature_used_for_source_ranking": (
                self.liquidation_feature_used_for_source_ranking
            ),
            "parameter_retuning_performed": self.parameter_retuning_performed,
            "trade_actionable": self.trade_actionable,
            "strategy_promotion_allowed": self.strategy_promotion_allowed,
            "demo_activation_allowed": self.demo_activation_allowed,
            "live_activation_allowed": self.live_activation_allowed,
            "bybit_live_order_routing_allowed": (
                self.bybit_live_order_routing_allowed
            ),
            "causal_claim_allowed": False,
            "predictive_guarantee_allowed": False,
        }
        if include_context_id:
            payload["context_id"] = self.context_id
        return payload


def assess_single_subscription_coverage(
    *,
    window_start: datetime,
    signal_available_at: datetime,
    statuses: tuple[LiquidationStatusPoint, ...],
    maximum_status_age_seconds: int = 60,
) -> tuple[bool, tuple[str, ...], datetime | None, datetime | None]:
    start = _utc(window_start)
    signal = _utc(signal_available_at)
    if signal <= start:
        raise ValueError("liquidation coverage window must have positive duration")
    if not 20 <= maximum_status_age_seconds <= 300:
        raise ValueError("liquidation coverage status-age bound is invalid")
    for status in statuses:
        status.validate()
    ordered = tuple(sorted(statuses, key=lambda item: _utc(item.observed_at)))
    before_start = [item for item in ordered if _utc(item.observed_at) <= start]
    before_end = [item for item in ordered if _utc(item.observed_at) <= signal]
    start_point = before_start[-1] if before_start else None
    end_point = before_end[-1] if before_end else None
    maximum_age = timedelta(seconds=maximum_status_age_seconds)
    reasons: list[str] = []
    if start_point is None:
        reasons.append("START_STATUS_MISSING")
    else:
        if start - _utc(start_point.observed_at) > maximum_age:
            reasons.append("START_STATUS_STALE")
        if start_point.state not in _CONNECTED_STATES:
            reasons.append("START_STATUS_NOT_CONNECTED")
    in_window = [
        item
        for item in ordered
        if start < _utc(item.observed_at) <= signal
    ]
    if any(item.state in {"DISCONNECTED", "STOPPED"} for item in in_window):
        reasons.append("DISCONNECT_IN_WINDOW")
    if any(item.state not in _CONNECTED_STATES for item in in_window):
        reasons.append("NON_CONNECTED_STATUS_IN_WINDOW")
    connected_points = [
        item for item in in_window if item.state in _CONNECTED_STATES
    ]
    if start_point is not None and start_point.state in _CONNECTED_STATES:
        previous = start
        for point in connected_points:
            observed = _utc(point.observed_at)
            if observed - previous > maximum_age:
                reasons.append("STATUS_GAP_IN_WINDOW")
                break
            previous = observed
    if end_point is None:
        reasons.append("END_STATUS_MISSING")
    else:
        if signal - _utc(end_point.observed_at) > maximum_age:
            reasons.append("END_STATUS_STALE")
        if end_point.state not in _CONNECTED_STATES:
            reasons.append("END_STATUS_NOT_CONNECTED")
    unique = tuple(dict.fromkeys(reasons))
    return (
        not unique,
        unique,
        None if start_point is None else _utc(start_point.observed_at),
        None if end_point is None else _utc(end_point.observed_at),
    )


def build_prospective_liquidation_context(
    *,
    seed_id: str,
    source_snapshot_id: str,
    symbol: str,
    side: str,
    signal_available_at: datetime,
    evaluated_at: datetime,
    coverage_subscription_id: str | None,
    coverage_statuses: tuple[LiquidationStatusPoint, ...],
    events: tuple[LiquidationPoint, ...],
    maximum_status_age_seconds: int = 60,
) -> ProspectiveLiquidationContext:
    signal = _utc(signal_available_at)
    coverage_start = signal - timedelta(minutes=60)
    if coverage_subscription_id is None:
        qualified = False
        reasons = ("NO_ELIGIBLE_SUBSCRIPTION",)
        start_status_at = None
        end_status_at = None
    else:
        _validate_sha(
            coverage_subscription_id,
            "liquidation subscription",
        )
        qualified, reasons, start_status_at, end_status_at = (
            assess_single_subscription_coverage(
                window_start=coverage_start,
                signal_available_at=signal,
                statuses=coverage_statuses,
                maximum_status_age_seconds=maximum_status_age_seconds,
            )
        )
    for event in events:
        event.validate()
        event_time = _utc(event.event_time)
        if event_time >= signal:
            raise ValueError("liquidation context cannot use event at/after signal")
        if event_time < coverage_start:
            raise ValueError("liquidation event precedes context lookback")
    windows = tuple(
        _build_window(
            window_minutes=minutes,
            signal_available_at=signal,
            events=events,
            coverage_qualified=qualified,
        )
        for minutes in _WINDOWS
    )
    context = ProspectiveLiquidationContext(
        seed_id=seed_id,
        source_snapshot_id=source_snapshot_id,
        symbol=symbol,
        side=side,
        signal_available_at=signal,
        coverage_window_start_at=coverage_start,
        coverage_subscription_id=coverage_subscription_id,
        coverage_qualified=qualified,
        coverage_reason_codes=reasons,
        coverage_start_status_at=start_status_at,
        coverage_end_status_at=end_status_at,
        maximum_status_age_seconds=maximum_status_age_seconds,
        evaluated_at=_utc(evaluated_at),
        windows=windows,
    )
    context.validate()
    return context


def _build_window(
    *,
    window_minutes: int,
    signal_available_at: datetime,
    events: tuple[LiquidationPoint, ...],
    coverage_qualified: bool,
) -> ProspectiveLiquidationWindow:
    start = signal_available_at - timedelta(minutes=window_minutes)
    if not coverage_qualified:
        return _empty_window(
            window_minutes=window_minutes,
            window_start_at=start,
            window_end_at=signal_available_at,
        )
    rows = tuple(
        item
        for item in events
        if start <= _utc(item.event_time) < signal_available_at
    )
    if not rows:
        return _known_zero_window(
            window_minutes=window_minutes,
            window_start_at=start,
            window_end_at=signal_available_at,
        )
    long_rows = [
        item for item in rows if item.liquidated_position_side == "LONG"
    ]
    short_rows = [
        item for item in rows if item.liquidated_position_side == "SHORT"
    ]
    long_notional = sum(
        (item.estimated_notional_usdt for item in long_rows),
        Decimal("0"),
    )
    short_notional = sum(
        (item.estimated_notional_usdt for item in short_rows),
        Decimal("0"),
    )
    total = long_notional + short_notional
    signed = long_notional - short_notional
    times = tuple(_utc(item.event_time) for item in rows)
    return ProspectiveLiquidationWindow(
        window_minutes=window_minutes,
        window_start_at=start,
        window_end_at=signal_available_at,
        event_count=len(rows),
        long_liquidation_count=len(long_rows),
        short_liquidation_count=len(short_rows),
        long_estimated_notional_usdt=long_notional,
        short_estimated_notional_usdt=short_notional,
        total_estimated_notional_usdt=total,
        long_minus_short_estimated_notional_usdt=signed,
        normalized_long_minus_short_imbalance=signed / total,
        largest_event_estimated_notional_usdt=max(
            item.estimated_notional_usdt for item in rows
        ),
        first_event_at=min(times),
        last_event_at=max(times),
        known_zero=False,
    )


def _empty_window(
    *,
    window_minutes: int,
    window_start_at: datetime,
    window_end_at: datetime,
) -> ProspectiveLiquidationWindow:
    return ProspectiveLiquidationWindow(
        window_minutes=window_minutes,
        window_start_at=window_start_at,
        window_end_at=window_end_at,
        event_count=None,
        long_liquidation_count=None,
        short_liquidation_count=None,
        long_estimated_notional_usdt=None,
        short_estimated_notional_usdt=None,
        total_estimated_notional_usdt=None,
        long_minus_short_estimated_notional_usdt=None,
        normalized_long_minus_short_imbalance=None,
        largest_event_estimated_notional_usdt=None,
        first_event_at=None,
        last_event_at=None,
        known_zero=False,
    )


def _known_zero_window(
    *,
    window_minutes: int,
    window_start_at: datetime,
    window_end_at: datetime,
) -> ProspectiveLiquidationWindow:
    zero = Decimal("0")
    return ProspectiveLiquidationWindow(
        window_minutes=window_minutes,
        window_start_at=window_start_at,
        window_end_at=window_end_at,
        event_count=0,
        long_liquidation_count=0,
        short_liquidation_count=0,
        long_estimated_notional_usdt=zero,
        short_estimated_notional_usdt=zero,
        total_estimated_notional_usdt=zero,
        long_minus_short_estimated_notional_usdt=zero,
        normalized_long_minus_short_imbalance=zero,
        largest_event_estimated_notional_usdt=zero,
        first_event_at=None,
        last_event_at=None,
        known_zero=True,
    )


def _required_int(value: int | None) -> int:
    if value is None:
        raise ValueError("required liquidation integer is missing")
    return value


def _required_decimal(value: Decimal | None) -> Decimal:
    if value is None or not value.is_finite():
        raise ValueError("required liquidation decimal is missing or non-finite")
    return value


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _time_text(value: datetime | None) -> str | None:
    return None if value is None else _utc(value).isoformat()


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("liquidation context timestamp must be timezone-aware")
    if value.utcoffset() is None:
        raise ValueError("liquidation context timestamp must have UTC offset")
    return value.astimezone(UTC)


def _validate_sha(value: str, name: str) -> None:
    if len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError(f"{name} must be lowercase sha256 hex")


__all__ = [
    "LiquidationPoint",
    "LiquidationStatusPoint",
    "ProspectiveLiquidationContext",
    "ProspectiveLiquidationWindow",
    "assess_single_subscription_coverage",
    "build_prospective_liquidation_context",
]
