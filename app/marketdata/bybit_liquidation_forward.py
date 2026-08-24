from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

_BUCKET_MS = 5 * 60 * 1000
_DEFAULT_WS_HOST = "stream.bybit.com"
_ALLOWED_WS_HOSTS = frozenset(
    {
        "stream.bybit.com",
        "stream.bybit.tr",
        "stream.bybit.id",
        "stream.bybit.kz",
        "stream.bybitgeorgia.ge",
        "stream.manepa.jp",
        "ws2.spark-fintech.com",
    }
)
_ALLOWED_STATUS = frozenset(
    {"CONNECTING", "CONNECTED", "HEARTBEAT", "DISCONNECTED", "STOPPED"}
)


class BybitLiquidationProtocolError(RuntimeError):
    """Raised when public liquidation data violates the declared Bybit contract."""


@dataclass(frozen=True)
class BybitLiquidationEvent:
    event_id: str
    system_ts_ms: int
    event_time_ms: int
    symbol: str
    raw_position_side: str
    liquidated_position_side: str
    quantity_base: Decimal
    bankruptcy_price: Decimal
    estimated_notional_usdt: Decimal
    message_ordinal: int
    exchange_event_id_available: bool = False
    historical_backfill_available: bool = False
    trade_actionable: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def validate(self) -> None:
        _validate_sha(self.event_id)
        _validate_nonnegative_int(self.system_ts_ms, "system timestamp")
        _validate_nonnegative_int(self.event_time_ms, "event timestamp")
        _validate_symbol(self.symbol)
        if self.raw_position_side not in {"Buy", "Sell"}:
            raise ValueError("liquidation raw position side must be Buy or Sell")
        expected_side = "LONG" if self.raw_position_side == "Buy" else "SHORT"
        if self.liquidated_position_side != expected_side:
            raise ValueError("liquidation position-side interpretation is inconsistent")
        for name, value in (
            ("quantity_base", self.quantity_base),
            ("bankruptcy_price", self.bankruptcy_price),
            ("estimated_notional_usdt", self.estimated_notional_usdt),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"liquidation {name} must be positive and finite")
        expected_notional = self.quantity_base * self.bankruptcy_price
        if self.estimated_notional_usdt != expected_notional:
            raise ValueError(
                "liquidation estimated notional must equal quantity x bankruptcy price"
            )
        _validate_nonnegative_int(self.message_ordinal, "message ordinal")
        if (
            self.exchange_event_id_available
            or self.historical_backfill_available
            or self.trade_actionable
            or self.live_mainnet_order_routing_allowed
        ):
            raise ValueError(
                "forward liquidation evidence cannot claim unavailable/live capabilities"
            )

    @property
    def bucket_start_ms(self) -> int:
        return (self.event_time_ms // _BUCKET_MS) * _BUCKET_MS

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "event_id": self.event_id,
            "system_ts_ms": self.system_ts_ms,
            "event_time_ms": self.event_time_ms,
            "bucket_start_ms": self.bucket_start_ms,
            "symbol": self.symbol,
            "raw_position_side": self.raw_position_side,
            "liquidated_position_side": self.liquidated_position_side,
            "quantity_base": str(self.quantity_base),
            "bankruptcy_price": str(self.bankruptcy_price),
            "estimated_notional_usdt": str(self.estimated_notional_usdt),
            "message_ordinal": self.message_ordinal,
            "exchange_event_id_available": self.exchange_event_id_available,
            "historical_backfill_available": self.historical_backfill_available,
            "trade_actionable": self.trade_actionable,
            "live_mainnet_order_routing_allowed": (
                self.live_mainnet_order_routing_allowed
            ),
        }


