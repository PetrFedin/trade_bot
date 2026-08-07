from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import urlencode

from app.domain.trading import Side
from app.execution.trade_fills import (
    ExactBrokerFill,
    FillAccountingResult,
    PaperTradeFillAccounting,
)
from app.oms.indexed import IndexedOmsStore
from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaPaperCredentialsV100,
    AlpacaPaperEndpointsV100,
    AlpacaPaperPolicyV100,
    AlpacaPaperProtocolError,
    AlpacaPaperRateLimitExceeded,
    HttpTransportV100,
    TokenBucketV100,
)


class FillActivityRecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class AlpacaFillActivity:
    activity_id: str
    broker_order_id: str
    symbol: str
    side: Side
    cumulative_quantity: Decimal
    quantity: Decimal
    price: Decimal
    occurred_at: datetime
    activity_kind: str

    def validate(self) -> None:
        for field, value in (
            ("activity_id", self.activity_id),
            ("broker_order_id", self.broker_order_id),
            ("symbol", self.symbol),
            ("activity_kind", self.activity_kind),
        ):
            if not value.strip():
                raise FillActivityRecoveryError(f"{field} is required")
        if self.symbol != self.symbol.upper():
            raise FillActivityRecoveryError("symbol must be uppercase")
        if self.activity_kind not in {"fill", "partial_fill"}:
            raise FillActivityRecoveryError("unsupported fill activity kind")
        for field, value in (
            ("cumulative_quantity", self.cumulative_quantity),
            ("quantity", self.quantity),
            ("price", self.price),
        ):
            if not value.is_finite() or value <= 0:
                raise FillActivityRecoveryError(f"{field} must be positive and finite")
        if self.quantity > self.cumulative_quantity:
            raise FillActivityRecoveryError("fill quantity exceeds cumulative quantity")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise FillActivityRecoveryError("occurred_at must be timezone-aware")


@dataclass(frozen=True)
class FillActivityPage:
    activities: tuple[AlpacaFillActivity, ...]
    next_page_token: str | None


class FillActivitySource(Protocol):
    def page(
        self,
        *,
        after: datetime,
        until: datetime,
        page_size: int,
        page_token: str | None,
    ) -> FillActivityPage: ...


@dataclass(frozen=True)
class FillBackfillPolicy:
    maximum_window: timedelta = timedelta(days=7)
    maximum_pages: int = 20
    maximum_activities: int = 1000
    page_size: int = 100

    def validate(self) -> None:
        if self.maximum_window <= timedelta(0):
            raise ValueError("maximum_window must be positive")
        if self.maximum_pages < 1:
            raise ValueError("maximum_pages must be positive")
        if self.maximum_activities < 1:
            raise ValueError("maximum_activities must be positive")
        if self.page_size < 1 or self.page_size > 100:
            raise ValueError("page_size must be within [1, 100]")


@dataclass(frozen=True)
class FillBackfillResult:
    complete: bool
    pages_read: int
    activities_seen: int
    portfolio_events_appended: int
    duplicate_portfolio_events: int
    oms_advances: int
    unresolved_broker_order_ids: tuple[str, ...]
    reasons: tuple[str, ...]


