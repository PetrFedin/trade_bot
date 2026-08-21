from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan

_ALLOWED_EXIT_MODES = frozenset({"OPEN_ENDED_RUNNER", "FIXED_20_TARGET"})


@dataclass(frozen=True)
class BybitEntryRecoveryEnvelope:
    """Frozen pre-submit facts required to protect a recovered broker ENTRY without guessing."""

    entry_order_link_id: str
    order_side: str
    approved_order_quantity: Decimal
    trade_plan: CryptoTradePlan
    instrument: BybitInstrumentSpec
    strategy_config: CryptoPerpStrategyConfig
    planned_exit_mode: str
    schema_version: int = 1
    demo_only: bool = True
    live_mainnet_order_routing_allowed: bool = False

    def validate(self) -> None:
        if not self.entry_order_link_id.startswith("ASTRA-DEMO-E-"):
            raise ValueError("entry recovery envelope requires ASTRA-DEMO-E orderLinkId")
        if self.order_side not in {"Buy", "Sell"}:
            raise ValueError("entry recovery envelope order side must be Buy or Sell")
        if (
            not self.approved_order_quantity.is_finite()
            or self.approved_order_quantity <= 0
        ):
            raise ValueError("entry recovery approved quantity must be positive and finite")
        if self.schema_version != 1:
            raise ValueError("unsupported entry recovery envelope schema version")
        if self.planned_exit_mode not in _ALLOWED_EXIT_MODES:
            raise ValueError("entry recovery envelope exit mode is invalid")
        self.instrument.validate()
        self.strategy_config.validate()
        _validate_trade_plan(self.trade_plan)
        if self.trade_plan.symbol != self.instrument.symbol:
            raise ValueError("entry recovery trade plan/instrument symbol mismatch")
        expected_side = "Buy" if self.trade_plan.side is CryptoSide.LONG else "Sell"
        if self.order_side != expected_side:
            raise ValueError("entry recovery order side does not match trade plan")
        if self.approved_order_quantity != self.trade_plan.reference_quantity:
            raise ValueError("entry recovery approved quantity does not match frozen trade plan")
        if not self.demo_only or self.live_mainnet_order_routing_allowed:
            raise ValueError("entry recovery envelope cannot grant live routing")


@dataclass(frozen=True)
class BybitEntryRecoveryReceipt:
    entry_order_link_id: str
    record_sha256: str
    idempotent_existing_record: bool
    demo_only: bool = True
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitEntryRecoveryRecord:
    envelope: BybitEntryRecoveryEnvelope
    record_sha256: str
    demo_only: bool = True
    live_mainnet_order_routing_allowed: bool = False


