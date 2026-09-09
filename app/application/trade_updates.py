from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
import json
import threading

from app.execution.trade_fills import (
    FillAccountingResult,
    PaperTradeFillAccounting,
    parse_alpaca_trade_fill,
)
from app.oms.indexed import IndexedOmsStore
from app.runtime.alpaca_paper_adapter_v100 import (
    AlpacaTradeUpdateStreamV100,
    TradeUpdateV100,
)
from app.runtime.platform_common_v90 import sha256_digest


class UnmappedBrokerOrderError(LookupError):
    pass


@dataclass(frozen=True)
class TradeUpdateProcessingResult:
    stream_update: TradeUpdateV100 | None
    fill_accounting: FillAccountingResult | None
    intent_id: str | None


class PaperTradeUpdateProcessor:
    """Validate and route one account-wide Alpaca trade update.

    The legacy stream state machine remains the protocol/freshness/replay authority.
    Exact fill economics are parsed independently from the same raw frame, then mapped
    through the durable client-order index so process restarts do not lose routing state.
    """

    def __init__(
        self,
        *,
        stream: AlpacaTradeUpdateStreamV100,
        oms: IndexedOmsStore,
        fill_accounting: PaperTradeFillAccounting,
    ) -> None:
        self.stream = stream
        self.oms = oms
        self.fill_accounting = fill_accounting
        self._validated_fill_digests: set[str] = set()
        self._provenance_lock = threading.RLock()

    def process(
        self,
        raw_frame: bytes | str,
        *,
        received_at: datetime,
        expected_generation: int,
    ) -> TradeUpdateProcessingResult:
        stream_update = self.stream.ingest(
            raw_frame,
            received_at=received_at,
            expected_generation=expected_generation,
        )
        exact_fill = parse_alpaca_trade_fill(raw_frame)
        if exact_fill is None:
            return TradeUpdateProcessingResult(stream_update, None, None)

        if stream_update is not None:
            self._assert_parser_agreement(stream_update, exact_fill.client_order_id)
            if stream_update.order.broker_order_id != exact_fill.broker_order_id:
                raise ValueError("TRADE_UPDATE_BROKER_ORDER_ID_DIVERGENCE")
            if stream_update.order.filled_quantity != exact_fill.cumulative_quantity:
                raise ValueError("TRADE_UPDATE_CUMULATIVE_FILL_DIVERGENCE")
            with self._provenance_lock:
                self._validated_fill_digests.add(stream_update.digest)
        else:
            digest = self._frame_digest(raw_frame)
            with self._provenance_lock:
                if digest not in self._validated_fill_digests:
                    raise ValueError("TRADE_UPDATE_FILL_WITHOUT_VALIDATED_STREAM_PROVENANCE")

        record = self.oms.get_by_client_order_id(exact_fill.client_order_id)
        if record is None:
            raise UnmappedBrokerOrderError(exact_fill.client_order_id)
        accounting = self.fill_accounting.apply(record.intent_id, exact_fill)
        return TradeUpdateProcessingResult(stream_update, accounting, record.intent_id)

    @staticmethod
    def _assert_parser_agreement(update: TradeUpdateV100, client_order_id: str) -> None:
        if update.order.client_order_id != client_order_id:
            raise ValueError("TRADE_UPDATE_CLIENT_ORDER_ID_DIVERGENCE")

    @staticmethod
    def _frame_digest(raw_frame: bytes | str) -> str:
        try:
            document = json.loads(
                raw_frame.decode("utf-8") if isinstance(raw_frame, bytes) else raw_frame
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("TRADE_UPDATE_VALIDATED_DIGEST_RECONSTRUCTION_FAILED") from exc
        if not isinstance(document, Mapping):
            raise ValueError("TRADE_UPDATE_VALIDATED_DIGEST_RECONSTRUCTION_FAILED")
        return sha256_digest(document)
