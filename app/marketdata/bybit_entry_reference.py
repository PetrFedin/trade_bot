from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from threading import Lock

from app.marketdata.bybit_demo_quotes import (
    BybitDemoMarketQuote,
    BybitDemoMarketQuoteClient,
)


@dataclass(frozen=True)
class BybitEntryExecutionReference:
    symbol: str
    side: str
    price: Decimal
    quote_received_time_ms: int
    quote_server_time_ms: int
    quote_age_ms: int


class BybitEntryReferenceStore:
    """Process-local handoff of the exact validated pre-entry quote to the OMS adapter."""

    live_mainnet_order_routing_allowed = False

    def __init__(self, *, maximum_handoff_age_ms: int = 5_000) -> None:
        if not 1 <= maximum_handoff_age_ms <= 30_000:
            raise ValueError("Bybit entry reference handoff age must be within [1, 30000] ms")
        self.maximum_handoff_age_ms = maximum_handoff_age_ms
        self._quotes: dict[str, BybitDemoMarketQuote] = {}
        self._lock = Lock()

    def record(self, quote: BybitDemoMarketQuote) -> None:
        quote.validate()
        with self._lock:
            self._quotes[quote.symbol] = quote

    def consume(
        self,
        *,
        symbol: str,
        side: str,
        now_ms: int,
    ) -> BybitEntryExecutionReference:
        if symbol != symbol.strip().upper() or not symbol.endswith("USDT"):
            raise ValueError("Bybit entry reference symbol must be normalized USDT")
        if side not in {"Buy", "Sell"}:
            raise ValueError("Bybit entry reference side must be Buy or Sell")
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < 0:
            raise ValueError("Bybit entry reference clock must be a non-negative integer")
        with self._lock:
            quote = self._quotes.get(symbol)
        if quote is None:
            raise RuntimeError("BYBIT_PRE_ENTRY_EXECUTABLE_REFERENCE_UNAVAILABLE")
        quote.validate()
        handoff_age = now_ms - quote.received_time_ms
        if handoff_age < 0:
            raise RuntimeError("BYBIT_PRE_ENTRY_EXECUTABLE_REFERENCE_CLOCK_REGRESSION")
        if handoff_age > self.maximum_handoff_age_ms:
            raise RuntimeError("BYBIT_PRE_ENTRY_EXECUTABLE_REFERENCE_STALE")
        price = quote.ask_price if side == "Buy" else quote.bid_price
        return BybitEntryExecutionReference(
            symbol=symbol,
            side=side,
            price=price,
            quote_received_time_ms=quote.received_time_ms,
            quote_server_time_ms=quote.server_time_ms,
            quote_age_ms=quote.age_ms,
        )


class BybitEntryReferenceQuoteClient(BybitDemoMarketQuoteClient):
    """Existing quote client plus a narrow, non-authoritative pre-entry handoff."""

    def __init__(
        self,
        *,
        reference_store: BybitEntryReferenceStore,
        observation_hook: Callable[[], None] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        self.reference_store = reference_store
        self.observation_hook = observation_hook

    def get_quote(self, *, symbol: str) -> BybitDemoMarketQuote:
        quote = super().get_quote(symbol=symbol)
        self.reference_store.record(quote)
        if self.observation_hook is not None:
            try:
                self.observation_hook()
            except Exception:  # noqa: BLE001 - telemetry must not replace valid market data.
                pass
        return quote
