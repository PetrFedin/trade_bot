from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo import BybitDemoOrderAck, BybitDemoOrderRequest
from app.execution.bybit_demo_excursion_store import BybitDemoExcursionStore
from app.execution.bybit_demo_protection_client import BybitDemoProtectionPosition
from app.execution.bybit_demo_trade_management_runtime import (
    BybitDemoTradeManagementRuntimeResult,
    BybitDemoTradeManagementRuntimeStatus,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoSide

_QUANTITY_EPSILON = Decimal("0.000000000001")


class BybitDemoMaxHoldCloseStatus(StrEnum):
    NOT_DUE = "NOT_DUE"
    WRITES_DISABLED = "WRITES_DISABLED"
    POSITION_ALREADY_CLOSED = "POSITION_ALREADY_CLOSED"
    CLOSE_BLOCKED = "CLOSE_BLOCKED"
    CLOSE_WRITE_FAILED = "CLOSE_WRITE_FAILED"
    CLOSE_UNRESOLVED = "CLOSE_UNRESOLVED"
    CLOSE_CONFIRMED = "CLOSE_CONFIRMED"


@dataclass(frozen=True)
class BybitDemoMaxHoldClosePolicy:
    writes_enabled: bool = False
    reconciliation_attempts: int = 4
    reconciliation_delay_seconds: float = 0.25

    def validate(self) -> None:
        if self.reconciliation_attempts < 1:
            raise ValueError("max-hold close reconciliation attempts must be positive")
        if self.reconciliation_delay_seconds < 0:
            raise ValueError("max-hold close reconciliation delay cannot be negative")


@dataclass(frozen=True)
class BybitDemoMaxHoldCloseResult:
    status: BybitDemoMaxHoldCloseStatus
    reasons: tuple[str, ...]
    close_request: BybitDemoOrderRequest | None
    close_ack: BybitDemoOrderAck | None
    reconciliation_attempts: int
    residual_size: Decimal | None
    position_closed: bool | None
    next_entry_allowed: bool = False
    lifecycle_reconciliation_still_required: bool = True
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


Sleeper = Callable[[float], None]


def execute_bybit_demo_max_hold_close(
    management: BybitDemoTradeManagementRuntimeResult,
    *,
    excursion_store: BybitDemoExcursionStore,
    client: Any,
    quote_client: Any,
    instrument: BybitInstrumentSpec,
    policy: BybitDemoMaxHoldClosePolicy | None = None,
    sleeper: Sleeper = time.sleep,
) -> BybitDemoMaxHoldCloseResult:
    """Close a baseline max-hold position only with explicit demo write permission.

    The management decision proves the frozen 36-bar rule is due. This executor then re-reads the
    actual position, validates exact full-size closeability with a fresh quote, submits a
    deterministic reduce-only market order, and independently confirms that the original-side
    position disappeared. Order acknowledgement alone is never treated as a completed close.
    """

    active = BybitDemoMaxHoldClosePolicy() if policy is None else policy
    active.validate()
    instrument.validate()
    _validate_dependencies(excursion_store, client, quote_client)
    if management.live_mainnet_order_routing_allowed:
        raise ValueError("max-hold close rejected mainnet-capable management result")
    decision = management.decision
    if (
        management.status is not BybitDemoTradeManagementRuntimeStatus.MAX_HOLD_CLOSE_REQUIRED
        or decision is None
        or not decision.max_hold_close_required
    ):
        return _result(BybitDemoMaxHoldCloseStatus.NOT_DUE, reasons=("MAX_HOLD_NOT_DUE",))
    if not active.writes_enabled:
        return _result(
            BybitDemoMaxHoldCloseStatus.WRITES_DISABLED,
            reasons=("MAX_HOLD_CLOSE_WRITES_DISABLED",),
        )

    try:
        checkpoint = excursion_store.load()
    except Exception as exc:  # noqa: BLE001 - durable identity is mandatory before a close write.
        return _result(
            BybitDemoMaxHoldCloseStatus.CLOSE_BLOCKED,
            reasons=(f"MAX_HOLD_EXCURSION_LOAD_FAILED:{type(exc).__name__}",),
        )
    excursion = checkpoint.state
    if excursion.symbol != instrument.symbol:
        return _result(
            BybitDemoMaxHoldCloseStatus.CLOSE_BLOCKED,
            reasons=("MAX_HOLD_INSTRUMENT_SYMBOL_MISMATCH",),
        )
    expected_side = "Buy" if excursion.side is CryptoSide.LONG else "Sell"

    try:
        position = _single_position(
            client.get_positions(settle_coin="USDT"),
            symbol=excursion.symbol,
            side=expected_side,
        )
    except Exception as exc:  # noqa: BLE001 - ambiguous position state blocks any close write.
        return _result(
            BybitDemoMaxHoldCloseStatus.CLOSE_BLOCKED,
            reasons=(f"MAX_HOLD_POSITION_READ_FAILED:{type(exc).__name__}",),
        )
    if position is None:
        return _result(
            BybitDemoMaxHoldCloseStatus.POSITION_ALREADY_CLOSED,
            reasons=("MAX_HOLD_POSITION_ALREADY_CLOSED",),
            residual_size=Decimal("0"),
            position_closed=True,
        )
    if abs(position.size - excursion.initial_quantity) > _QUANTITY_EPSILON:
        return _result(
            BybitDemoMaxHoldCloseStatus.CLOSE_BLOCKED,
            reasons=("MAX_HOLD_POSITION_SIZE_CHANGED_FROM_BASELINE",),
            residual_size=position.size,
            position_closed=False,
        )

    try:
        quote = quote_client.get_quote(symbol=excursion.symbol)
        quote.validate()
        if quote.symbol != excursion.symbol:
            raise ValueError("max-hold quote symbol mismatch")
        quantity = instrument.normalize_market_quantity(
            position.size,
            reference_price=quote.last_price,
        )
    except Exception as exc:  # noqa: BLE001 - fresh market preflight is mandatory.
        return _result(
            BybitDemoMaxHoldCloseStatus.CLOSE_BLOCKED,
            reasons=(f"MAX_HOLD_CLOSE_PREFLIGHT_FAILED:{type(exc).__name__}",),
            residual_size=position.size,
            position_closed=False,
        )
    if quantity is None or abs(quantity - position.size) > _QUANTITY_EPSILON:
        return _result(
            BybitDemoMaxHoldCloseStatus.CLOSE_BLOCKED,
            reasons=("MAX_HOLD_FULL_POSITION_NOT_EXACTLY_CLOSEABLE",),
            residual_size=position.size,
            position_closed=False,
        )

    request = BybitDemoOrderRequest(
        symbol=excursion.symbol,
        side="Sell" if excursion.side is CryptoSide.LONG else "Buy",
        quantity=quantity,
        order_link_id=_max_hold_order_link_id(checkpoint.entry_order_link_id),
        reduce_only=True,
    )
    request.validate()
    try:
        ack = client.place_market_order(request)
        if ack.live_mainnet_order or not ack.accepted:
            raise ValueError("max-hold close returned unsafe or rejected acknowledgement")
    except Exception as exc:  # noqa: BLE001 - original exchange protection remains authoritative.
        return _result(
            BybitDemoMaxHoldCloseStatus.CLOSE_WRITE_FAILED,
            reasons=(f"MAX_HOLD_CLOSE_WRITE_FAILED:{type(exc).__name__}",),
            close_request=request,
            residual_size=position.size,
            position_closed=False,
        )

    closed, attempts, residual, reason = _reconcile_position_close(
        client=client,
        symbol=excursion.symbol,
        side=expected_side,
        attempts=active.reconciliation_attempts,
        delay_seconds=active.reconciliation_delay_seconds,
        sleeper=sleeper,
    )
    if not closed:
        return _result(
            BybitDemoMaxHoldCloseStatus.CLOSE_UNRESOLVED,
            reasons=(reason,),
            close_request=request,
            close_ack=ack,
            reconciliation_attempts=attempts,
            residual_size=residual,
            position_closed=False,
        )
    return _result(
        BybitDemoMaxHoldCloseStatus.CLOSE_CONFIRMED,
        reasons=("MAX_HOLD_CLOSE_CONFIRMED",),
        close_request=request,
        close_ack=ack,
        reconciliation_attempts=attempts,
        residual_size=Decimal("0"),
        position_closed=True,
    )


def _reconcile_position_close(
    *,
    client: Any,
    symbol: str,
    side: str,
    attempts: int,
    delay_seconds: float,
    sleeper: Sleeper,
) -> tuple[bool, int, Decimal | None, str]:
    residual: Decimal | None = None
    successful_read = False
    last_error_type: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            position = _single_position(
                client.get_positions(settle_coin="USDT"),
                symbol=symbol,
                side=side,
            )
        except Exception as exc:  # noqa: BLE001 - accepted order still requires position proof.
            last_error_type = type(exc).__name__
            if attempt < attempts and delay_seconds > 0:
                sleeper(delay_seconds)
            continue
        successful_read = True
        if position is None:
            return True, attempt, Decimal("0"), "MAX_HOLD_POSITION_CONFIRMED_CLOSED"
        residual = position.size
        if attempt < attempts and delay_seconds > 0:
            sleeper(delay_seconds)
    if not successful_read and last_error_type is not None:
        reason = f"MAX_HOLD_POST_CLOSE_POSITION_READ_FAILED:{last_error_type}"
    else:
        reason = "MAX_HOLD_RESIDUAL_POSITION"
    return False, attempts, residual, reason


def _single_position(
    positions: Sequence[BybitDemoProtectionPosition],
    *,
    symbol: str,
    side: str,
) -> BybitDemoProtectionPosition | None:
    matching = tuple(
        position
        for position in positions
        if position.symbol == symbol and position.side == side and position.size > 0
    )
    if len(matching) > 1:
        raise ValueError("multiple matching max-hold positions")
    return None if not matching else matching[0]


def _max_hold_order_link_id(entry_order_link_id: str) -> str:
    if not entry_order_link_id.startswith("ASTRA-DEMO-"):
        raise ValueError("max-hold close requires ASTRA-DEMO entry orderLinkId")
    digest = hashlib.sha256(
        f"{entry_order_link_id}|MAX_HOLD_CLOSE".encode("utf-8")
    ).hexdigest()[:16].upper()
    return f"ASTRA-DEMO-H-{digest}"


def _validate_dependencies(
    excursion_store: BybitDemoExcursionStore,
    client: Any,
    quote_client: Any,
) -> None:
    if getattr(excursion_store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("max-hold close rejected mainnet-capable excursion store")
    if getattr(excursion_store, "order_writes_supported", True) is not False:
        raise ValueError("max-hold close requires diagnostics-only excursion store")
    if getattr(client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("max-hold close rejected mainnet-capable order client")
    if getattr(quote_client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("max-hold close rejected mainnet-capable quote client")


def _result(
    status: BybitDemoMaxHoldCloseStatus,
    *,
    reasons: tuple[str, ...] = (),
    close_request: BybitDemoOrderRequest | None = None,
    close_ack: BybitDemoOrderAck | None = None,
    reconciliation_attempts: int = 0,
    residual_size: Decimal | None = None,
    position_closed: bool | None = None,
) -> BybitDemoMaxHoldCloseResult:
    return BybitDemoMaxHoldCloseResult(
        status=status,
        reasons=reasons,
        close_request=close_request,
        close_ack=close_ack,
        reconciliation_attempts=reconciliation_attempts,
        residual_size=residual_size,
        position_closed=position_closed,
    )