@dataclass(frozen=True)
class BybitLiquidation5mBucket:
    symbol: str
    bucket_start_ms: int
    event_count: int
    long_liquidation_count: int
    short_liquidation_count: int
    long_estimated_notional_usdt: Decimal
    short_estimated_notional_usdt: Decimal
    total_estimated_notional_usdt: Decimal
    long_minus_short_estimated_notional_usdt: Decimal
    normalized_long_minus_short_imbalance: Decimal
    largest_event_estimated_notional_usdt: Decimal

    def validate(self) -> None:
        _validate_symbol(self.symbol)
        _validate_nonnegative_int(self.bucket_start_ms, "bucket timestamp")
        if self.bucket_start_ms % _BUCKET_MS != 0:
            raise ValueError("liquidation bucket must be aligned to five minutes")
        if self.event_count <= 0:
            raise ValueError("liquidation bucket must contain at least one event")
        if self.long_liquidation_count < 0 or self.short_liquidation_count < 0:
            raise ValueError("liquidation bucket counts cannot be negative")
        side_count = self.long_liquidation_count + self.short_liquidation_count
        if side_count != self.event_count:
            raise ValueError("liquidation bucket side counts do not reconcile")
        if self.long_estimated_notional_usdt < 0:
            raise ValueError("liquidation bucket LONG notional cannot be negative")
        if self.short_estimated_notional_usdt < 0:
            raise ValueError("liquidation bucket SHORT notional cannot be negative")
        expected_total = (
            self.long_estimated_notional_usdt
            + self.short_estimated_notional_usdt
        )
        if self.total_estimated_notional_usdt != expected_total:
            raise ValueError("liquidation bucket total notional does not reconcile")
        if self.total_estimated_notional_usdt <= 0:
            raise ValueError("liquidation bucket total notional must be positive")
        expected_delta = (
            self.long_estimated_notional_usdt
            - self.short_estimated_notional_usdt
        )
        if self.long_minus_short_estimated_notional_usdt != expected_delta:
            raise ValueError("liquidation bucket signed notional does not reconcile")
        expected_imbalance = expected_delta / self.total_estimated_notional_usdt
        if self.normalized_long_minus_short_imbalance != expected_imbalance:
            raise ValueError("liquidation bucket normalized imbalance does not reconcile")
        if not Decimal("-1") <= expected_imbalance <= Decimal("1"):
            raise ValueError(
                "liquidation bucket normalized imbalance must be within [-1, 1]"
            )
        if self.largest_event_estimated_notional_usdt <= 0:
            raise ValueError("liquidation bucket largest event must be positive")


StatusCallback = Callable[
    [str, str, int, str | None],
    None | Awaitable[None],
]
EventCallback = Callable[
    [tuple[BybitLiquidationEvent, ...]],
    None | Awaitable[None],
]


def validate_bybit_public_liquidation_ws_host(host: str) -> str:
    normalized = host.strip().lower()
    if normalized not in _ALLOWED_WS_HOSTS:
        raise ValueError("Bybit public liquidation WebSocket host is not allowlisted")
    return normalized


def build_all_liquidation_topics(symbols: Sequence[str]) -> tuple[str, ...]:
    if not 1 <= len(symbols) <= 50:
        raise ValueError("Bybit liquidation subscription requires 1..50 symbols")
    seen: set[str] = set()
    topics: list[str] = []
    for raw_symbol in symbols:
        symbol = raw_symbol.strip().upper()
        _validate_symbol(symbol)
        if symbol in seen:
            raise ValueError(
                "Bybit liquidation subscription cannot contain duplicate symbols"
            )
        seen.add(symbol)
        topics.append(f"allLiquidation.{symbol}")
    return tuple(topics)


def parse_bybit_all_liquidation_message(
    payload: Mapping[str, Any],
    *,
    expected_symbols: Sequence[str] | None = None,
) -> tuple[BybitLiquidationEvent, ...]:
    topic = payload.get("topic")
    if not isinstance(topic, str) or not topic.startswith("allLiquidation."):
        raise BybitLiquidationProtocolError("unexpected Bybit liquidation topic")
    topic_symbol = topic.removeprefix("allLiquidation.")
    _validate_symbol(topic_symbol)
    if payload.get("type") != "snapshot":
        raise BybitLiquidationProtocolError(
            "Bybit liquidation message must be snapshot type"
        )
    system_ts_ms = _required_nonnegative_int(payload, "ts")
    rows = _liquidation_rows(payload.get("data"))
    expected = _expected_symbol_set(expected_symbols)
    if expected is not None and topic_symbol not in expected:
        raise BybitLiquidationProtocolError(
            "liquidation topic symbol is outside subscription"
        )
    events = tuple(
        _parse_liquidation_row(
            row,
            ordinal=ordinal,
            topic_symbol=topic_symbol,
            system_ts_ms=system_ts_ms,
            expected_symbols=expected,
        )
        for ordinal, row in enumerate(rows)
    )
    return events