class AlpacaPaperFillActivityReader:
    """Bounded GET-only client for Alpaca Paper FILL account activities."""

    def __init__(
        self,
        *,
        credentials: AlpacaPaperCredentialsV100,
        transport: HttpTransportV100,
        endpoints: AlpacaPaperEndpointsV100 = AlpacaPaperEndpointsV100(),
        policy: AlpacaPaperPolicyV100 = AlpacaPaperPolicyV100(),
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        endpoints.validate()
        policy.validate()
        self.credentials = credentials
        self.transport = transport
        self.endpoints = endpoints
        self.policy = policy
        self.sleeper = sleeper
        self._read_limiter = TokenBucketV100(
            capacity=policy.read_capacity,
            refill_per_second=policy.read_refill_per_second,
            clock=clock,
        )

    def page(
        self,
        *,
        after: datetime,
        until: datetime,
        page_size: int,
        page_token: str | None,
    ) -> FillActivityPage:
        after = self._aware_utc(after, "after")
        until = self._aware_utc(until, "until")
        if after >= until:
            raise ValueError("after must precede until")
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be within [1, 100]")
        query: dict[str, str | int] = {
            "after": after.isoformat().replace("+00:00", "Z"),
            "until": until.isoformat().replace("+00:00", "Z"),
            "direction": "asc",
            "page_size": page_size,
        }
        if page_token is not None:
            token = page_token.strip()
            if not token:
                raise ValueError("page_token cannot be blank")
            query["page_token"] = token
        path = "/v2/account/activities/FILL?" + urlencode(query)
        document = self._read_json(path)
        if not isinstance(document, list):
            raise AlpacaPaperProtocolError("fill activities response must be a list")
        activities = tuple(self._parse_activity(item) for item in document)
        activity_ids = [activity.activity_id for activity in activities]
        if len(activity_ids) != len(set(activity_ids)):
            raise AlpacaPaperProtocolError("fill activities page contains duplicate ids")
        next_page_token = activities[-1].activity_id if len(activities) == page_size else None
        return FillActivityPage(activities=activities, next_page_token=next_page_token)

    def _read_json(self, path: str) -> object:
        delay = self.policy.initial_backoff_seconds
        for attempt in range(1, self.policy.maximum_read_attempts + 1):
            if not self._read_limiter.try_acquire():
                raise AlpacaPaperRateLimitExceeded("local paper read rate limit exceeded")
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
                    raise FillActivityRecoveryError("activity read transport exhausted") from exc
                self.sleeper(delay)
                delay = min(self.policy.maximum_backoff_seconds, max(delay * 2, delay))
                continue
            if len(response.body) > self.policy.maximum_response_bytes:
                raise AlpacaPaperProtocolError("broker response exceeds configured size limit")
            if 200 <= response.status < 300:
                try:
                    return json.loads(response.body.decode("utf-8")) if response.body else []
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise AlpacaPaperProtocolError(
                        "invalid fill activities JSON response"
                    ) from exc
            retryable = response.status in {408, 425, 429} or response.status >= 500
            if retryable and attempt < self.policy.maximum_read_attempts:
                self.sleeper(delay)
                delay = min(self.policy.maximum_backoff_seconds, max(delay * 2, delay))
                continue
            raise FillActivityRecoveryError(f"fill activities HTTP status {response.status}")
        raise AssertionError("unreachable")

    @classmethod
    def _parse_activity(cls, value: object) -> AlpacaFillActivity:
        if not isinstance(value, Mapping):
            raise AlpacaPaperProtocolError("fill activity must be an object")
        activity_type = str(value.get("activity_type", "")).upper()
        if activity_type != "FILL":
            raise AlpacaPaperProtocolError("unexpected account activity type")
        kind = str(value.get("type", "fill")).lower()
        try:
            side = Side(str(value.get("side", "")).upper())
        except ValueError as exc:
            raise AlpacaPaperProtocolError("unsupported fill activity side") from exc
        activity = AlpacaFillActivity(
            activity_id=str(value.get("id", "")).strip(),
            broker_order_id=str(value.get("order_id", "")).strip(),
            symbol=str(value.get("symbol", "")).strip().upper(),
            side=side,
            cumulative_quantity=cls._decimal(value.get("cum_qty"), "cum_qty"),
            quantity=cls._decimal(value.get("qty"), "qty"),
            price=cls._decimal(value.get("price"), "price"),
            occurred_at=cls._timestamp(value.get("transaction_time")),
            activity_kind=kind,
        )
        activity.validate()
        return activity

    @staticmethod
    def _decimal(value: object, field: str) -> Decimal:
        if value is None or value == "":
            raise AlpacaPaperProtocolError(f"missing fill activity decimal: {field}")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise AlpacaPaperProtocolError(f"invalid fill activity decimal: {field}") from exc
        if not result.is_finite():
            raise AlpacaPaperProtocolError(f"non-finite fill activity decimal: {field}")
        return result

    @staticmethod
    def _timestamp(value: object) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise AlpacaPaperProtocolError("fill activity transaction_time is required")
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AlpacaPaperProtocolError("invalid fill activity transaction_time") from exc
        if result.tzinfo is None or result.utcoffset() is None:
            raise AlpacaPaperProtocolError(
                "fill activity transaction_time must be timezone-aware"
            )
        return result.astimezone(UTC)

    @staticmethod
    def _aware_utc(value: datetime, field: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(UTC)


class PaperFillBackfillService:
    """Recover missed fills through GET-only activity history and strict accounting."""

    def __init__(
        self,
        *,
        source: FillActivitySource,
        oms: IndexedOmsStore,
        accounting: PaperTradeFillAccounting,
        policy: FillBackfillPolicy | None = None,
    ) -> None:
        self.policy = FillBackfillPolicy() if policy is None else policy
        self.policy.validate()
        self.source = source
        self.oms = oms
        self.accounting = accounting

    def recover(self, *, after: datetime, until: datetime) -> FillBackfillResult:
        after = self._aware(after, "after")
        until = self._aware(until, "until")
        if after >= until:
            raise ValueError("after must precede until")
        if until - after > self.policy.maximum_window:
            raise ValueError("fill backfill window exceeds configured maximum")

        page_token: str | None = None
        pages_read = 0
        collected: list[AlpacaFillActivity] = []
        seen_activity_ids: set[str] = set()
        reasons: set[str] = set()

        while pages_read < self.policy.maximum_pages:
            page = self.source.page(
                after=after,
                until=until,
                page_size=self.policy.page_size,
                page_token=page_token,
            )
            pages_read += 1
            for activity in page.activities:
                if activity.activity_id in seen_activity_ids:
                    raise FillActivityRecoveryError("duplicate fill activity across pages")
                if len(collected) >= self.policy.maximum_activities:
                    reasons.add("ACTIVITY_LIMIT_REACHED")
                    return self._result(
                        pages_read=pages_read,
                        activities_seen=len(collected),
                        appended=0,
                        duplicates=0,
                        oms_advances=0,
                        unresolved=set(),
                        reasons=reasons,
                    )
                seen_activity_ids.add(activity.activity_id)
                collected.append(activity)
            if page.next_page_token is None:
                break
            if page.next_page_token == page_token:
                raise FillActivityRecoveryError(
                    "fill activity pagination token did not advance"
                )
            page_token = page.next_page_token
        else:
            if page_token is not None:
                reasons.add("PAGE_LIMIT_REACHED")

        if reasons:
            return self._result(
                pages_read=pages_read,
                activities_seen=len(collected),
                appended=0,
                duplicates=0,
                oms_advances=0,
                unresolved=set(),
                reasons=reasons,
            )

        appended = 0
        duplicates = 0
        oms_advances = 0
        unresolved: set[str] = set()
        ordered = sorted(
            collected,
            key=lambda activity: (activity.occurred_at, activity.activity_id),
        )
        for activity in ordered:
            record = self.oms.get_by_broker_order_id(activity.broker_order_id)
            if record is None:
                unresolved.add(activity.broker_order_id)
                continue
            exact_fill = ExactBrokerFill(
                execution_id=f"activity:{activity.activity_id}",
                broker_order_id=activity.broker_order_id,
                client_order_id=record.client_order_id,
                symbol=activity.symbol,
                side=activity.side,
                order_quantity=record.quantity,
                cumulative_quantity=activity.cumulative_quantity,
                quantity=activity.quantity,
                price=activity.price,
                occurred_at=activity.occurred_at,
            )
            result: FillAccountingResult = self.accounting.apply(
                record.intent_id,
                exact_fill,
            )
            if result.portfolio_event_appended:
                appended += 1
            else:
                duplicates += 1
            if result.oms_advanced:
                oms_advances += 1

        if unresolved:
            reasons.add("UNRESOLVED_BROKER_ORDERS")
        return self._result(
            pages_read=pages_read,
            activities_seen=len(collected),
            appended=appended,
            duplicates=duplicates,
            oms_advances=oms_advances,
            unresolved=unresolved,
            reasons=reasons,
        )

    @staticmethod
    def _aware(value: datetime, field: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{field} must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _result(
        *,
        pages_read: int,
        activities_seen: int,
        appended: int,
        duplicates: int,
        oms_advances: int,
        unresolved: set[str],
        reasons: set[str],
    ) -> FillBackfillResult:
        return FillBackfillResult(
            complete=not reasons,
            pages_read=pages_read,
            activities_seen=activities_seen,
            portfolio_events_appended=appended,
            duplicate_portfolio_events=duplicates,
            oms_advances=oms_advances,
            unresolved_broker_order_ids=tuple(sorted(unresolved)),
            reasons=tuple(sorted(reasons)),
        )
