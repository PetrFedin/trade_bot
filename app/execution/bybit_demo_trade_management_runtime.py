from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from app.execution.bybit_demo_excursion_store import BybitDemoExcursionStore
from app.execution.bybit_demo_protection_client import BybitDemoProtectionPosition
from app.execution.bybit_demo_stop_ratchet_client import (
    BybitDemoStopRatchetAck,
    BybitDemoStopRatchetRequest,
)
from app.execution.bybit_demo_trade_management_parity import (
    BybitDemoTradeManagementParityAction,
    BybitDemoTradeManagementParityDecision,
    evaluate_bybit_demo_trade_management_parity,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import interval_milliseconds
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide

_ZERO = Decimal("0")
_QUANTITY_EPSILON = Decimal("0.000000000001")


class BybitDemoTradeManagementRuntimeStatus(StrEnum):
    TRACKING_BLOCKED = "TRACKING_BLOCKED"
    POSITION_CLOSED = "POSITION_CLOSED"
    NO_CHANGE = "NO_CHANGE"
    SHADOW_RATCHET_DUE = "SHADOW_RATCHET_DUE"
    MAX_HOLD_CLOSE_REQUIRED = "MAX_HOLD_CLOSE_REQUIRED"
    RATCHET_WINDOW_MISSED = "RATCHET_WINDOW_MISSED"
    RATCHET_WRITE_FAILED = "RATCHET_WRITE_FAILED"
    RATCHET_UNVERIFIED = "RATCHET_UNVERIFIED"
    RATCHET_VERIFIED = "RATCHET_VERIFIED"


@dataclass(frozen=True)
class BybitDemoTradeManagementRuntimePolicy:
    stop_ratchet_writes_enabled: bool = False
    interval: str = "5"
    execution_limit: int = 100

    def validate(self) -> None:
        interval_milliseconds(self.interval)
        if not 1 <= self.execution_limit <= 100:
            raise ValueError("demo trade-management execution limit must be within [1, 100]")


@dataclass(frozen=True)
class BybitDemoTradeManagementRuntimeResult:
    status: BybitDemoTradeManagementRuntimeStatus
    reasons: tuple[str, ...]
    decision: BybitDemoTradeManagementParityDecision | None
    entry_execution_time_ms: int | None
    entry_bucket_start_ms: int | None
    protection_bar_start_ms: int | None
    actual_entry_fee_usdt: Decimal | None
    fresh_last_price: Decimal | None
    ratchet_ack: BybitDemoStopRatchetAck | None
    post_write_position: BybitDemoProtectionPosition | None
    stop_ratchet_write_attempted: bool
    stop_ratchet_verified: bool
    max_hold_close_write_allowed: bool = False
    demo_only: bool = True
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def run_bybit_demo_trade_management_cycle(
    *,
    excursion_store: BybitDemoExcursionStore,
    client: Any,
    completed_bar_client: Any,
    quote_client: Any,
    instrument: BybitInstrumentSpec,
    strategy_config: CryptoPerpStrategyConfig,
    now_ms: int,
    runtime_policy: BybitDemoTradeManagementRuntimePolicy | None = None,
) -> BybitDemoTradeManagementRuntimeResult:
    """Evaluate and optionally apply the frozen baseline completed-bar stop ratchet.

    Stop writes are disabled by default. Even when explicitly enabled, the runtime can only tighten
    the independent full-position stop-loss. It never rewrites a fixed TP or runner trailing stop.
    The entry bucket is counted for holding time but excluded from favorable/adverse protection
    extrema because a demo market fill can occur after that candle has already moved. Max-hold is
    surfaced as a required close decision but is not automatically written by this runtime yet.
    """

    policy = (
        BybitDemoTradeManagementRuntimePolicy()
        if runtime_policy is None
        else runtime_policy
    )
    policy.validate()
    instrument.validate()
    strategy_config.validate()
    _validate_dependencies(
        excursion_store=excursion_store,
        client=client,
        completed_bar_client=completed_bar_client,
        quote_client=quote_client,
        stop_writes_enabled=policy.stop_ratchet_writes_enabled,
    )
    if isinstance(now_ms, bool) or now_ms < 0:
        raise ValueError("demo trade-management now_ms must be non-negative")

    try:
        checkpoint = excursion_store.load()
    except Exception as exc:  # noqa: BLE001 - missing durable basis must block management.
        return _result(
            BybitDemoTradeManagementRuntimeStatus.TRACKING_BLOCKED,
            reasons=(f"MANAGEMENT_EXCURSION_LOAD_FAILED:{type(exc).__name__}",),
        )
    excursion = checkpoint.state
    expected_side = "Buy" if excursion.side is CryptoSide.LONG else "Sell"

    try:
        entry_evidence = _entry_execution_evidence(
            client=client,
            symbol=excursion.symbol,
            side=expected_side,
            entry_order_link_id=checkpoint.entry_order_link_id,
            execution_limit=policy.execution_limit,
        )
    except Exception as exc:  # noqa: BLE001 - unresolved entry timing/fee blocks ratchet.
        return _result(
            BybitDemoTradeManagementRuntimeStatus.TRACKING_BLOCKED,
            reasons=(f"MANAGEMENT_ENTRY_EVIDENCE_FAILED:{type(exc).__name__}",),
        )
    if abs(entry_evidence.quantity - excursion.initial_quantity) > _QUANTITY_EPSILON:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.TRACKING_BLOCKED,
            reasons=("MANAGEMENT_ENTRY_QUANTITY_MISMATCH",),
            entry_evidence=entry_evidence,
        )
    if abs(entry_evidence.average_price - excursion.entry_price) > _QUANTITY_EPSILON:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.TRACKING_BLOCKED,
            reasons=("MANAGEMENT_ENTRY_PRICE_MISMATCH",),
            entry_evidence=entry_evidence,
        )

    interval_ms = interval_milliseconds(policy.interval)
    entry_bucket_start_ms = (entry_evidence.first_exec_time_ms // interval_ms) * interval_ms
    if now_ms < entry_evidence.first_exec_time_ms:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.TRACKING_BLOCKED,
            reasons=("MANAGEMENT_CLOCK_PRECEDES_ENTRY",),
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
        )
    current_bucket_start_ms = (now_ms // interval_ms) * interval_ms
    holding_bar_count = max(
        0,
        (current_bucket_start_ms - entry_bucket_start_ms) // interval_ms,
    )
    protection_bar_start_ms = entry_bucket_start_ms + interval_ms
    try:
        completed_bars = completed_bar_client.fetch_completed_range(
            symbol=excursion.symbol,
            start_ms=protection_bar_start_ms,
            now_ms=now_ms,
            interval=policy.interval,
        )
    except Exception as exc:  # noqa: BLE001 - incomplete bar history must fail closed.
        return _result(
            BybitDemoTradeManagementRuntimeStatus.TRACKING_BLOCKED,
            reasons=(f"MANAGEMENT_COMPLETED_BAR_READ_FAILED:{type(exc).__name__}",),
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
        )

    try:
        position = _single_position(
            client.get_positions(settle_coin="USDT"),
            symbol=excursion.symbol,
            side=expected_side,
        )
    except Exception as exc:  # noqa: BLE001 - ambiguous position state blocks management.
        return _result(
            BybitDemoTradeManagementRuntimeStatus.TRACKING_BLOCKED,
            reasons=(f"MANAGEMENT_POSITION_READ_FAILED:{type(exc).__name__}",),
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
        )
    if position is None:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.POSITION_CLOSED,
            reasons=("MANAGEMENT_POSITION_NO_LONGER_OPEN",),
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
        )

    try:
        fee_rate = client.get_fee_rate(symbol=excursion.symbol)
        effective_config = replace(
            strategy_config,
            taker_fee_rate=fee_rate.taker_fee_rate,
        )
        effective_config.validate()
        decision = evaluate_bybit_demo_trade_management_parity(
            excursion,
            position=position,
            completed_bars_since_entry=completed_bars,
            completed_holding_bar_count=holding_bar_count,
            actual_entry_fee_usdt=entry_evidence.fee_usdt,
            strategy_config=effective_config,
            instrument=instrument,
        )
    except Exception as exc:  # noqa: BLE001 - fee/parity uncertainty blocks ratchet.
        return _result(
            BybitDemoTradeManagementRuntimeStatus.TRACKING_BLOCKED,
            reasons=(f"MANAGEMENT_PARITY_EVALUATION_FAILED:{type(exc).__name__}",),
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
        )

    if decision.action is BybitDemoTradeManagementParityAction.BLOCKED:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.TRACKING_BLOCKED,
            reasons=decision.reasons,
            decision=decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
        )
    if decision.action is BybitDemoTradeManagementParityAction.MAX_HOLD_CLOSE_REQUIRED:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.MAX_HOLD_CLOSE_REQUIRED,
            reasons=decision.reasons,
            decision=decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
        )
    if decision.action is BybitDemoTradeManagementParityAction.NO_CHANGE:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.NO_CHANGE,
            decision=decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
        )
    if not policy.stop_ratchet_writes_enabled:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.SHADOW_RATCHET_DUE,
            reasons=("DEMO_STOP_RATCHET_WRITES_DISABLED", *decision.reasons),
            decision=decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
        )

    try:
        quote = quote_client.get_quote(symbol=excursion.symbol)
        quote.validate()
        if quote.symbol != excursion.symbol:
            raise ValueError("management quote symbol mismatch")
        pre_write = _single_position(
            client.get_positions(settle_coin="USDT"),
            symbol=excursion.symbol,
            side=expected_side,
        )
    except Exception as exc:  # noqa: BLE001 - fresh state is mandatory before stop write.
        return _result(
            BybitDemoTradeManagementRuntimeStatus.TRACKING_BLOCKED,
            reasons=(f"MANAGEMENT_PREWRITE_REFRESH_FAILED:{type(exc).__name__}",),
            decision=decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
        )
    if pre_write is None:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.POSITION_CLOSED,
            reasons=("MANAGEMENT_POSITION_CLOSED_BEFORE_RATCHET",),
            decision=decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
            fresh_last_price=quote.last_price,
        )

    refreshed_decision = evaluate_bybit_demo_trade_management_parity(
        excursion,
        position=pre_write,
        completed_bars_since_entry=completed_bars,
        completed_holding_bar_count=holding_bar_count,
        actual_entry_fee_usdt=entry_evidence.fee_usdt,
        strategy_config=effective_config,
        instrument=instrument,
    )
    if refreshed_decision.action is BybitDemoTradeManagementParityAction.NO_CHANGE:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.NO_CHANGE,
            reasons=("MANAGEMENT_STOP_ALREADY_SATISFIES_BASELINE",),
            decision=refreshed_decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
            fresh_last_price=quote.last_price,
        )
    if refreshed_decision.action is BybitDemoTradeManagementParityAction.BLOCKED:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.TRACKING_BLOCKED,
            reasons=refreshed_decision.reasons,
            decision=refreshed_decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
            fresh_last_price=quote.last_price,
        )
    if refreshed_decision.action is BybitDemoTradeManagementParityAction.MAX_HOLD_CLOSE_REQUIRED:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.MAX_HOLD_CLOSE_REQUIRED,
            reasons=refreshed_decision.reasons,
            decision=refreshed_decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
            fresh_last_price=quote.last_price,
        )
    desired_stop = refreshed_decision.desired_stop_loss_price
    current_stop = pre_write.stop_loss_price
    if desired_stop is None or current_stop is None:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.TRACKING_BLOCKED,
            reasons=("MANAGEMENT_RATCHET_PRICE_UNAVAILABLE",),
            decision=refreshed_decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
            fresh_last_price=quote.last_price,
        )

    request = BybitDemoStopRatchetRequest(
        symbol=excursion.symbol,
        side=expected_side,
        previous_stop_loss_price=current_stop,
        new_stop_loss_price=desired_stop,
        current_last_price=quote.last_price,
    )
    try:
        request.validate()
    except ValueError:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.RATCHET_WINDOW_MISSED,
            reasons=("MANAGEMENT_DESIRED_STOP_NOT_BEHIND_FRESH_LAST_PRICE",),
            decision=refreshed_decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
            fresh_last_price=quote.last_price,
        )

    try:
        ack = client.ratchet_position_stop_loss(request)
    except Exception as exc:  # noqa: BLE001 - existing protection remains authoritative on failure.
        return _result(
            BybitDemoTradeManagementRuntimeStatus.RATCHET_WRITE_FAILED,
            reasons=(f"MANAGEMENT_STOP_RATCHET_WRITE_FAILED:{type(exc).__name__}",),
            decision=refreshed_decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
            fresh_last_price=quote.last_price,
            write_attempted=True,
        )

    try:
        post_write = _single_position(
            client.get_positions(settle_coin="USDT"),
            symbol=excursion.symbol,
            side=expected_side,
        )
    except Exception as exc:  # noqa: BLE001 - accepted write is not proof of exchange state.
        return _result(
            BybitDemoTradeManagementRuntimeStatus.RATCHET_UNVERIFIED,
            reasons=(f"MANAGEMENT_POSTWRITE_READ_FAILED:{type(exc).__name__}",),
            decision=refreshed_decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
            fresh_last_price=quote.last_price,
            ack=ack,
            write_attempted=True,
        )
    verification_reasons = _post_write_reasons(
        before=pre_write,
        after=post_write,
        desired_stop=desired_stop,
    )
    if verification_reasons:
        return _result(
            BybitDemoTradeManagementRuntimeStatus.RATCHET_UNVERIFIED,
            reasons=verification_reasons,
            decision=refreshed_decision,
            entry_evidence=entry_evidence,
            entry_bucket_start_ms=entry_bucket_start_ms,
            protection_bar_start_ms=protection_bar_start_ms,
            fresh_last_price=quote.last_price,
            ack=ack,
            post_write_position=post_write,
            write_attempted=True,
        )
    return _result(
        BybitDemoTradeManagementRuntimeStatus.RATCHET_VERIFIED,
        reasons=("MANAGEMENT_BASELINE_STOP_RATCHET_VERIFIED",),
        decision=refreshed_decision,
        entry_evidence=entry_evidence,
        entry_bucket_start_ms=entry_bucket_start_ms,
        protection_bar_start_ms=protection_bar_start_ms,
        fresh_last_price=quote.last_price,
        ack=ack,
        post_write_position=post_write,
        write_attempted=True,
        verified=True,
    )