def encode_entry_recovery_envelope(
    envelope: BybitEntryRecoveryEnvelope,
) -> tuple[str, str]:
    envelope.validate()
    payload = {
        "schema_version": envelope.schema_version,
        "entry_order_link_id": envelope.entry_order_link_id,
        "order_side": envelope.order_side,
        "approved_order_quantity": str(envelope.approved_order_quantity),
        "planned_exit_mode": envelope.planned_exit_mode,
        "trade_plan": _trade_plan_payload(envelope.trade_plan),
        "instrument": _instrument_payload(envelope.instrument),
        "strategy_config": _strategy_config_payload(envelope.strategy_config),
        "demo_only": True,
        "live_mainnet_order_routing_allowed": False,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    record_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, record_sha


def decode_entry_recovery_envelope(
    canonical: str,
    *,
    expected_sha256: str | None = None,
) -> BybitEntryRecoveryRecord:
    try:
        payload = json.loads(canonical)
    except json.JSONDecodeError as exc:
        raise ValueError("entry recovery envelope is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("entry recovery envelope payload must be an object")
    actual_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if expected_sha256 is not None and actual_sha != expected_sha256:
        raise ValueError("entry recovery envelope checksum mismatch")
    envelope = _envelope_from_payload(payload)
    envelope.validate()
    normalized, normalized_sha = encode_entry_recovery_envelope(envelope)
    if normalized != canonical or normalized_sha != actual_sha:
        raise ValueError("entry recovery envelope is not canonical")
    return BybitEntryRecoveryRecord(envelope=envelope, record_sha256=actual_sha)


def _validate_trade_plan(plan: CryptoTradePlan) -> None:
    if plan.symbol != plan.symbol.strip().upper() or not plan.symbol.endswith("USDT"):
        raise ValueError("entry recovery trade plan symbol must be normalized USDT")
    if not isinstance(plan.side, CryptoSide):
        raise ValueError("entry recovery trade plan side is invalid")
    if not isinstance(plan.decision_time, str) or not plan.decision_time:
        raise ValueError("entry recovery trade plan decision time is required")
    decimals = (
        plan.reference_price,
        plan.notional_usdt,
        plan.reference_quantity,
        plan.risk_budget_usdt,
        plan.stop_fraction,
        plan.estimated_round_trip_cost_usdt,
        plan.estimated_stop_loss_after_cost_usdt,
        plan.target_net_profit_usd,
        plan.required_move_fraction,
        plan.expected_move_fraction,
        plan.expected_net_edge_usd,
        plan.quality_score,
    )
    if any(not value.is_finite() for value in decimals):
        raise ValueError("entry recovery trade plan contains non-finite values")
    positive = (
        plan.reference_price,
        plan.notional_usdt,
        plan.reference_quantity,
        plan.risk_budget_usdt,
        plan.stop_fraction,
        plan.target_net_profit_usd,
        plan.required_move_fraction,
        plan.expected_move_fraction,
    )
    if any(value <= 0 for value in positive):
        raise ValueError("entry recovery trade plan required values must be positive")
    if plan.estimated_round_trip_cost_usdt < 0 or plan.estimated_stop_loss_after_cost_usdt < 0:
        raise ValueError("entry recovery trade plan modeled costs cannot be negative")


def _trade_plan_payload(plan: CryptoTradePlan) -> dict[str, object]:
    return {
        "symbol": plan.symbol,
        "side": plan.side.value,
        "decision_time": plan.decision_time,
        "reference_price": str(plan.reference_price),
        "notional_usdt": str(plan.notional_usdt),
        "reference_quantity": str(plan.reference_quantity),
        "risk_budget_usdt": str(plan.risk_budget_usdt),
        "stop_fraction": str(plan.stop_fraction),
        "estimated_round_trip_cost_usdt": str(plan.estimated_round_trip_cost_usdt),
        "estimated_stop_loss_after_cost_usdt": str(plan.estimated_stop_loss_after_cost_usdt),
        "target_net_profit_usd": str(plan.target_net_profit_usd),
        "required_move_fraction": str(plan.required_move_fraction),
        "expected_move_fraction": str(plan.expected_move_fraction),
        "expected_net_edge_usd": str(plan.expected_net_edge_usd),
        "quality_score": str(plan.quality_score),
    }


def _instrument_payload(spec: BybitInstrumentSpec) -> dict[str, object]:
    return {
        "symbol": spec.symbol,
        "status": spec.status,
        "contract_type": spec.contract_type,
        "base_coin": spec.base_coin,
        "quote_coin": spec.quote_coin,
        "settle_coin": spec.settle_coin,
        "tick_size": str(spec.tick_size),
        "min_order_qty": str(spec.min_order_qty),
        "qty_step": str(spec.qty_step),
        "min_notional_value": str(spec.min_notional_value),
        "max_market_order_qty": str(spec.max_market_order_qty),
        "max_leverage": str(spec.max_leverage),
        "funding_interval_minutes": spec.funding_interval_minutes,
    }


def _strategy_config_payload(config: CryptoPerpStrategyConfig) -> dict[str, object]:
    return {
        "fast_ema_bars": config.fast_ema_bars,
        "slow_ema_bars": config.slow_ema_bars,
        "momentum_bars": config.momentum_bars,
        "breakout_bars": config.breakout_bars,
        "atr_bars": config.atr_bars,
        "turnover_bars": config.turnover_bars,
        "minimum_average_turnover_usdt": str(config.minimum_average_turnover_usdt),
        "minimum_atr_fraction": str(config.minimum_atr_fraction),
        "maximum_atr_fraction": str(config.maximum_atr_fraction),
        "minimum_abs_momentum": str(config.minimum_abs_momentum),
        "minimum_quality_score": str(config.minimum_quality_score),
        "maximum_one_bar_atr_multiple": str(config.maximum_one_bar_atr_multiple),
        "risk_fraction_per_trade": str(config.risk_fraction_per_trade),
        "maximum_notional_to_equity": str(config.maximum_notional_to_equity),
        "hard_stop_atr_multiple": str(config.hard_stop_atr_multiple),
        "expected_move_atr_multiple": str(config.expected_move_atr_multiple),
        "target_net_profit_usd": str(config.target_net_profit_usd),
        "taker_fee_rate": str(config.taker_fee_rate),
        "slippage_bps_per_fill": str(config.slippage_bps_per_fill),
        "maximum_concurrent_positions": config.maximum_concurrent_positions,
        "allowed_entry_sides": [side.value for side in config.allowed_entry_sides],
    }


def _envelope_from_payload(payload: Mapping[str, Any]) -> BybitEntryRecoveryEnvelope:
    plan = _mapping(payload, "trade_plan")
    instrument = _mapping(payload, "instrument")
    config = _mapping(payload, "strategy_config")
    sides = config.get("allowed_entry_sides")
    if not isinstance(sides, list) or not sides:
        raise ValueError("entry recovery strategy allowed sides are invalid")
    try:
        allowed_sides = tuple(CryptoSide(str(value)) for value in sides)
    except ValueError as exc:
        raise ValueError("entry recovery strategy allowed side is invalid") from exc
    trade_plan = CryptoTradePlan(
        symbol=_text(plan, "symbol"),
        side=CryptoSide(_text(plan, "side")),
        decision_time=_text(plan, "decision_time"),
        reference_price=_decimal(plan, "reference_price"),
        notional_usdt=_decimal(plan, "notional_usdt"),
        reference_quantity=_decimal(plan, "reference_quantity"),
        risk_budget_usdt=_decimal(plan, "risk_budget_usdt"),
        stop_fraction=_decimal(plan, "stop_fraction"),
        estimated_round_trip_cost_usdt=_decimal(plan, "estimated_round_trip_cost_usdt"),
        estimated_stop_loss_after_cost_usdt=_decimal(
            plan, "estimated_stop_loss_after_cost_usdt"
        ),
        target_net_profit_usd=_decimal(plan, "target_net_profit_usd"),
        required_move_fraction=_decimal(plan, "required_move_fraction"),
        expected_move_fraction=_decimal(plan, "expected_move_fraction"),
        expected_net_edge_usd=_decimal(plan, "expected_net_edge_usd"),
        quality_score=_decimal(plan, "quality_score"),
    )
    instrument_spec = BybitInstrumentSpec(
        symbol=_text(instrument, "symbol"),
        status=_text(instrument, "status"),
        contract_type=_text(instrument, "contract_type"),
        base_coin=_text(instrument, "base_coin"),
        quote_coin=_text(instrument, "quote_coin"),
        settle_coin=_text(instrument, "settle_coin"),
        tick_size=_decimal(instrument, "tick_size"),
        min_order_qty=_decimal(instrument, "min_order_qty"),
        qty_step=_decimal(instrument, "qty_step"),
        min_notional_value=_decimal(instrument, "min_notional_value"),
        max_market_order_qty=_decimal(instrument, "max_market_order_qty"),
        max_leverage=_decimal(instrument, "max_leverage"),
        funding_interval_minutes=_integer(instrument, "funding_interval_minutes"),
    )
    strategy_config = CryptoPerpStrategyConfig(
        fast_ema_bars=_integer(config, "fast_ema_bars"),
        slow_ema_bars=_integer(config, "slow_ema_bars"),
        momentum_bars=_integer(config, "momentum_bars"),
        breakout_bars=_integer(config, "breakout_bars"),
        atr_bars=_integer(config, "atr_bars"),
        turnover_bars=_integer(config, "turnover_bars"),
        minimum_average_turnover_usdt=_decimal(config, "minimum_average_turnover_usdt"),
        minimum_atr_fraction=_decimal(config, "minimum_atr_fraction"),
        maximum_atr_fraction=_decimal(config, "maximum_atr_fraction"),
        minimum_abs_momentum=_decimal(config, "minimum_abs_momentum"),
        minimum_quality_score=_decimal(config, "minimum_quality_score"),
        maximum_one_bar_atr_multiple=_decimal(config, "maximum_one_bar_atr_multiple"),
        risk_fraction_per_trade=_decimal(config, "risk_fraction_per_trade"),
        maximum_notional_to_equity=_decimal(config, "maximum_notional_to_equity"),
        hard_stop_atr_multiple=_decimal(config, "hard_stop_atr_multiple"),
        expected_move_atr_multiple=_decimal(config, "expected_move_atr_multiple"),
        target_net_profit_usd=_decimal(config, "target_net_profit_usd"),
        taker_fee_rate=_decimal(config, "taker_fee_rate"),
        slippage_bps_per_fill=_decimal(config, "slippage_bps_per_fill"),
        maximum_concurrent_positions=_integer(config, "maximum_concurrent_positions"),
        allowed_entry_sides=allowed_sides,
    )
    return BybitEntryRecoveryEnvelope(
        entry_order_link_id=_text(payload, "entry_order_link_id"),
        order_side=_text(payload, "order_side"),
        approved_order_quantity=_decimal(payload, "approved_order_quantity"),
        trade_plan=trade_plan,
        instrument=instrument_spec,
        strategy_config=strategy_config,
        planned_exit_mode=_text(payload, "planned_exit_mode"),
        schema_version=_integer(payload, "schema_version"),
        demo_only=_boolean(payload, "demo_only"),
        live_mainnet_order_routing_allowed=_boolean(
            payload, "live_mainnet_order_routing_allowed"
        ),
    )


def _mapping(payload: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    value = payload.get(field)
    if not isinstance(value, Mapping):
        raise ValueError(f"entry recovery envelope missing {field}")
    return value


def _text(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"entry recovery envelope invalid {field}")
    return value


def _decimal(payload: Mapping[str, Any], field: str) -> Decimal:
    value = payload.get(field)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"entry recovery envelope invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"entry recovery envelope non-finite {field}")
    return parsed


def _integer(payload: Mapping[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool):
        raise ValueError(f"entry recovery envelope invalid {field}")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"entry recovery envelope invalid {field}") from exc
    return parsed


def _boolean(payload: Mapping[str, Any], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"entry recovery envelope invalid {field}")
    return value
