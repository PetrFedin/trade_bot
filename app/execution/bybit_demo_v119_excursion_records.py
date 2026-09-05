from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_ALLOWED_SIDES = frozenset({"LONG", "SHORT"})
_REVISION_HEX = frozenset("0123456789abcdef")
_STATE_KEYS = frozenset(
    {
        "symbol",
        "side",
        "entry_price",
        "initial_quantity",
        "stop_fraction",
        "observation_count",
        "latest_server_time_ms",
        "latest_mark_price",
        "latest_gross_r",
        "observed_peak_favorable_r",
        "observed_trough_r",
        "latest_giveback_from_peak_r",
        "current_quantity",
        "partial_close_seen",
        "exchange_unrealised_pnl_usdt",
        "projected_initial_quantity_gross_pnl_usdt",
        "current_quantity_gross_pnl_usdt",
    }
)
_ZERO = Decimal("0")


@dataclass(frozen=True)
class BybitDemoExcursionStateV119:
    """Neutral persistence state for one active Demo trade excursion.

    This record deliberately carries no strategy, broker, market-data or order-mutation type.
    Safety markers are object invariants and remain outside the historical v119 ``state_json``
    payload so the canonical revision formula stays compatible with the preserved source.
    """

    symbol: str
    side: str
    entry_price: Decimal
    initial_quantity: Decimal
    stop_fraction: Decimal
    observation_count: int = 0
    latest_server_time_ms: int | None = None
    latest_mark_price: Decimal | None = None
    latest_gross_r: Decimal = _ZERO
    observed_peak_favorable_r: Decimal = _ZERO
    observed_trough_r: Decimal = _ZERO
    latest_giveback_from_peak_r: Decimal = _ZERO
    current_quantity: Decimal | None = None
    partial_close_seen: bool = False
    exchange_unrealised_pnl_usdt: Decimal | None = None
    projected_initial_quantity_gross_pnl_usdt: Decimal = _ZERO
    current_quantity_gross_pnl_usdt: Decimal = _ZERO
    diagnostics_only: bool = True
    exit_threshold_retuning_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def validate(self) -> None:
        _validate_state(self)


@dataclass(frozen=True)
class BybitDemoExcursionCheckpointV119:
    entry_order_link_id: str
    state: BybitDemoExcursionStateV119
    revision: str
    diagnostics_only: bool = True
    exit_threshold_retuning_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def validate(self) -> None:
        validate_demo_order_link_v119(self.entry_order_link_id)
        validate_excursion_revision_v119(self.revision)
        self.state.validate()
        if not self.diagnostics_only:
            raise ValueError("demo excursion checkpoint must remain diagnostics only")
        if self.exit_threshold_retuning_allowed:
            raise ValueError("demo excursion checkpoint cannot authorize exit retuning")
        if self.strategy_promotion_allowed:
            raise ValueError("demo excursion checkpoint cannot authorize strategy promotion")
        if self.live_mainnet_order_routing_allowed:
            raise ValueError("demo excursion checkpoint cannot permit live routing")


