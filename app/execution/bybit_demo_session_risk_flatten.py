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
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoSide
from app.strategy.crypto_session_risk import (
    CryptoSessionRiskState,
    evaluate_crypto_session_risk,
)

_QUANTITY_EPSILON = Decimal("0.000000000001")
Sleeper = Callable[[float], None]


class BybitDemoSessionRiskFlattenStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    WRITES_DISABLED = "WRITES_DISABLED"
    POSITION_ALREADY_CLOSED = "POSITION_ALREADY_CLOSED"
    CLOSE_BLOCKED = "CLOSE_BLOCKED"
    CLOSE_WRITE_FAILED = "CLOSE_WRITE_FAILED"
    CLOSE_UNRESOLVED = "CLOSE_UNRESOLVED"
    CLOSE_CONFIRMED = "CLOSE_CONFIRMED"


@dataclass(frozen=True)
class BybitDemoSessionRiskFlattenPolicy:
    writes_enabled: bool = False
    reconciliation_attempts: int = 4
    reconciliation_delay_seconds: float = 0.25

    def validate(self) -> None:
        if self.reconciliation_attempts < 1:
            raise ValueError("session-risk flatten reconciliation attempts must be positive")
        if self.reconciliation_delay_seconds < 0:
            raise ValueError("session-risk flatten reconciliation delay cannot be negative")


@dataclass(frozen=True)
class BybitDemoSessionRiskFlattenResult:
    status: BybitDemoSessionRiskFlattenStatus
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


def execute_bybit_demo_session_risk_flatten(
    *,
    session_state: CryptoSessionRiskState,
    excursion_store: BybitDemoExcursionStore,
    client: Any,
    quote_client: Any,
    instrument: BybitInstrumentSpec,
    policy: BybitDemoSessionRiskFlattenPolicy | None = None,
    sleeper: Sleeper = time.sleep,
) -> BybitDemoSessionRiskFlattenResult:
    """Reduce an existing Demo position to zero when durable session risk requires flattening.

    The executor is deliberately narrower than normal trade management. It only operates after the
    qualified session-risk policy says ``flatten_required``. It re-reads the exact broker position,
    submits one deterministic reduce-only market close for the current residual quantity, and then
    independently reconciles broker truth. Acknowledgement alone never clears durable lifecycle
    state. New exposure is impossible through this executor.
    """

    active = BybitDemoSessionRiskFlattenPolicy() if policy is None else policy
    active.validate()
    instrument.validate()
    _validate_dependencies(excursion_store, client, quote_client)

    decision = evaluate_crypto_session_risk(session_state)
    if not decision.flatten_required:
        return _result(
            BybitDemoSessionRiskFlattenStatus.NOT_REQUIRED,
            reasons=decision.reasons,
        )
    if not active.writes_enabled:
        return _result(
            BybitDemoSessionRiskFlattenStatus.WRITES_DISABLED,
            reasons=tuple(
                dict.fromkeys((*decision.reasons, "SESSION_RISK_FLATTEN_WRITES_DISABLED"))
            ),
        )

    try:
        checkpoint = excursion_store.load()
    except Exception as exc:  # noqa: BLE001 - durable trade identity is mandatory.
        return _result(
            BybitDemoSessionRiskFlattenStatus.CLOSE_BLOCKED,
            reasons=(f"SESSION_RISK_EXCURSION_LOAD_FAILED:{type(exc).__name__}",),
        )
    excursion = checkpoint.state
    if excursion.symbol != instrument.symbol:
        return _result(
            BybitDemoSessionRiskFlattenStatus.CLOSE_BLOCKED,
            reasons=("SESSION_RISK_INSTRUMENT_SYMBOL_MISMATCH",),
        )
    expected_side = "Buy" if excursion.side is CryptoSide.LONG else "Sell"

    try:
        position = _single_position(
            client.get_positions(settle_coin="USDT"),
            symbol=excursion.symbol,
            side=expected_side,
        )
    except Exception as exc:  # noqa: BLE001 - unknown broker state blocks mutation.
        return _result(
            BybitDemoSessionRiskFlattenStatus.CLOSE_BLOCKED,
            reasons=(f"SESSION_RISK_POSITION_READ_FAILED:{type(exc).__name__}",),
        )
    if position is None:
        return _result(
            BybitDemoSessionRiskFlattenStatus.POSITION_ALREADY_CLOSED,
            reasons=tuple(
                dict.fromkeys((*decision.reasons, "SESSION_RISK_POSITION_ALREADY_CLOSED"))
            ),
            residual_size=Decimal("0"),
            position_closed=True,
        )
    if position.size <= 0:
        return _result(
            BybitDemoSessionRiskFlattenStatus.CLOSE_BLOCKED,
            reasons=("SESSION_RISK_POSITION_SIZE_INVALID",),
        )
    if position.size - excursion.initial_quantity > _QUANTITY_EPSILON:
        return _result(
            BybitDemoSessionRiskFlattenStatus.CLOSE_BLOCKED,
            reasons=("SESSION_RISK_POSITION_EXCEEDS_DURABLE_BASELINE",),
            residual_size=position.size,
            position_closed=False,
        )

    try:
        quote = quote_client.get_quote(symbol=excursion.symbol)
        quote.validate()
        if quote.symbol != excursion.symbol:
            raise ValueError("session-risk flatten quote symbol mismatch")
        quantity = instrument.normalize_market_quantity(
            position.size,
            reference_price=quote.last_price,
        )
    except Exception as exc:  # noqa: BLE001 - fresh market preflight is mandatory.
        return _result(
            BybitDemoSessionRiskFlattenStatus.CLOSE_BLOCKED,
            reasons=(f"SESSION_RISK_CLOSE_PREFLIGHT_FAILED:{type(exc).__name__}",),
            residual_size=position.size,
            position_closed=False,
        )
    if quantity is None or abs(quantity - position.size) > _QUANTITY_EPSILON:
        return _result(
            BybitDemoSessionRiskFlattenStatus.CLOSE_BLOCKED,
            reasons=("SESSION_RISK_FULL_POSITION_NOT_EXACTLY_CLOSEABLE",),
            residual_size=position.size,
            position_closed=False,
        )

    request = BybitDemoOrderRequest(
        symbol=excursion.symbol,
        side="Sell" if excursion.side is CryptoSide.LONG else "Buy",
        quantity=quantity,
        order_link_id=_session_risk_order_link_id(checkpoint.entry_order_link_id),
        reduce_only=True,
        reference_price=quote.last_price,
    )
    request.validate()
    try:
        ack = client.place_market_order(request)
        if ack.live_mainnet_order or not ack.accepted:
            raise ValueError("session-risk flatten returned unsafe or rejected acknowledgement")
    except Exception as exc:  # noqa: BLE001 - never retry an ambiguous mutation automatically.
        return _result(
            BybitDemoSessionRiskFlattenStatus.CLOSE_WRITE_FAILED,
            reasons=(f"SESSION_RISK_CLOSE_WRITE_FAILED:{type(exc).__name__}",),
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
            BybitDemoSessionRiskFlattenStatus.CLOSE_UNRESOLVED,
            reasons=(reason,),
            close_request=request,
            close_ack=ack,
            reconciliation_attempts=attempts,
            residual_size=residual,
            position_closed=False,
        )
    return _result(
        BybitDemoSessionRiskFlattenStatus.CLOSE_CONFIRMED,
        reasons=tuple(
            dict.fromkeys((*decision.reasons, "SESSION_RISK_FLATTEN_CONFIRMED"))
        ),
        close_request=request,
        close_ack=ack,
        reconciliation_attempts=attempts,
        residual_size=Decimal("0"),
        position_closed=True,
    )


