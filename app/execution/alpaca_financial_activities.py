from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import urlencode, urlparse

from app.execution.financial_activity_store import (
    BrokerFinancialActivity,
    FinancialActivityStore,
)
from app.portfolio.ledger import CashAdjustmentKind, PortfolioLedger
from app.portfolio.protocols import PortfolioStore


class FinancialActivityRecoveryError(RuntimeError):
    pass


class FinancialActivityProtocolError(ValueError):
    pass


class FinancialActivityRateLimitExceeded(RuntimeError):
    pass


class FinancialActivityCredentials(Protocol):
    def rest_headers(self) -> Mapping[str, str]: ...


class FinancialHttpResponse(Protocol):
    status: int
    body: bytes


class FinancialHttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> FinancialHttpResponse: ...


@dataclass(frozen=True)
class FinancialActivityEndpoints:
    rest_base_url: str = "https://paper-api.alpaca.markets"

    def validate(self) -> None:
        parsed = urlparse(self.rest_base_url)
        if parsed.scheme != "https":
            raise ValueError("financial activity REST endpoint must use https")
        if parsed.hostname != "paper-api.alpaca.markets":
            raise ValueError("financial activity reader is restricted to Alpaca Paper")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("financial activity REST endpoint must be a clean base URL")


@dataclass(frozen=True)
class FinancialActivityReadPolicy:
    timeout_seconds: float = 10.0
    maximum_response_bytes: int = 2_000_000
    maximum_read_attempts: int = 3
    initial_backoff_seconds: float = 0.25
    maximum_backoff_seconds: float = 2.0
    read_capacity: float = 20.0
    read_refill_per_second: float = 10.0

    def validate(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.maximum_response_bytes < 1:
            raise ValueError("maximum_response_bytes must be positive")
        if self.maximum_read_attempts < 1:
            raise ValueError("maximum_read_attempts must be positive")
        if self.initial_backoff_seconds < 0 or self.maximum_backoff_seconds < 0:
            raise ValueError("backoff seconds must be non-negative")
        if self.maximum_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("maximum backoff cannot be below initial backoff")
        if self.read_capacity <= 0 or self.read_refill_per_second <= 0:
            raise ValueError("read rate limits must be positive")


class _TokenBucket:
    def __init__(
        self,
        *,
        capacity: float,
        refill_per_second: float,
        clock: Callable[[], float],
    ) -> None:
        if capacity <= 0 or refill_per_second <= 0:
            raise ValueError("token bucket configuration must be positive")
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self.clock = clock
        self.tokens = capacity
        self.last_refill = clock()

    def try_acquire(self) -> bool:
        now = self.clock()
        elapsed = max(0.0, now - self.last_refill)
        self.last_refill = now
        self.tokens = min(
            self.capacity,
            self.tokens + elapsed * self.refill_per_second,
        )
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True


_EXTERNAL_FLOW_TYPES = {"CSD", "CSW"}
_FEE_TYPES = {
    "CFEE",
    "DIVFEE",
    "DIVFT",
    "DIVNRA",
    "DIVTW",
    "FEE",
    "INTNRA",
    "INTTW",
    "PTC",
}
_INCOME_TYPES = {
    "CGD",
    "DIV",
    "DIVCGL",
    "DIVCGS",
    "DIVROC",
    "DIVTXEX",
    "INT",
    "PTR",
}


@dataclass(frozen=True)
class FinancialActivityPage:
    activities: tuple[BrokerFinancialActivity, ...]
    next_page_token: str | None


class FinancialActivitySource(Protocol):
    def page(
        self,
        *,
        after: datetime,
        until: datetime,
        page_size: int,
        page_token: str | None,
    ) -> FinancialActivityPage: ...


@dataclass(frozen=True)
class FinancialActivityRecoveryPolicy:
    overlap: timedelta = timedelta(days=1)
    maximum_window: timedelta = timedelta(days=7)
    maximum_pages: int = 20
    maximum_activities: int = 2000
    page_size: int = 100

    def validate(self) -> None:
        if self.overlap < timedelta(0):
            raise ValueError("overlap must be non-negative")
        if self.maximum_window <= timedelta(0):
            raise ValueError("maximum_window must be positive")
        if self.maximum_pages < 1 or self.maximum_activities < 1:
            raise ValueError("recovery limits must be positive")
        if self.page_size < 1 or self.page_size > 100:
            raise ValueError("page_size must be within [1, 100]")


@dataclass(frozen=True)
class FinancialActivityRecoveryResult:
    acquisition_complete: bool
    ready: bool
    recovered_through: datetime | None
    pages_read: int
    activities_seen: int
    projected: int
    duplicates: int
    quarantined: int
    pending: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class FinancialActivityReadiness:
    ready: bool
    reasons: tuple[str, ...]
    recovered_through: datetime | None
    pending: int
    quarantined: int


class AlpacaPaperFinancialActivityReader:
    """GET-only reader for non-trade Alpaca Paper account activities."""

    def __init__(
        self,
        *,
        credentials: FinancialActivityCredentials,
        transport: FinancialHttpTransport,
        account_identity: str,
        release_identity: str,
        endpoints: FinancialActivityEndpoints | None = None,
        policy: FinancialActivityReadPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        _validate_scope(account_identity, release_identity)
        self.account_identity = account_identity
        self.release_identity = release_identity
        self.endpoints = (
            FinancialActivityEndpoints() if endpoints is None else endpoints
        )
        self.policy = FinancialActivityReadPolicy() if policy is None else policy
        self.endpoints.validate()
        self.policy.validate()
        self.credentials = credentials
        self.transport = transport
        self.sleeper = sleeper
        self._read_limiter = _TokenBucket(
            capacity=self.policy.read_capacity,
            refill_per_second=self.policy.read_refill_per_second,
            clock=clock,
        )

    def page(
        self,
        *,
        after: datetime,
        until: datetime,
        page_size: int,
        page_token: str | None,
    ) -> FinancialActivityPage:
        after = _aware(after, "after")
        until = _aware(until, "until")
        if after >= until:
            raise ValueError("after must precede until")
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be within [1, 100]")
        query: dict[str, str | int] = {
            "category": "non_trade_activity",
            "after": after.isoformat().replace("+00:00", "Z"),
            "until": until.isoformat().replace("+00:00", "Z"),
            "direction": "asc",
            "page_size": page_size,
        }
        source_cursor = "ROOT"
        if page_token is not None:
            token = page_token.strip()
            if not token:
                raise ValueError("page_token cannot be blank")
            query["page_token"] = token
            source_cursor = token
        document = self._read_json("/v2/account/activities?" + urlencode(query))
        if not isinstance(document, list):
            raise FinancialActivityProtocolError(
                "financial activities response must be a list"
            )
        activities = tuple(
            self._parse_activity(item, source_cursor=source_cursor) for item in document
        )
        ids = [activity.activity_id for activity in activities]
        if len(ids) != len(set(ids)):
            raise FinancialActivityProtocolError(
                "financial activities page contains duplicate ids"
            )
        next_page_token = (
            activities[-1].activity_id if len(activities) == page_size else None
        )
        return FinancialActivityPage(activities, next_page_token)

    def _read_json(self, path: str) -> object:
        delay = self.policy.initial_backoff_seconds
        for attempt in range(1, self.policy.maximum_read_attempts + 1):
            if not self._read_limiter.try_acquire():
                raise FinancialActivityRateLimitExceeded(
                    "local paper read rate limit exceeded"
                )
            try:
                response = self.transport.request(
                    "GET",
                    self.endpoints.rest_base_url.rstrip("/") + path,
                    headers=self.credentials.rest_headers(),
                    body=None,
                    timeout_seconds=self.policy.timeout_seconds,
                )
            except (TimeoutError, OSError) as exc:
                if attempt == self.policy.maximum_read_attempts:
                    raise FinancialActivityRecoveryError(
                        "activity read transport exhausted"
                    ) from exc
                self.sleeper(delay)
                delay = min(
                    self.policy.maximum_backoff_seconds,
                    max(delay * 2, delay),
                )
                continue
            if len(response.body) > self.policy.maximum_response_bytes:
                raise FinancialActivityProtocolError(
                    "broker response exceeds configured size limit"
                )
            if 200 <= response.status < 300:
                try:
                    return json.loads(response.body.decode("utf-8")) if response.body else []
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise FinancialActivityProtocolError(
                        "invalid financial activities JSON"
                    ) from exc
            retryable = response.status in {408, 425, 429} or response.status >= 500
            if retryable and attempt < self.policy.maximum_read_attempts:
                self.sleeper(delay)
                delay = min(
                    self.policy.maximum_backoff_seconds,
                    max(delay * 2, delay),
                )
                continue
            raise FinancialActivityRecoveryError(
                f"financial activities HTTP status {response.status}"
            )
        raise AssertionError("unreachable")

    def _parse_activity(
        self,
        value: object,
        *,
        source_cursor: str,
    ) -> BrokerFinancialActivity:
        if not isinstance(value, Mapping):
            raise FinancialActivityProtocolError("financial activity must be an object")
        activity_type = str(value.get("activity_type", "")).strip().upper()
        if activity_type == "FILL":
            raise FinancialActivityProtocolError(
                "FILL must use the execution accounting path"
            )
        canonical_payload = json.dumps(
            dict(value),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        activity = BrokerFinancialActivity(
            activity_id=str(value.get("id", "")).strip(),
            activity_type=activity_type,
            net_amount=_decimal(value.get("net_amount"), "net_amount"),
            currency=str(value.get("currency", "USD")).strip().upper(),
            symbol=(
                None
                if not str(value.get("symbol", "")).strip()
                else str(value.get("symbol", "")).strip().upper()
            ),
            occurred_at=_activity_time(value),
            account_identity=self.account_identity,
            release_identity=self.release_identity,
            source_cursor=source_cursor,
            canonical_payload=canonical_payload,
        )
        activity.validate()
        return activity


class FinancialActivityProjector:
    """Exactly-once cash projection bound to one account/release scope."""

    def __init__(
        self,
        *,
        store: FinancialActivityStore,
        portfolio: PortfolioStore,
        runtime_ledger: PortfolioLedger,
        account_identity: str,
        release_identity: str,
    ) -> None:
        _validate_scope(account_identity, release_identity)
        self.store = store
        self.portfolio = portfolio
        self.runtime_ledger = runtime_ledger
        self.account_identity = account_identity
        self.release_identity = release_identity

    def project_pending(
        self,
        *,
        occurred_at: datetime,
        limit: int = 1000,
    ) -> tuple[int, int]:
        moment = _aware(occurred_at, "occurred_at")
        projected = 0
        quarantined = 0
        for record in self.store.pending(
            account_identity=self.account_identity,
            release_identity=self.release_identity,
            limit=limit,
        ):
            activity = record.activity
            if (
                activity.account_identity != self.account_identity
                or activity.release_identity != self.release_identity
            ):
                raise FinancialActivityRecoveryError(
                    "financial activity projection scope mismatch"
                )
            classification = _classification(activity)
            if not isinstance(classification, CashAdjustmentKind):
                self.store.quarantine(
                    activity.activity_id,
                    reason=classification,
                    occurred_at=moment,
                )
                quarantined += 1
                continue
            try:
                appended = self.portfolio.append_cash_adjustment(
                    activity_id=activity.activity_id,
                    amount=activity.net_amount,
                    kind=classification,
                    occurred_at=activity.occurred_at,
                )
                self.runtime_ledger.apply_cash_adjustment(
                    activity_id=activity.activity_id,
                    amount=activity.net_amount,
                    kind=classification,
                )
                self.store.mark_projected(
                    activity.activity_id,
                    portfolio_event_id=f"broker-cash:{activity.activity_id}",
                    occurred_at=moment,
                )
                if appended:
                    projected += 1
            except ValueError as exc:
                self.store.quarantine(
                    activity.activity_id,
                    reason=f"CASH_PROJECTION_REJECTED:{exc}",
                    occurred_at=moment,
                )
                quarantined += 1
        return projected, quarantined


class FinancialActivityRecoveryService:
    """Restart-safe recovery with durable high-water and bounded overlap."""

    def __init__(
        self,
        *,
        source: FinancialActivitySource,
        store: FinancialActivityStore,
        projector: FinancialActivityProjector,
        account_identity: str,
        release_identity: str,
        bootstrap_after: datetime,
        policy: FinancialActivityRecoveryPolicy | None = None,
    ) -> None:
        _validate_scope(account_identity, release_identity)
        if (
            projector.account_identity != account_identity
            or projector.release_identity != release_identity
        ):
            raise ValueError("financial projector scope must match recovery scope")
        self.source = source
        self.store = store
        self.projector = projector
        self.account_identity = account_identity
        self.release_identity = release_identity
        self.bootstrap_after = _aware(bootstrap_after, "bootstrap_after")
        self.policy = FinancialActivityRecoveryPolicy() if policy is None else policy
        self.policy.validate()

    def recover(
        self,
        *,
        until: datetime,
        observed_at: datetime,
    ) -> FinancialActivityRecoveryResult:
        until = _aware(until, "until")
        observed_at = _aware(observed_at, "observed_at")
        if until > observed_at:
            raise ValueError("until cannot exceed observed_at")
        prior = self.store.recovery_state(
            account_identity=self.account_identity,
            release_identity=self.release_identity,
        )
        if prior is None:
            after = self.bootstrap_after
        else:
            after = max(
                self.bootstrap_after,
                prior.recovered_through - self.policy.overlap,
            )
        if after >= until:
            projected, quarantined = self.projector.project_pending(
                occurred_at=observed_at
            )
            readiness = financial_activity_readiness(
                self.store,
                account_identity=self.account_identity,
                release_identity=self.release_identity,
                now=observed_at,
                maximum_age=self.policy.maximum_window,
            )
            return FinancialActivityRecoveryResult(
                acquisition_complete=True,
                ready=readiness.ready,
                recovered_through=prior.recovered_through if prior else None,
                pages_read=0,
                activities_seen=0,
                projected=projected,
                duplicates=0,
                quarantined=quarantined,
                pending=readiness.pending,
                reasons=readiness.reasons,
            )

        window_until = min(until, after + self.policy.maximum_window)
        page_token: str | None = None
        pages_read = 0
        seen = 0
        duplicates = 0
        reasons: set[str] = set()
        while pages_read < self.policy.maximum_pages:
            page = self.source.page(
                after=after,
                until=window_until,
                page_size=self.policy.page_size,
                page_token=page_token,
            )
            pages_read += 1
            for activity in page.activities:
                if activity.account_identity != self.account_identity:
                    raise FinancialActivityRecoveryError(
                        "activity account identity mismatch"
                    )
                if activity.release_identity != self.release_identity:
                    raise FinancialActivityRecoveryError(
                        "activity release identity mismatch"
                    )
                if seen >= self.policy.maximum_activities:
                    reasons.add("ACTIVITY_LIMIT_REACHED")
                    break
                before_pending = self.store.pending_count(
                    account_identity=self.account_identity,
                    release_identity=self.release_identity,
                )
                record = self.store.ingest(activity, ingested_at=observed_at)
                seen += 1
                after_pending = self.store.pending_count(
                    account_identity=self.account_identity,
                    release_identity=self.release_identity,
                )
                if record.state.value != "PENDING" or after_pending == before_pending:
                    duplicates += 1
            if reasons:
                break
            if page.next_page_token is None:
                page_token = None
                break
            if page.next_page_token == page_token:
                raise FinancialActivityRecoveryError(
                    "financial activity page token did not advance"
                )
            page_token = page.next_page_token
        else:
            if page_token is not None:
                reasons.add("PAGE_LIMIT_REACHED")

        projected, newly_quarantined = self.projector.project_pending(
            occurred_at=observed_at
        )
        acquisition_complete = not reasons and page_token is None
        if acquisition_complete:
            self.store.advance_recovery(
                account_identity=self.account_identity,
                release_identity=self.release_identity,
                recovered_through=window_until,
                occurred_at=observed_at,
            )
            if window_until < until:
                reasons.add("MORE_HISTORY_REQUIRED")
                acquisition_complete = False

        readiness = financial_activity_readiness(
            self.store,
            account_identity=self.account_identity,
            release_identity=self.release_identity,
            now=observed_at,
            maximum_age=self.policy.maximum_window,
        )
        reasons.update(readiness.reasons)
        return FinancialActivityRecoveryResult(
            acquisition_complete=acquisition_complete,
            ready=not reasons and readiness.ready,
            recovered_through=readiness.recovered_through,
            pages_read=pages_read,
            activities_seen=seen,
            projected=projected,
            duplicates=duplicates,
            quarantined=newly_quarantined,
            pending=readiness.pending,
            reasons=tuple(sorted(reasons)),
        )


def financial_activity_readiness(
    store: FinancialActivityStore,
    *,
    account_identity: str,
    release_identity: str,
    now: datetime,
    maximum_age: timedelta,
) -> FinancialActivityReadiness:
    _validate_scope(account_identity, release_identity)
    moment = _aware(now, "now")
    if maximum_age <= timedelta(0):
        raise ValueError("maximum_age must be positive")
    state = store.recovery_state(
        account_identity=account_identity,
        release_identity=release_identity,
    )
    pending = store.pending_count(
        account_identity=account_identity,
        release_identity=release_identity,
    )
    quarantined = store.quarantined_count(
        account_identity=account_identity,
        release_identity=release_identity,
    )
    reasons: set[str] = set()
    recovered_through: datetime | None = None
    if state is None:
        reasons.add("FINANCIAL_ACTIVITY_RECOVERY_REQUIRED")
    else:
        recovered_through = state.recovered_through
        if recovered_through > moment:
            reasons.add("FINANCIAL_ACTIVITY_RECOVERY_CLOCK_INVALID")
        elif moment - recovered_through > maximum_age:
            reasons.add("FINANCIAL_ACTIVITY_RECOVERY_STALE")
    if pending:
        reasons.add("FINANCIAL_ACTIVITY_PROJECTION_PENDING")
    if quarantined:
        reasons.add("FINANCIAL_ACTIVITY_QUARANTINED")
    return FinancialActivityReadiness(
        ready=not reasons,
        reasons=tuple(sorted(reasons)),
        recovered_through=recovered_through,
        pending=pending,
        quarantined=quarantined,
    )


def _classification(activity: BrokerFinancialActivity) -> CashAdjustmentKind | str:
    if activity.currency != "USD":
        return "NON_USD_FINANCIAL_ACTIVITY_UNSUPPORTED"
    if activity.net_amount == 0:
        return "ZERO_NET_FINANCIAL_ACTIVITY_UNSUPPORTED"
    if activity.activity_type in _EXTERNAL_FLOW_TYPES:
        if activity.activity_type == "CSD" and activity.net_amount <= 0:
            return "CSD_SIGN_INVALID"
        if activity.activity_type == "CSW" and activity.net_amount >= 0:
            return "CSW_SIGN_INVALID"
        return CashAdjustmentKind.EXTERNAL_FLOW
    if activity.activity_type in _FEE_TYPES:
        if activity.net_amount >= 0:
            return "FEE_SIGN_INVALID"
        return CashAdjustmentKind.FEE
    if activity.activity_type in _INCOME_TYPES:
        if activity.activity_type == "PTR" and activity.net_amount <= 0:
            return "PTR_SIGN_INVALID"
        return CashAdjustmentKind.INCOME
    return f"UNSUPPORTED_FINANCIAL_ACTIVITY:{activity.activity_type}"


def _decimal(value: object, field: str) -> Decimal:
    if value is None or value == "":
        raise FinancialActivityProtocolError(
            f"missing financial activity decimal: {field}"
        )
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise FinancialActivityProtocolError(
            f"invalid financial activity decimal: {field}"
        ) from exc
    if not result.is_finite():
        raise FinancialActivityProtocolError(
            f"non-finite financial activity decimal: {field}"
        )
    return result


def _activity_time(value: Mapping[object, object]) -> datetime:
    raw = value.get("transaction_time") or value.get("date")
    if not isinstance(raw, str) or not raw.strip():
        raise FinancialActivityProtocolError("financial activity date is required")
    text = raw.strip()
    try:
        if len(text) == 10:
            parsed_date = date.fromisoformat(text)
            return datetime.combine(parsed_date, datetime_time.min, tzinfo=UTC)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FinancialActivityProtocolError("invalid financial activity date") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FinancialActivityProtocolError(
            "financial activity date must be timezone-aware"
        )
    return parsed.astimezone(UTC)


def _aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _validate_scope(account_identity: str, release_identity: str) -> None:
    if not account_identity.strip() or not release_identity.strip():
        raise ValueError("account_identity and release_identity are required")