def aggregate_liquidations_5m(
    events: Sequence[BybitLiquidationEvent],
) -> tuple[BybitLiquidation5mBucket, ...]:
    grouped: dict[tuple[str, int], list[BybitLiquidationEvent]] = {}
    for event in events:
        event.validate()
        key = (event.symbol, event.bucket_start_ms)
        grouped.setdefault(key, []).append(event)
    buckets = [
        _aggregate_bucket(symbol, bucket_start_ms, rows)
        for (symbol, bucket_start_ms), rows in sorted(grouped.items())
    ]
    return tuple(buckets)


async def capture_bybit_public_liquidations(
    symbols: Sequence[str],
    *,
    on_events: EventCallback,
    on_status: StatusCallback | None = None,
    host: str = _DEFAULT_WS_HOST,
    heartbeat_seconds: float = 20.0,
    maximum_reconnect_delay_seconds: float = 30.0,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Capture forward-only public liquidation events with bounded reconnects."""

    ws_host = validate_bybit_public_liquidation_ws_host(host)
    topics = build_all_liquidation_topics(symbols)
    if not 5 <= heartbeat_seconds <= 60:
        raise ValueError(
            "Bybit liquidation heartbeat must be within [5, 60] seconds"
        )
    if not 1 <= maximum_reconnect_delay_seconds <= 120:
        raise ValueError(
            "Bybit liquidation reconnect delay must be within [1, 120] seconds"
        )
    expected_symbols = tuple(topic.rsplit(".", 1)[1] for topic in topics)
    active_stop = stop_event if stop_event is not None else asyncio.Event()
    url = f"wss://{ws_host}/v5/public/linear"
    reconnect_delay = 1.0
    while not active_stop.is_set():
        connection_epoch = uuid.uuid4().hex
        await _call_status(
            on_status,
            "CONNECTING",
            connection_epoch,
            _now_ms(),
            None,
        )
        try:
            await _capture_connection(
                url=url,
                topics=topics,
                expected_symbols=expected_symbols,
                on_events=on_events,
                on_status=on_status,
                connection_epoch=connection_epoch,
                heartbeat_seconds=heartbeat_seconds,
                stop_event=active_stop,
            )
        except asyncio.CancelledError:
            raise
        except (OSError, ConnectionClosed, BybitLiquidationProtocolError) as exc:
            await _call_status(
                on_status,
                "DISCONNECTED",
                connection_epoch,
                _now_ms(),
                type(exc).__name__,
            )
            if active_stop.is_set():
                return
            should_stop = await _wait_for_stop(active_stop, reconnect_delay)
            if should_stop:
                return
            reconnect_delay = min(
                reconnect_delay * 2,
                maximum_reconnect_delay_seconds,
            )
        else:
            await _call_status(
                on_status,
                "STOPPED",
                connection_epoch,
                _now_ms(),
                None,
            )
            return


async def _capture_connection(
    *,
    url: str,
    topics: tuple[str, ...],
    expected_symbols: tuple[str, ...],
    on_events: EventCallback,
    on_status: StatusCallback | None,
    connection_epoch: str,
    heartbeat_seconds: float,
    stop_event: asyncio.Event,
) -> None:
    async with connect(
        url,
        ping_interval=None,
        close_timeout=5,
        max_size=4 * 1024 * 1024,
    ) as websocket:
        request = {
            "req_id": f"liq-{connection_epoch[:16]}",
            "op": "subscribe",
            "args": list(topics),
        }
        await websocket.send(json.dumps(request, separators=(",", ":")))
        subscribed = False
        last_heartbeat = time.monotonic()
        while not stop_event.is_set():
            remaining = heartbeat_seconds - (time.monotonic() - last_heartbeat)
            timeout = max(0.25, remaining)
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=timeout)
            except TimeoutError:
                await websocket.send('{"op":"ping"}')
                last_heartbeat = time.monotonic()
                continue
            payload = _decode_ws_payload(raw)
            op = payload.get("op")
            if op == "subscribe":
                if payload.get("success") is not True:
                    raise BybitLiquidationProtocolError(
                        "Bybit liquidation subscription was rejected"
                    )
                subscribed = True
                await _call_status(
                    on_status,
                    "CONNECTED",
                    connection_epoch,
                    _now_ms(),
                    None,
                )
                continue
            if op in {"ping", "pong"} or payload.get("ret_msg") == "pong":
                last_heartbeat = time.monotonic()
                await _call_status(
                    on_status,
                    "HEARTBEAT",
                    connection_epoch,
                    _now_ms(),
                    None,
                )
                continue
            if not subscribed:
                raise BybitLiquidationProtocolError(
                    "Bybit liquidation data arrived before subscription acknowledgement"
                )
            events = parse_bybit_all_liquidation_message(
                payload,
                expected_symbols=expected_symbols,
            )
            if events:
                await _maybe_await(on_events(events))


def _decode_ws_payload(raw: str | bytes) -> Mapping[str, Any]:
    if not isinstance(raw, str):
        raise BybitLiquidationProtocolError(
            "Bybit liquidation WebSocket returned non-text frame"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BybitLiquidationProtocolError(
            "Bybit liquidation WebSocket returned invalid JSON"
        ) from exc
    if not isinstance(payload, Mapping):
        raise BybitLiquidationProtocolError(
            "Bybit liquidation WebSocket payload must be an object"
        )
    return payload


def _liquidation_rows(raw_data: Any) -> tuple[Mapping[str, Any], ...]:
    if isinstance(raw_data, Mapping):
        return (raw_data,)
    if isinstance(raw_data, list) and all(
        isinstance(item, Mapping) for item in raw_data
    ):
        return tuple(raw_data)
    raise BybitLiquidationProtocolError(
        "Bybit liquidation data must be object or object list"
    )


def _expected_symbol_set(
    symbols: Sequence[str] | None,
) -> set[str] | None:
    if symbols is None:
        return None
    expected: set[str] = set()
    for raw_symbol in symbols:
        symbol = raw_symbol.strip().upper()
        _validate_symbol(symbol)
        expected.add(symbol)
    return expected


def _parse_liquidation_row(
    row: Mapping[str, Any],
    *,
    ordinal: int,
    topic_symbol: str,
    system_ts_ms: int,
    expected_symbols: set[str] | None,
) -> BybitLiquidationEvent:
    event_time_ms = _required_nonnegative_int(row, "T")
    symbol = _required_symbol(row, "s")
    if symbol != topic_symbol:
        raise BybitLiquidationProtocolError(
            "liquidation row symbol does not match topic"
        )
    if expected_symbols is not None and symbol not in expected_symbols:
        raise BybitLiquidationProtocolError(
            "liquidation row symbol is outside subscription"
        )
    raw_side = row.get("S")
    if raw_side not in {"Buy", "Sell"}:
        raise BybitLiquidationProtocolError(
            "liquidation row side must be Buy or Sell"
        )
    quantity = _required_positive_decimal(row, "v")
    bankruptcy_price = _required_positive_decimal(row, "p")
    event_id = _liquidation_event_id(
        system_ts_ms=system_ts_ms,
        event_time_ms=event_time_ms,
        symbol=symbol,
        raw_side=raw_side,
        quantity=quantity,
        bankruptcy_price=bankruptcy_price,
        ordinal=ordinal,
    )
    event = BybitLiquidationEvent(
        event_id=event_id,
        system_ts_ms=system_ts_ms,
        event_time_ms=event_time_ms,
        symbol=symbol,
        raw_position_side=raw_side,
        liquidated_position_side=("LONG" if raw_side == "Buy" else "SHORT"),
        quantity_base=quantity,
        bankruptcy_price=bankruptcy_price,
        estimated_notional_usdt=quantity * bankruptcy_price,
        message_ordinal=ordinal,
    )
    event.validate()
    return event


def _liquidation_event_id(
    *,
    system_ts_ms: int,
    event_time_ms: int,
    symbol: str,
    raw_side: str,
    quantity: Decimal,
    bankruptcy_price: Decimal,
    ordinal: int,
) -> str:
    canonical = {
        "system_ts_ms": system_ts_ms,
        "event_time_ms": event_time_ms,
        "symbol": symbol,
        "raw_position_side": raw_side,
        "quantity_base": str(quantity),
        "bankruptcy_price": str(bankruptcy_price),
        "message_ordinal": ordinal,
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _aggregate_bucket(
    symbol: str,
    bucket_start_ms: int,
    rows: Sequence[BybitLiquidationEvent],
) -> BybitLiquidation5mBucket:
    long_rows = [
        row for row in rows if row.liquidated_position_side == "LONG"
    ]
    short_rows = [
        row for row in rows if row.liquidated_position_side == "SHORT"
    ]
    long_notional = sum(
        (row.estimated_notional_usdt for row in long_rows),
        Decimal("0"),
    )
    short_notional = sum(
        (row.estimated_notional_usdt for row in short_rows),
        Decimal("0"),
    )
    total = long_notional + short_notional
    delta = long_notional - short_notional
    bucket = BybitLiquidation5mBucket(
        symbol=symbol,
        bucket_start_ms=bucket_start_ms,
        event_count=len(rows),
        long_liquidation_count=len(long_rows),
        short_liquidation_count=len(short_rows),
        long_estimated_notional_usdt=long_notional,
        short_estimated_notional_usdt=short_notional,
        total_estimated_notional_usdt=total,
        long_minus_short_estimated_notional_usdt=delta,
        normalized_long_minus_short_imbalance=delta / total,
        largest_event_estimated_notional_usdt=max(
            row.estimated_notional_usdt for row in rows
        ),
    )
    bucket.validate()
    return bucket


async def _wait_for_stop(stop_event: asyncio.Event, delay: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except TimeoutError:
        return False
    return True


async def _call_status(
    callback: StatusCallback | None,
    status: str,
    connection_epoch: str,
    observed_at_ms: int,
    reason_code: str | None,
) -> None:
    if status not in _ALLOWED_STATUS:
        raise ValueError("invalid liquidation stream status")
    if callback is None:
        return
    await _maybe_await(
        callback(
            status,
            connection_epoch,
            observed_at_ms,
            reason_code,
        )
    )


async def _maybe_await(value: None | Awaitable[None]) -> None:
    if value is not None:
        await value


def _required_nonnegative_int(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool):
        raise BybitLiquidationProtocolError(
            f"liquidation {key} must be an integer"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BybitLiquidationProtocolError(
            f"liquidation {key} must be an integer"
        ) from exc
    if parsed < 0:
        raise BybitLiquidationProtocolError(
            f"liquidation {key} cannot be negative"
        )
    return parsed


def _required_positive_decimal(row: Mapping[str, Any], key: str) -> Decimal:
    value = row.get(key)
    if isinstance(value, bool):
        raise BybitLiquidationProtocolError(
            f"liquidation {key} must be numeric"
        )
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BybitLiquidationProtocolError(
            f"liquidation {key} must be numeric"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise BybitLiquidationProtocolError(
            f"liquidation {key} must be positive and finite"
        )
    return parsed


def _required_symbol(row: Mapping[str, Any], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str):
        raise BybitLiquidationProtocolError(
            f"liquidation {key} must be a symbol string"
        )
    symbol = value.strip().upper()
    _validate_symbol(symbol)
    return symbol


def _validate_symbol(symbol: str) -> None:
    if not symbol or symbol != symbol.upper() or len(symbol) > 40:
        raise ValueError(
            "Bybit liquidation symbol must be uppercase and non-empty"
        )
    if not symbol.isascii() or not symbol.isalnum():
        raise ValueError(
            "Bybit liquidation symbol contains unsupported characters"
        )


def _validate_nonnegative_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(
            f"liquidation {name} must be a non-negative integer"
        )


def _validate_sha(value: str) -> None:
    if len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError(
            "liquidation event id must be lowercase sha256 hex"
        )


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "BybitLiquidation5mBucket",
    "BybitLiquidationEvent",
    "BybitLiquidationProtocolError",
    "aggregate_liquidations_5m",
    "build_all_liquidation_topics",
    "capture_bybit_public_liquidations",
    "parse_bybit_all_liquidation_message",
    "validate_bybit_public_liquidation_ws_host",
]
