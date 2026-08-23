from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.execution.bybit_mainnet_readonly import BybitMainnetReadOnlyClient

_MAX_HISTORY = timedelta(days=730)
_MAX_QUERY_WINDOW = timedelta(days=7)
_MAX_PAGES_PER_WINDOW = 1000
_ZERO = Decimal("0")


@dataclass(frozen=True)
class BybitBrokerFundingLedgerEntry:
    transaction_id: str
    symbol: str
    transaction_time_ms: int
    funding_usdt: Decimal
    cash_flow_usdt: Decimal | None
    transaction_type: str

    def validate(self) -> None:
        if not self.transaction_id:
            raise ValueError("broker funding ledger transaction id is required")
        if (
            not self.symbol
            or self.symbol != self.symbol.strip().upper()
            or not self.symbol.endswith("USDT")
            or not self.symbol.isalnum()
        ):
            raise ValueError("broker funding ledger symbol must be normalized USDT")
        if self.transaction_time_ms < 0:
            raise ValueError("broker funding ledger timestamp cannot be negative")
        if not self.funding_usdt.is_finite():
            raise ValueError("broker funding ledger funding must be finite")
        if self.cash_flow_usdt is not None and not self.cash_flow_usdt.is_finite():
            raise ValueError("broker funding ledger cash flow must be finite")
        if self.transaction_type != "SETTLEMENT":
            raise ValueError("broker funding ledger accepts settlement rows only")

    @property
    def direction(self) -> str:
        if self.funding_usdt > 0:
            return "RECEIVED"
        if self.funding_usdt < 0:
            return "PAID"
        return "ZERO"

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "transaction_id": self.transaction_id,
            "symbol": self.symbol,
            "transaction_time_ms": self.transaction_time_ms,
            "transaction_time": datetime.fromtimestamp(
                self.transaction_time_ms / 1000,
                tz=UTC,
            ).isoformat(),
            "funding_usdt": str(self.funding_usdt),
            "cash_flow_usdt": (
                None if self.cash_flow_usdt is None else str(self.cash_flow_usdt)
            ),
            "transaction_type": self.transaction_type,
            "direction": self.direction,
        }


@dataclass(frozen=True)
class BybitBrokerFundingLedgerAcquisition:
    start_at: str
    end_exclusive_at: str
    entries: tuple[BybitBrokerFundingLedgerEntry, ...]
    request_count: int
    window_count: int
    api_host: str
    read_only: bool = True
    order_writes_supported: bool = False
    bybit_live_order_routing_allowed: bool = False
    public_reconstruction_reconciled: bool = False

    def validate(self) -> None:
        start = _parse_time(self.start_at)
        end = _parse_time(self.end_exclusive_at)
        if end <= start:
            raise ValueError("broker funding ledger acquisition interval is invalid")
        if end - start > _MAX_HISTORY:
            raise ValueError("broker funding ledger acquisition exceeds supported history")
        if self.request_count <= 0 or self.window_count <= 0:
            raise ValueError("broker funding ledger acquisition requires requests/windows")
        if not self.api_host or self.api_host != self.api_host.strip().lower():
            raise ValueError("broker funding ledger API host is invalid")
        previous_key: tuple[int, str] | None = None
        seen_ids: set[str] = set()
        for entry in self.entries:
            entry.validate()
            moment = datetime.fromtimestamp(entry.transaction_time_ms / 1000, tz=UTC)
            if not start <= moment < end:
                raise ValueError("broker funding ledger entry falls outside acquisition interval")
            if entry.transaction_id in seen_ids:
                raise ValueError("broker funding ledger transaction id is duplicated")
            seen_ids.add(entry.transaction_id)
            key = (entry.transaction_time_ms, entry.transaction_id)
            if previous_key is not None and key <= previous_key:
                raise ValueError("broker funding ledger entries must be ordered and unique")
            previous_key = key
        if (
            not self.read_only
            or self.order_writes_supported
            or self.bybit_live_order_routing_allowed
            or self.public_reconstruction_reconciled
        ):
            raise ValueError("broker funding ledger acquisition cannot activate trading or claim reconciliation")

    @property
    def total_funding_usdt(self) -> Decimal:
        return sum((item.funding_usdt for item in self.entries), start=_ZERO)

    @property
    def received_funding_usdt(self) -> Decimal:
        return sum(
            (item.funding_usdt for item in self.entries if item.funding_usdt > 0),
            start=_ZERO,
        )

    @property
    def paid_funding_usdt(self) -> Decimal:
        return sum(
            (-item.funding_usdt for item in self.entries if item.funding_usdt < 0),
            start=_ZERO,
        )

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": "BYBIT_MAINNET_BROKER_FUNDING_LEDGER_V116",
            "start_at": self.start_at,
            "end_exclusive_at": self.end_exclusive_at,
            "entry_count": len(self.entries),
            "request_count": self.request_count,
            "window_count": self.window_count,
            "api_host": self.api_host,
            "total_funding_usdt": str(self.total_funding_usdt),
            "received_funding_usdt": str(self.received_funding_usdt),
            "paid_funding_usdt": str(self.paid_funding_usdt),
            "entries": [item.to_payload() for item in self.entries],
            "read_only": self.read_only,
            "order_writes_supported": self.order_writes_supported,
            "bybit_live_order_routing_allowed": self.bybit_live_order_routing_allowed,
            "broker_ledger_funding_collected": True,
            "public_reconstruction_reconciled": self.public_reconstruction_reconciled,
        }