@dataclass(frozen=True)
class _EntryExecutionEvidence:
    first_exec_time_ms: int
    quantity: Decimal
    average_price: Decimal
    fee_usdt: Decimal


def _entry_execution_evidence(
    *,
    client: Any,
    symbol: str,
    side: str,
    entry_order_link_id: str,
    execution_limit: int,
) -> _EntryExecutionEvidence:
    rows = client.get_executions(
        symbol=symbol,
        order_link_id=entry_order_link_id,
        limit=execution_limit,
    )
    seen_ids: set[str] = set()
    fills: list[tuple[int, Decimal, Decimal, Decimal]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise ValueError("management entry execution row must be an object")
        if row.get("symbol") not in (None, symbol):
            continue
        if str(row.get("orderLinkId") or "") != entry_order_link_id:
            continue
        if row.get("side") != side:
            continue
        exec_id = _required_text(row, "execId")
        if exec_id in seen_ids:
            continue
        quantity = _required_decimal(row, "execQty")
        price = _required_decimal(row, "execPrice")
        fee = _required_decimal(row, "execFee", allow_negative=True)
        exec_time = _required_int(row, "execTime")
        if quantity <= 0 or price <= 0:
            raise ValueError("management entry execution quantity/price must be positive")
        fills.append((exec_time, quantity, price, fee))
        seen_ids.add(exec_id)
    if not fills:
        raise ValueError("management entry executions are unavailable")
    quantity = sum((item[1] for item in fills), start=_ZERO)
    notional = sum((item[1] * item[2] for item in fills), start=_ZERO)
    return _EntryExecutionEvidence(
        first_exec_time_ms=min(item[0] for item in fills),
        quantity=quantity,
        average_price=notional / quantity,
        fee_usdt=sum((item[3] for item in fills), start=_ZERO),
    )


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
        raise ValueError("multiple matching demo management positions")
    return None if not matching else matching[0]


def _post_write_reasons(
    *,
    before: BybitDemoProtectionPosition,
    after: BybitDemoProtectionPosition | None,
    desired_stop: Decimal,
) -> tuple[str, ...]:
    if after is None:
        return ("MANAGEMENT_POSITION_DISAPPEARED_AFTER_STOP_WRITE",)
    reasons: list[str] = []
    if after.stop_loss_price != desired_stop:
        reasons.append("MANAGEMENT_STOP_RATCHET_NOT_REFLECTED")
    if after.take_profit_price != before.take_profit_price:
        reasons.append("MANAGEMENT_TAKE_PROFIT_CHANGED_UNEXPECTEDLY")
    if after.trailing_stop_distance != before.trailing_stop_distance:
        reasons.append("MANAGEMENT_TRAILING_STOP_CHANGED_UNEXPECTEDLY")
    if after.size != before.size:
        reasons.append("MANAGEMENT_POSITION_SIZE_CHANGED_DURING_RATCHET")
    if after.average_price != before.average_price:
        reasons.append("MANAGEMENT_AVERAGE_ENTRY_CHANGED_DURING_RATCHET")
    return tuple(reasons)


def _validate_dependencies(
    *,
    excursion_store: BybitDemoExcursionStore,
    client: Any,
    completed_bar_client: Any,
    quote_client: Any,
    stop_writes_enabled: bool,
) -> None:
    if getattr(excursion_store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("demo management rejected mainnet-capable excursion store")
    if getattr(excursion_store, "order_writes_supported", True) is not False:
        raise ValueError("demo management requires diagnostics-only excursion store")
    if getattr(client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("demo management rejected mainnet-capable order client")
    if getattr(completed_bar_client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("demo management rejected mainnet-capable completed-bar client")
    if getattr(completed_bar_client, "order_writes_supported", True) is not False:
        raise ValueError("demo management completed-bar client must be read-only")
    if getattr(quote_client, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("demo management rejected mainnet-capable quote client")
    if stop_writes_enabled and not getattr(client, "stop_ratchet_write_supported", False):
        raise ValueError("demo management writes require stop-ratchet capability")


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"management execution missing {field}")
    return value


def _required_decimal(
    row: Mapping[str, Any],
    field: str,
    *,
    allow_negative: bool = False,
) -> Decimal:
    value = row.get(field)
    if value is None or value == "":
        raise ValueError(f"management execution missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"management execution invalid {field}") from exc
    if not parsed.is_finite() or (not allow_negative and parsed < 0):
        raise ValueError(f"management execution invalid {field}")
    return parsed


def _required_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if isinstance(value, bool):
        raise ValueError(f"management execution invalid {field}")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"management execution invalid {field}") from exc
    if parsed < 0:
        raise ValueError(f"management execution invalid {field}")
    return parsed


def _result(
    status: BybitDemoTradeManagementRuntimeStatus,
    *,
    reasons: tuple[str, ...] = (),
    decision: BybitDemoTradeManagementParityDecision | None = None,
    entry_evidence: _EntryExecutionEvidence | None = None,
    entry_bucket_start_ms: int | None = None,
    protection_bar_start_ms: int | None = None,
    fresh_last_price: Decimal | None = None,
    ack: BybitDemoStopRatchetAck | None = None,
    post_write_position: BybitDemoProtectionPosition | None = None,
    write_attempted: bool = False,
    verified: bool = False,
) -> BybitDemoTradeManagementRuntimeResult:
    return BybitDemoTradeManagementRuntimeResult(
        status=status,
        reasons=reasons,
        decision=decision,
        entry_execution_time_ms=(
            None if entry_evidence is None else entry_evidence.first_exec_time_ms
        ),
        entry_bucket_start_ms=entry_bucket_start_ms,
        protection_bar_start_ms=protection_bar_start_ms,
        actual_entry_fee_usdt=None if entry_evidence is None else entry_evidence.fee_usdt,
        fresh_last_price=fresh_last_price,
        ratchet_ack=ack,
        post_write_position=post_write_position,
        stop_ratchet_write_attempted=write_attempted,
        stop_ratchet_verified=verified,
    )