def canonical_json_v119(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def encode_excursion_state_v119(state: BybitDemoExcursionStateV119) -> dict[str, Any]:
    state.validate()
    return {
        "symbol": state.symbol,
        "side": state.side,
        "entry_price": str(state.entry_price),
        "initial_quantity": str(state.initial_quantity),
        "stop_fraction": str(state.stop_fraction),
        "observation_count": state.observation_count,
        "latest_server_time_ms": state.latest_server_time_ms,
        "latest_mark_price": _optional_decimal_text(state.latest_mark_price),
        "latest_gross_r": str(state.latest_gross_r),
        "observed_peak_favorable_r": str(state.observed_peak_favorable_r),
        "observed_trough_r": str(state.observed_trough_r),
        "latest_giveback_from_peak_r": str(state.latest_giveback_from_peak_r),
        "current_quantity": _optional_decimal_text(state.current_quantity),
        "partial_close_seen": state.partial_close_seen,
        "exchange_unrealised_pnl_usdt": _optional_decimal_text(
            state.exchange_unrealised_pnl_usdt
        ),
        "projected_initial_quantity_gross_pnl_usdt": str(
            state.projected_initial_quantity_gross_pnl_usdt
        ),
        "current_quantity_gross_pnl_usdt": str(state.current_quantity_gross_pnl_usdt),
    }


def decode_excursion_state_v119(value: dict[str, Any]) -> BybitDemoExcursionStateV119:
    if not isinstance(value, dict):
        raise ValueError("demo excursion state must be an object")
    actual_keys = frozenset(value)
    if actual_keys != _STATE_KEYS:
        missing = sorted(_STATE_KEYS - actual_keys)
        unknown = sorted(actual_keys - _STATE_KEYS)
        detail = f"missing={','.join(missing)};unknown={','.join(unknown)}"
        raise ValueError(f"demo excursion state keys are invalid:{detail}")

    state = BybitDemoExcursionStateV119(
        symbol=_text_field(value, "symbol"),
        side=_text_field(value, "side"),
        entry_price=_decimal_field(value, "entry_price"),
        initial_quantity=_decimal_field(value, "initial_quantity"),
        stop_fraction=_decimal_field(value, "stop_fraction"),
        observation_count=_int_field(value, "observation_count"),
        latest_server_time_ms=_optional_int_field(value, "latest_server_time_ms"),
        latest_mark_price=_optional_decimal_field(value, "latest_mark_price"),
        latest_gross_r=_decimal_field(value, "latest_gross_r"),
        observed_peak_favorable_r=_decimal_field(value, "observed_peak_favorable_r"),
        observed_trough_r=_decimal_field(value, "observed_trough_r"),
        latest_giveback_from_peak_r=_decimal_field(value, "latest_giveback_from_peak_r"),
        current_quantity=_optional_decimal_field(value, "current_quantity"),
        partial_close_seen=_bool_field(value, "partial_close_seen"),
        exchange_unrealised_pnl_usdt=_optional_decimal_field(
            value,
            "exchange_unrealised_pnl_usdt",
        ),
        projected_initial_quantity_gross_pnl_usdt=_decimal_field(
            value,
            "projected_initial_quantity_gross_pnl_usdt",
        ),
        current_quantity_gross_pnl_usdt=_decimal_field(
            value,
            "current_quantity_gross_pnl_usdt",
        ),
    )
    state.validate()
    return state


def excursion_revision_v119(
    entry_order_link_id: str,
    state: BybitDemoExcursionStateV119 | dict[str, Any],
) -> str:
    validate_demo_order_link_v119(entry_order_link_id)
    payload = encode_excursion_state_v119(state) if isinstance(state, BybitDemoExcursionStateV119) else state
    decoded = decode_excursion_state_v119(payload)
    canonical_payload = encode_excursion_state_v119(decoded)
    canonical = canonical_json_v119(
        {
            "entry_order_link_id": entry_order_link_id,
            "state": canonical_payload,
        }
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_excursion_checkpoint_v119(
    *,
    entry_order_link_id: str,
    state: BybitDemoExcursionStateV119,
) -> BybitDemoExcursionCheckpointV119:
    checkpoint = BybitDemoExcursionCheckpointV119(
        entry_order_link_id=entry_order_link_id,
        state=state,
        revision=excursion_revision_v119(entry_order_link_id, state),
    )
    checkpoint.validate()
    return checkpoint


def validate_demo_order_link_v119(value: str) -> None:
    if not isinstance(value, str) or not value.startswith("ASTRA-DEMO-"):
        raise ValueError("demo excursion checkpoint requires ASTRA-DEMO orderLinkId")


def validate_excursion_revision_v119(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in _REVISION_HEX for character in value
    ):
        raise ValueError("expected demo excursion revision must be sha256 hex")


def _validate_state(state: BybitDemoExcursionStateV119) -> None:
    if not isinstance(state.symbol, str) or state.symbol != state.symbol.strip().upper():
        raise ValueError("demo excursion state symbol must be normalized")
    if not state.symbol.endswith("USDT") or len(state.symbol) <= 4:
        raise ValueError("demo excursion state symbol must be normalized USDT")
    if state.side not in _ALLOWED_SIDES:
        raise ValueError("demo excursion state side must be LONG or SHORT")

    _finite_decimal(state.entry_price, "entry_price", positive=True)
    _finite_decimal(state.initial_quantity, "initial_quantity", positive=True)
    _finite_decimal(state.stop_fraction, "stop_fraction", positive=True)
    _finite_decimal(state.latest_gross_r, "latest_gross_r")
    _finite_decimal(state.observed_peak_favorable_r, "observed_peak_favorable_r")
    _finite_decimal(state.observed_trough_r, "observed_trough_r")
    _finite_decimal(state.latest_giveback_from_peak_r, "latest_giveback_from_peak_r")
    _finite_decimal(
        state.projected_initial_quantity_gross_pnl_usdt,
        "projected_initial_quantity_gross_pnl_usdt",
    )
    _finite_decimal(
        state.current_quantity_gross_pnl_usdt,
        "current_quantity_gross_pnl_usdt",
    )
    if state.latest_mark_price is not None:
        _finite_decimal(state.latest_mark_price, "latest_mark_price", positive=True)
    if state.current_quantity is not None:
        _finite_decimal(state.current_quantity, "current_quantity", nonnegative=True)
    if state.exchange_unrealised_pnl_usdt is not None:
        _finite_decimal(state.exchange_unrealised_pnl_usdt, "exchange_unrealised_pnl_usdt")

    if isinstance(state.observation_count, bool) or not isinstance(state.observation_count, int):
        raise ValueError("demo excursion state observation_count must be an integer")
    if state.observation_count < 0:
        raise ValueError("demo excursion state observation_count cannot be negative")
    if state.latest_server_time_ms is not None and (
        isinstance(state.latest_server_time_ms, bool)
        or not isinstance(state.latest_server_time_ms, int)
        or state.latest_server_time_ms < 0
    ):
        raise ValueError("demo excursion state latest_server_time_ms is invalid")
    if not isinstance(state.partial_close_seen, bool):
        raise ValueError("demo excursion state partial_close_seen must be boolean")
    if not state.diagnostics_only:
        raise ValueError("demo excursion state must remain diagnostics only")
    if state.exit_threshold_retuning_allowed:
        raise ValueError("demo excursion state cannot authorize exit retuning")
    if state.strategy_promotion_allowed:
        raise ValueError("demo excursion state cannot authorize strategy promotion")
    if state.live_mainnet_order_routing_allowed:
        raise ValueError("demo excursion state cannot permit live routing")


def _finite_decimal(
    value: Decimal,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"demo excursion state {field} must be a finite Decimal")
    if positive and value <= 0:
        raise ValueError(f"demo excursion state {field} must be positive")
    if nonnegative and value < 0:
        raise ValueError(f"demo excursion state {field} cannot be negative")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _text_field(value: dict[str, Any], field: str) -> str:
    raw = value[field]
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"demo excursion state invalid {field}")
    return raw


def _int_field(value: dict[str, Any], field: str) -> int:
    raw = value[field]
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"demo excursion state invalid {field}")
    return raw


def _optional_int_field(value: dict[str, Any], field: str) -> int | None:
    raw = value[field]
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"demo excursion state invalid {field}")
    return raw


def _decimal_field(value: dict[str, Any], field: str) -> Decimal:
    raw = value[field]
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"demo excursion state invalid {field}")
    return _parse_decimal(raw, field)


def _optional_decimal_field(value: dict[str, Any], field: str) -> Decimal | None:
    raw = value[field]
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"demo excursion state invalid {field}")
    return _parse_decimal(raw, field)


def _parse_decimal(raw: str, field: str) -> Decimal:
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"demo excursion state invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"demo excursion state non-finite {field}")
    return parsed


def _bool_field(value: dict[str, Any], field: str) -> bool:
    raw = value[field]
    if not isinstance(raw, bool):
        raise ValueError(f"demo excursion state invalid {field}")
    return raw


__all__ = [
    "BybitDemoExcursionCheckpointV119",
    "BybitDemoExcursionStateV119",
    "build_excursion_checkpoint_v119",
    "canonical_json_v119",
    "decode_excursion_state_v119",
    "encode_excursion_state_v119",
    "excursion_revision_v119",
    "validate_demo_order_link_v119",
    "validate_excursion_revision_v119",
]
