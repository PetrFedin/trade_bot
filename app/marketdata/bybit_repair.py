from __future__ import annotations

import json
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from app.marketdata.bybit_public import BybitPublicLinearSubscription
from app.marketdata.continuity import (
    OperationalContinuityCheckpoint,
    OperationalContinuityStore,
    OperationalRepairBarStore,
    continuity_checkpoint_id,
)
from app.marketdata.operational import OperationalBar, OperationalMarketDataStore

BYBIT_PUBLIC_REST_BASE = "https://api.bybit.com"
BYBIT_PROVIDER = "BYBIT"
BYBIT_LINEAR_VENUE = "BYBIT_LINEAR"
_CONTINUITY_EVIDENCE_SOURCE = "BYBIT_V5_MARKET_KLINE_GET"


class BybitRepairError(RuntimeError):
    pass


class BybitRepairProtocolError(BybitRepairError):
    pass


@dataclass(frozen=True)
class BybitRepairPolicy:
    timeout_seconds: float = 10.0
    maximum_repair_bars: int = 1000
    maximum_response_bytes: int = 2_000_000
    maximum_server_age_seconds: float = 90.0
    maximum_server_future_skew_seconds: float = 2.0

    def validate(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= self.maximum_repair_bars <= 1000:
            raise ValueError("maximum_repair_bars must be in [1, 1000]")
        if self.maximum_response_bytes < 1024:
            raise ValueError("maximum_response_bytes is too small")
        if self.maximum_server_age_seconds <= 0:
            raise ValueError("maximum_server_age_seconds must be positive")
        if self.maximum_server_future_skew_seconds < 0:
            raise ValueError("maximum_server_future_skew_seconds must be non-negative")


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


class HttpTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> HttpResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class StdlibBybitPublicHttpTransport:
    """TLS-verifying, redirect-rejecting and GET-only public market transport."""

    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        self.context = ssl.create_default_context() if context is None else context
        if self.context.verify_mode != ssl.CERT_REQUIRED or not self.context.check_hostname:
            raise ValueError("TLS hostname and certificate verification must remain enabled")
        self.opener = build_opener(_NoRedirect(), HTTPSHandler(context=self.context))

    def get(
        self,
        url: str,
        *,
        timeout_seconds: float,
        maximum_response_bytes: int,
    ) -> HttpResponse:
        split = urlsplit(url)
        if (
            split.scheme != "https"
            or split.netloc != "api.bybit.com"
            or split.path != "/v5/market/kline"
        ):
            raise BybitRepairProtocolError("Bybit repair transport rejected non-allowlisted URL")
        if split.username is not None or split.password is not None or split.fragment:
            raise BybitRepairProtocolError("Bybit repair URL contains forbidden authority data")
        if timeout_seconds <= 0 or maximum_response_bytes < 1:
            raise ValueError("invalid HTTP transport policy")
        request = Request(
            url=url,
            method="GET",
            headers={
                "Accept": "application/json",
                "User-Agent": "astra-f22c-bybit-continuity/7.39.0",
            },
        )
        try:
            with self.opener.open(request, timeout=timeout_seconds) as response:
                body = response.read(maximum_response_bytes + 1)
                if len(body) > maximum_response_bytes:
                    raise BybitRepairProtocolError("Bybit repair response exceeded size limit")
                return HttpResponse(status=int(response.status), body=body)
        except HTTPError as exc:
            if 300 <= exc.code < 400:
                raise BybitRepairProtocolError("HTTP redirects are forbidden") from exc
            body = exc.read(maximum_response_bytes + 1)
            return HttpResponse(status=int(exc.code), body=body[:maximum_response_bytes])
        except socket.timeout as exc:
            raise TimeoutError("Bybit repair request timed out") from exc
        except URLError as exc:
            if isinstance(exc.reason, socket.timeout):
                raise TimeoutError("Bybit repair request timed out") from exc
            raise OSError(f"Bybit repair transport failure: {type(exc.reason).__name__}") from exc


class BybitPublicKlineClient:
    """Read-only bounded parser for completed Bybit linear klines."""

    def __init__(
        self,
        *,
        subscription: BybitPublicLinearSubscription,
        transport: HttpTransport,
        policy: BybitRepairPolicy | None = None,
    ) -> None:
        resolved = BybitRepairPolicy() if policy is None else policy
        subscription.validate()
        resolved.validate()
        self.subscription = subscription
        self.transport = transport
        self.policy = resolved

    def fetch_closed_range(
        self,
        *,
        first_open_time: datetime,
        last_open_time: datetime,
        observed_at: datetime,
    ) -> tuple[OperationalBar, ...]:
        first_open = _aware(first_open_time, "first_open_time")
        last_open = _aware(last_open_time, "last_open_time")
        observed = _aware(observed_at, "observed_at")
        interval = timedelta(seconds=self.subscription.interval_seconds)
        if first_open > last_open:
            raise ValueError("first_open_time cannot follow last_open_time")
        _require_aligned(first_open, self.subscription.interval_seconds)
        _require_aligned(last_open, self.subscription.interval_seconds)
        count = int((last_open - first_open) / interval) + 1
        if count > self.policy.maximum_repair_bars:
            raise BybitRepairError("REPAIR_RANGE_EXCEEDS_POLICY")
        if last_open + interval > observed:
            raise BybitRepairError("REPAIR_RANGE_INCLUDES_UNCLOSED_BAR")

        interval_ms = self.subscription.interval_seconds * 1000
        start_ms = _milliseconds(first_open)
        end_ms = _milliseconds(last_open) + interval_ms - 1
        query = urlencode(
            {
                "category": "linear",
                "symbol": self.subscription.symbol,
                "interval": self.subscription.interval,
                "start": start_ms,
                "end": end_ms,
                "limit": count,
            }
        )
        response = self.transport.get(
            f"{BYBIT_PUBLIC_REST_BASE}/v5/market/kline?{query}",
            timeout_seconds=self.policy.timeout_seconds,
            maximum_response_bytes=self.policy.maximum_response_bytes,
        )
        if response.status != 200:
            raise BybitRepairError(f"BYBIT_REPAIR_HTTP_{response.status}")
        payload = _decode_response(response.body)
        server_at = _milliseconds_timestamp(payload.get("time"), "time")
        self._validate_server_clock(server_at=server_at, observed_at=observed)
        result = payload.get("result")
        if not isinstance(result, dict):
            raise BybitRepairProtocolError("Bybit repair result must be an object")
        if result.get("category") != "linear":
            raise BybitRepairProtocolError("Bybit repair category mismatch")
        if result.get("symbol") != self.subscription.symbol:
            raise BybitRepairProtocolError("Bybit repair symbol mismatch")
        raw_rows = result.get("list")
        if not isinstance(raw_rows, list):
            raise BybitRepairProtocolError("Bybit repair list must be an array")

        bars = tuple(
            sorted(
                (self._bar(row, observed_at=observed) for row in raw_rows),
                key=lambda value: value.open_time,
            )
        )
        expected = tuple(first_open + index * interval for index in range(count))
        actual = tuple(bar.open_time for bar in bars)
        if actual != expected:
            raise BybitRepairProtocolError("BYBIT_REPAIR_RANGE_NOT_CONTIGUOUS")
        return bars

    def _bar(self, raw: object, *, observed_at: datetime) -> OperationalBar:
        if not isinstance(raw, list) or len(raw) != 7:
            raise BybitRepairProtocolError("Bybit repair kline row must contain seven fields")
        start_ms = _integer_string(raw[0], "startTime")
        open_time = _from_milliseconds(start_ms)
        interval = timedelta(seconds=self.subscription.interval_seconds)
        close_time = open_time + interval
        if close_time > observed_at:
            raise BybitRepairProtocolError("Bybit repair returned an unclosed candle")
        _non_negative_decimal(raw[6], "turnover")
        source_end = close_time - timedelta(milliseconds=1)
        bar = OperationalBar(
            provider=BYBIT_PROVIDER,
            venue=BYBIT_LINEAR_VENUE,
            symbol=self.subscription.symbol,
            interval_seconds=self.subscription.interval_seconds,
            open_time=open_time,
            close_time=close_time,
            source_timestamp=source_end,
            received_at=observed_at,
            source_event_id=(
                f"{self.subscription.topic}:{start_ms}:"
                f"{start_ms + self.subscription.interval_seconds * 1000 - 1}"
            ),
            is_final=True,
            open=_positive_decimal(raw[1], "open"),
            high=_positive_decimal(raw[2], "high"),
            low=_positive_decimal(raw[3], "low"),
            close=_positive_decimal(raw[4], "close"),
            volume=_non_negative_decimal(raw[5], "volume"),
            revision=0,
        )
        try:
            bar.validate()
        except ValueError as exc:
            raise BybitRepairProtocolError("invalid Bybit repair kline") from exc
        return bar

    def _validate_server_clock(
        self,
        *,
        server_at: datetime,
        observed_at: datetime,
    ) -> None:
        if server_at - observed_at > timedelta(
            seconds=self.policy.maximum_server_future_skew_seconds
        ):
            raise BybitRepairProtocolError("BYBIT_REPAIR_SERVER_CLOCK_IN_FUTURE")
        if observed_at - server_at > timedelta(
            seconds=self.policy.maximum_server_age_seconds
        ):
            raise BybitRepairProtocolError("BYBIT_REPAIR_SERVER_DATA_STALE")


@dataclass(frozen=True)
class BybitContinuityRepairResult:
    checkpoint: OperationalContinuityCheckpoint
    expected_bars: int
    repaired_bars: int
    existing_bars: int

    def validate(self) -> None:
        self.checkpoint.validate()
        if min(self.expected_bars, self.repaired_bars, self.existing_bars) < 0:
            raise ValueError("repair counters must be non-negative")
        if self.repaired_bars + self.existing_bars != self.expected_bars:
            raise ValueError("repair counters disagree with expected bars")


class BybitContinuityRepairService:
    """One bounded continuity proof/repair pass; no reconnect loop and no broker authority."""

    def __init__(
        self,
        *,
        subscription: BybitPublicLinearSubscription,
        marketdata: OperationalMarketDataStore,
        repair_store: OperationalRepairBarStore,
        continuity: OperationalContinuityStore,
        client: BybitPublicKlineClient,
    ) -> None:
        subscription.validate()
        if client.subscription != subscription:
            raise ValueError("repair client subscription mismatch")
        self.subscription = subscription
        self.marketdata = marketdata
        self.repair_store = repair_store
        self.continuity = continuity
        self.client = client

    def repair(
        self,
        *,
        observed_at: datetime,
        bootstrap_open_time: datetime | None = None,
    ) -> BybitContinuityRepairResult:
        observed = _aware(observed_at, "observed_at")
        latest = self.continuity.latest(
            provider=BYBIT_PROVIDER,
            venue=BYBIT_LINEAR_VENUE,
            symbol=self.subscription.symbol,
            interval_seconds=self.subscription.interval_seconds,
        )
        if latest is None:
            if bootstrap_open_time is None:
                raise BybitRepairError("CONTINUITY_BOOTSTRAP_REQUIRED")
            first_open = _aware(bootstrap_open_time, "bootstrap_open_time")
            _require_aligned(first_open, self.subscription.interval_seconds)
            previous_checkpoint_id = None
        else:
            if bootstrap_open_time is not None:
                raise BybitRepairError("BOOTSTRAP_FORBIDDEN_AFTER_CONTINUITY_EXISTS")
            first_open = latest.through_close_time
            previous_checkpoint_id = latest.checkpoint_id

        last_open = _last_completed_open(
            observed_at=observed,
            interval_seconds=self.subscription.interval_seconds,
        )
        if first_open > last_open:
            if latest is None:
                raise BybitRepairError("NO_CLOSED_BAR_AVAILABLE_FOR_BOOTSTRAP")
            result = BybitContinuityRepairResult(
                checkpoint=latest,
                expected_bars=0,
                repaired_bars=0,
                existing_bars=0,
            )
            result.validate()
            return result

        interval = timedelta(seconds=self.subscription.interval_seconds)
        expected_count = int((last_open - first_open) / interval) + 1
        if expected_count > self.client.policy.maximum_repair_bars:
            raise BybitRepairError("REPAIR_RANGE_EXCEEDS_POLICY")
        fetched = self.client.fetch_closed_range(
            first_open_time=first_open,
            last_open_time=last_open,
            observed_at=observed,
        )
        if len(fetched) != expected_count:
            raise BybitRepairProtocolError("BYBIT_REPAIR_RANGE_COUNT_MISMATCH")

        through_close = fetched[-1].close_time
        existing = self.marketdata.recent_bars(
            provider=BYBIT_PROVIDER,
            venue=BYBIT_LINEAR_VENUE,
            symbol=self.subscription.symbol,
            interval_seconds=self.subscription.interval_seconds,
            through_close_time=through_close,
            limit=expected_count,
        )
        existing_by_open = {
            bar.open_time: bar for bar in existing if bar.open_time >= first_open
        }
        for bar in fetched:
            prior = existing_by_open.get(bar.open_time)
            if prior is not None and not _same_economics(prior, bar):
                raise BybitRepairProtocolError("BYBIT_REPAIR_OVERLAP_ECONOMICS_MISMATCH")

        repaired_count = 0
        existing_count = 0
        for bar in fetched:
            if bar.open_time in existing_by_open:
                existing_count += 1
                continue
            if self.repair_store.record_without_decision(bar, recorded_at=observed):
                repaired_count += 1
            else:
                existing_count += 1

        durable = self.marketdata.recent_bars(
            provider=BYBIT_PROVIDER,
            venue=BYBIT_LINEAR_VENUE,
            symbol=self.subscription.symbol,
            interval_seconds=self.subscription.interval_seconds,
            through_close_time=through_close,
            limit=expected_count,
        )
        durable_by_open = {
            bar.open_time: bar for bar in durable if bar.open_time >= first_open
        }
        if tuple(sorted(durable_by_open)) != tuple(bar.open_time for bar in fetched):
            raise BybitRepairError("DURABLE_CONTINUITY_NOT_PROVEN")
        for bar in fetched:
            if not _same_economics(durable_by_open[bar.open_time], bar):
                raise BybitRepairError("DURABLE_CONTINUITY_ECONOMICS_MISMATCH")

        through_bar = durable_by_open[fetched[-1].open_time]
        checkpoint_id = continuity_checkpoint_id(
            previous_checkpoint_id=previous_checkpoint_id,
            provider=BYBIT_PROVIDER,
            venue=BYBIT_LINEAR_VENUE,
            symbol=self.subscription.symbol,
            interval_seconds=self.subscription.interval_seconds,
            through_bar_id=through_bar.bar_id,
            through_close_time=through_bar.close_time,
            evidence_source=_CONTINUITY_EVIDENCE_SOURCE,
        )
        checkpoint = OperationalContinuityCheckpoint(
            checkpoint_id=checkpoint_id,
            previous_checkpoint_id=previous_checkpoint_id,
            provider=BYBIT_PROVIDER,
            venue=BYBIT_LINEAR_VENUE,
            symbol=self.subscription.symbol,
            interval_seconds=self.subscription.interval_seconds,
            through_bar_id=through_bar.bar_id,
            through_close_time=through_bar.close_time,
            established_at=observed,
            evidence_source=_CONTINUITY_EVIDENCE_SOURCE,
        )
        self.continuity.append(checkpoint)
        result = BybitContinuityRepairResult(
            checkpoint=checkpoint,
            expected_bars=expected_count,
            repaired_bars=repaired_count,
            existing_bars=existing_count,
        )
        result.validate()
        return result


def _same_economics(left: OperationalBar, right: OperationalBar) -> bool:
    return (
        left.provider == right.provider
        and left.venue == right.venue
        and left.symbol == right.symbol
        and left.interval_seconds == right.interval_seconds
        and left.open_time == right.open_time
        and left.close_time == right.close_time
        and left.open == right.open
        and left.high == right.high
        and left.low == right.low
        and left.close == right.close
        and left.volume == right.volume
    )


def _last_completed_open(*, observed_at: datetime, interval_seconds: int) -> datetime:
    observed = _aware(observed_at, "observed_at")
    if interval_seconds < 1:
        raise ValueError("interval_seconds must be positive")
    epoch_seconds = int(observed.timestamp())
    boundary = epoch_seconds - (epoch_seconds % interval_seconds)
    return datetime.fromtimestamp(boundary - interval_seconds, tz=UTC)


def _require_aligned(value: datetime, interval_seconds: int) -> None:
    moment = _aware(value, "bar open time")
    if interval_seconds < 1:
        raise ValueError("interval_seconds must be positive")
    if moment.microsecond != 0 or int(moment.timestamp()) % interval_seconds != 0:
        raise ValueError("bar open time is not aligned to interval")


def _milliseconds(value: datetime) -> int:
    moment = _aware(value, "timestamp")
    return int(moment.timestamp() * 1000)


def _from_milliseconds(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def _milliseconds_timestamp(value: object, field: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BybitRepairProtocolError(f"Bybit repair {field} must be an integer")
    return _from_milliseconds(value)


def _integer_string(value: object, field: str) -> int:
    if not isinstance(value, str) or not value.isdigit():
        raise BybitRepairProtocolError(f"Bybit repair {field} must be an integer string")
    return int(value)


def _positive_decimal(value: object, field: str) -> Decimal:
    result = _parse_decimal(value, field)
    if result <= 0:
        raise BybitRepairProtocolError(f"Bybit repair {field} must be positive")
    return result


def _non_negative_decimal(value: object, field: str) -> Decimal:
    result = _parse_decimal(value, field)
    if result < 0:
        raise BybitRepairProtocolError(f"Bybit repair {field} must be non-negative")
    return result


def _parse_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, str):
        raise BybitRepairProtocolError(f"Bybit repair {field} must be a decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise BybitRepairProtocolError(f"Bybit repair {field} is not decimal") from exc
    if not result.is_finite():
        raise BybitRepairProtocolError(f"Bybit repair {field} must be finite")
    return result


def _decode_response(body: bytes) -> dict[str, object]:
    try:
        payload = json.loads(body.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BybitRepairProtocolError("invalid Bybit repair JSON response") from exc
    if not isinstance(payload, dict):
        raise BybitRepairProtocolError("Bybit repair response must be an object")
    if payload.get("retCode") != 0:
        raise BybitRepairError(f"BYBIT_REPAIR_RET_CODE_{payload.get('retCode')}")
    return payload