class BybitMainnetFundingLedgerClient(BybitMainnetReadOnlyClient):
    """Read-only transaction-log projection for authoritative broker funding cash flows."""

    def fetch_broker_funding_ledger(
        self,
        *,
        start_at: datetime,
        end_exclusive_at: datetime,
    ) -> BybitBrokerFundingLedgerAcquisition:
        start = _utc(start_at)
        end = _utc(end_exclusive_at)
        if end <= start:
            raise ValueError("broker funding ledger interval is invalid")
        if end - start > _MAX_HISTORY:
            raise ValueError("broker funding ledger interval cannot exceed 730 days")
        entries: dict[str, BybitBrokerFundingLedgerEntry] = {}
        request_count = 0
        window_count = 0
        cursor_start = start
        while cursor_start < end:
            cursor_end = min(cursor_start + _MAX_QUERY_WINDOW, end)
            window_count += 1
            cursor: str | None = None
            page_count = 0
            while True:
                page_count += 1
                if page_count > _MAX_PAGES_PER_WINDOW:
                    raise RuntimeError("broker funding ledger pagination exceeded safety limit")
                query: dict[str, object] = {
                    "accountType": "UNIFIED",
                    "category": "linear",
                    "currency": "USDT",
                    "type": "SETTLEMENT",
                    "startTime": int(cursor_start.timestamp() * 1000),
                    "endTime": int(cursor_end.timestamp() * 1000) - 1,
                    "limit": 50,
                }
                if cursor:
                    query["cursor"] = cursor
                result = self._private_get_result(
                    path="/v5/account/transaction-log",
                    query=query,
                )
                request_count += 1
                raw_rows = result.get("list")
                if not isinstance(raw_rows, list):
                    raise ValueError("broker funding ledger response is missing list")
                for raw in raw_rows:
                    if not isinstance(raw, dict):
                        raise ValueError("broker funding ledger row must be an object")
                    entry = _parse_entry(raw)
                    moment = datetime.fromtimestamp(entry.transaction_time_ms / 1000, tz=UTC)
                    if not cursor_start <= moment < cursor_end:
                        raise ValueError("broker funding ledger row exceeds requested window")
                    existing = entries.get(entry.transaction_id)
                    if existing is not None and existing != entry:
                        raise ValueError("broker funding ledger duplicate transaction conflicts")
                    entries[entry.transaction_id] = entry
                next_cursor = result.get("nextPageCursor")
                if next_cursor in (None, ""):
                    break
                if not isinstance(next_cursor, str):
                    raise ValueError("broker funding ledger cursor must be text")
                if next_cursor == cursor:
                    raise ValueError("broker funding ledger cursor did not advance")
                cursor = next_cursor
            cursor_start = cursor_end
        ordered = tuple(
            sorted(
                entries.values(),
                key=lambda item: (item.transaction_time_ms, item.transaction_id),
            )
        )
        acquisition = BybitBrokerFundingLedgerAcquisition(
            start_at=start.isoformat(),
            end_exclusive_at=end.isoformat(),
            entries=ordered,
            request_count=request_count,
            window_count=window_count,
            api_host=self.host,
        )
        acquisition.validate()
        return acquisition


def _parse_entry(raw: dict[str, Any]) -> BybitBrokerFundingLedgerEntry:
    transaction_type = _required_text(raw, "type").upper()
    if transaction_type != "SETTLEMENT":
        raise ValueError("broker funding ledger received non-settlement transaction")
    symbol = _required_text(raw, "symbol").upper()
    entry = BybitBrokerFundingLedgerEntry(
        transaction_id=_required_text(raw, "transactionId"),
        symbol=symbol,
        transaction_time_ms=_required_int(raw, "transactionTime"),
        funding_usdt=_required_decimal(raw, "funding"),
        cash_flow_usdt=_optional_decimal(raw.get("cashFlow")),
        transaction_type=transaction_type,
    )
    entry.validate()
    return entry


def _required_text(raw: dict[str, Any], field: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"broker funding ledger missing {field}")
    return value.strip()


def _required_int(raw: dict[str, Any], field: str) -> int:
    value = raw.get(field)
    if isinstance(value, bool):
        raise ValueError(f"broker funding ledger {field} is invalid")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"broker funding ledger {field} is invalid") from exc
    if parsed < 0:
        raise ValueError(f"broker funding ledger {field} cannot be negative")
    return parsed


def _required_decimal(raw: dict[str, Any], field: str) -> Decimal:
    value = raw.get(field)
    if value is None or value == "":
        raise ValueError(f"broker funding ledger missing {field}")
    return _decimal(value, field=field)


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return _decimal(value, field="cashFlow")


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"broker funding ledger {field} is invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"broker funding ledger {field} must be finite")
    return parsed


def _parse_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("broker funding ledger timestamp must be timezone-aware")
    return value.astimezone(UTC)