def _single_position(
    positions: Sequence[Any],
    *,
    symbol: str,
    side: str,
) -> Any | None:
    matching = tuple(
        position
        for position in positions
        if position.symbol == symbol and position.side == side and position.size > 0
    )
    if len(matching) > 1:
        raise ValueError("multiple matching session-risk positions")
    return None if not matching else matching[0]


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
        except Exception as exc:  # noqa: BLE001 - accepted close still needs broker proof.
            last_error_type = type(exc).__name__
            if attempt < attempts and delay_seconds > 0:
                sleeper(delay_seconds)
            continue
        successful_read = True
        if position is None:
            return True, attempt, Decimal("0"), "SESSION_RISK_POSITION_CONFIRMED_CLOSED"
        residual = position.size
        if attempt < attempts and delay_seconds > 0:
            sleeper(delay_seconds)
    if not successful_read and last_error_type is not None:
        reason = f"SESSION_RISK_POST_CLOSE_POSITION_READ_FAILED:{last_error_type}"
    else:
        reason = "SESSION_RISK_RESIDUAL_POSITION"
    return False, attempts, residual, reason


def _session_risk_order_link_id(entry_order_link_id: str) -> str:
    if not entry_order_link_id.startswith("ASTRA-DEMO-"):
        raise ValueError("session-risk close requires ASTRA-DEMO entry orderLinkId")
    digest = hashlib.sha256(
        f"{entry_order_link_id}|SESSION_RISK_FLATTEN".encode()
    ).hexdigest()[:16].upper()
    return f"ASTRA-DEMO-R-{digest}"


def _validate_dependencies(
    excursion_store: BybitDemoExcursionStore,
    client: Any,
    quote_client: Any,
) -> None:
    if getattr(excursion_store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("session-risk flatten rejected mainnet-capable excursion store")
    if getattr(excursion_store, "order_writes_supported", True) is not False:
        raise ValueError("session-risk flatten requires diagnostics-only excursion store")
    if getattr(client, "environment", None) != "BYBIT_DEMO":
        raise ValueError("session-risk flatten requires a BYBIT_DEMO order client")
    if getattr(client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("session-risk flatten rejected mainnet-capable order client")
    if getattr(quote_client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("session-risk flatten rejected mainnet-capable quote client")


def _result(
    status: BybitDemoSessionRiskFlattenStatus,
    *,
    reasons: tuple[str, ...] = (),
    close_request: BybitDemoOrderRequest | None = None,
    close_ack: BybitDemoOrderAck | None = None,
    reconciliation_attempts: int = 0,
    residual_size: Decimal | None = None,
    position_closed: bool | None = None,
) -> BybitDemoSessionRiskFlattenResult:
    return BybitDemoSessionRiskFlattenResult(
        status=status,
        reasons=reasons,
        close_request=close_request,
        close_ack=close_ack,
        reconciliation_attempts=reconciliation_attempts,
        residual_size=residual_size,
        position_closed=position_closed,
    )


__all__ = [
    "BybitDemoSessionRiskFlattenPolicy",
    "BybitDemoSessionRiskFlattenResult",
    "BybitDemoSessionRiskFlattenStatus",
    "execute_bybit_demo_session_risk_flatten",
]
