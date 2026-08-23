from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_live_evidence_ranking import CryptoLiveOpportunitySnapshot
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    build_trade_plan,
    evaluate_crypto_signal,
    execution_levels,
)

_ZERO = Decimal("0")
_INTERVAL = timedelta(minutes=5)
_HORIZONS = (15, 60, 240)
_HEX = frozenset("0123456789abcdef")
_TRACKABLE_STATES = frozenset(
    {
        "QUALIFIED_POSITIVE_EVIDENCE",
        "QUALIFIED_MIXED_EVIDENCE",
        "NO_SAMPLE_SUFFICIENT_EXACT_CELL",
        "DERIVATIVES_CONTEXT_INCOMPLETE",
    }
)
_FIRST_TOUCH_STATES = frozenset(
    {
        "TARGET_FIRST",
        "STOP_FIRST",
        "AMBIGUOUS_SAME_BAR",
        "NEITHER",
        "INCOMPLETE",
    }
)


@dataclass(frozen=True)
class CryptoShadowSourceCandidate:
    source_snapshot_id: str
    evidence_rank: int
    market_rank: int
    qualification_state: str
    symbol: str
    side: str
    decision_time: str
    signal_quality_score: Decimal
    planned_notional_usdt: Decimal
    risk_budget_usdt: Decimal
    estimated_round_trip_cost_usdt: Decimal

    def validate(self) -> None:
        _validate_sha(self.source_snapshot_id, "source snapshot")
        if not 1 <= self.evidence_rank <= 50:
            raise ValueError("shadow source evidence rank must be within [1, 50]")
        if not 1 <= self.market_rank <= 50:
            raise ValueError("shadow source market rank must be within [1, 50]")
        if self.qualification_state not in _TRACKABLE_STATES:
            raise ValueError("shadow source qualification state is not trackable")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("shadow source symbol is invalid")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("shadow source side is invalid")
        _parse_time(self.decision_time)
        for name, value in (
            ("signal_quality_score", self.signal_quality_score),
            ("planned_notional_usdt", self.planned_notional_usdt),
            ("risk_budget_usdt", self.risk_budget_usdt),
            ("estimated_round_trip_cost_usdt", self.estimated_round_trip_cost_usdt),
        ):
            if not value.is_finite():
                raise ValueError(f"shadow source {name} must be finite")
        if self.planned_notional_usdt <= 0 or self.risk_budget_usdt <= 0:
            raise ValueError("shadow source notional and risk budget must be positive")
        if self.estimated_round_trip_cost_usdt < 0:
            raise ValueError("shadow source modeled round-trip cost cannot be negative")


@dataclass(frozen=True)
class CryptoShadowSeed:
    source_snapshot_id: str
    source_evidence_rank: int
    source_market_rank: int
    source_qualification_state: str
    symbol: str
    side: str
    decision_bar_start_at: str
    signal_available_at: str
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    planned_notional_usdt: Decimal
    risk_budget_usdt: Decimal
    estimated_round_trip_cost_usdt: Decimal
    target_net_profit_usd: Decimal
    signal_quality_score: Decimal
    prospective: bool = True
    operator_review_required: bool = True
    trade_actionable: bool = False
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False
    bybit_live_order_routing_allowed: bool = False

    def validate(self) -> None:
        _validate_sha(self.source_snapshot_id, "source snapshot")
        if not 1 <= self.source_evidence_rank <= 50:
            raise ValueError("shadow seed evidence rank must be within [1, 50]")
        if not 1 <= self.source_market_rank <= 50:
            raise ValueError("shadow seed market rank must be within [1, 50]")
        if self.source_qualification_state not in _TRACKABLE_STATES:
            raise ValueError("shadow seed source state is not trackable")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("shadow seed symbol is invalid")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("shadow seed side is invalid")
        decision = _parse_time(self.decision_bar_start_at)
        available = _parse_time(self.signal_available_at)
        if available != decision + _INTERVAL:
            raise ValueError("shadow seed signal availability must follow the decision bar")
        for name, value in (
            ("entry_price", self.entry_price),
            ("stop_price", self.stop_price),
            ("target_price", self.target_price),
            ("planned_notional_usdt", self.planned_notional_usdt),
            ("risk_budget_usdt", self.risk_budget_usdt),
            ("estimated_round_trip_cost_usdt", self.estimated_round_trip_cost_usdt),
            ("target_net_profit_usd", self.target_net_profit_usd),
            ("signal_quality_score", self.signal_quality_score),
        ):
            if not value.is_finite():
                raise ValueError(f"shadow seed {name} must be finite")
        if min(self.entry_price, self.stop_price, self.target_price) <= 0:
            raise ValueError("shadow seed price levels must be positive")
        if self.planned_notional_usdt <= 0 or self.risk_budget_usdt <= 0:
            raise ValueError("shadow seed notional and risk budget must be positive")
        if self.estimated_round_trip_cost_usdt < 0:
            raise ValueError("shadow seed modeled round-trip cost cannot be negative")
        if self.target_net_profit_usd <= 0:
            raise ValueError("shadow seed target net profit must be positive")
        if self.side == "LONG":
            if not self.stop_price < self.entry_price < self.target_price:
                raise ValueError("LONG shadow seed levels are inconsistent")
        elif not self.target_price < self.entry_price < self.stop_price:
            raise ValueError("SHORT shadow seed levels are inconsistent")
        _validate_safety_flags(
            prospective=self.prospective,
            operator_review_required=self.operator_review_required,
            trade_actionable=self.trade_actionable,
            demo_activation_allowed=self.demo_activation_allowed,
            live_activation_allowed=self.live_activation_allowed,
            bybit_live_order_routing_allowed=self.bybit_live_order_routing_allowed,
        )

    @property
    def seed_id(self) -> str:
        canonical = json.dumps(
            self.to_payload(include_seed_id=False),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def to_payload(self, *, include_seed_id: bool = True) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "schema": "BYBIT_PROSPECTIVE_SHADOW_SEED_V112",
            "source_snapshot_id": self.source_snapshot_id,
            "source_evidence_rank": self.source_evidence_rank,
            "source_market_rank": self.source_market_rank,
            "source_qualification_state": self.source_qualification_state,
            "symbol": self.symbol,
            "side": self.side,
            "decision_bar_start_at": self.decision_bar_start_at,
            "signal_available_at": self.signal_available_at,
            "entry_price": str(self.entry_price),
            "stop_price": str(self.stop_price),
            "target_price": str(self.target_price),
            "planned_notional_usdt": str(self.planned_notional_usdt),
            "risk_budget_usdt": str(self.risk_budget_usdt),
            "estimated_round_trip_cost_usdt": str(
                self.estimated_round_trip_cost_usdt
            ),
            "target_net_profit_usd": str(self.target_net_profit_usd),
            "signal_quality_score": str(self.signal_quality_score),
            "prospective": self.prospective,
            "operator_review_required": self.operator_review_required,
            "trade_actionable": self.trade_actionable,
            "demo_activation_allowed": self.demo_activation_allowed,
            "live_activation_allowed": self.live_activation_allowed,
            "bybit_live_order_routing_allowed": self.bybit_live_order_routing_allowed,
            "causal_claim_allowed": False,
            "predictive_guarantee_allowed": False,
        }
        if include_seed_id:
            payload["seed_id"] = self.seed_id
        return payload


@dataclass(frozen=True)
class CryptoShadowHorizonOutcome:
    horizon_minutes: int
    complete: bool
    close_time: str | None
    close_price: Decimal | None
    directional_return_fraction: Decimal | None
    gross_pnl_usdt: Decimal | None
    modeled_net_pnl_usdt: Decimal | None

    def validate(self) -> None:
        if self.horizon_minutes not in _HORIZONS:
            raise ValueError("shadow horizon is unsupported")
        values = (
            self.close_time,
            self.close_price,
            self.directional_return_fraction,
            self.gross_pnl_usdt,
            self.modeled_net_pnl_usdt,
        )
        if self.complete and any(value is None for value in values):
            raise ValueError("complete shadow horizon requires all outcome values")
        if not self.complete and any(value is not None for value in values):
            raise ValueError("incomplete shadow horizon cannot carry outcome values")
        if self.close_time is not None:
            _parse_time(self.close_time)
        for name, value in (
            ("close_price", self.close_price),
            ("directional_return_fraction", self.directional_return_fraction),
            ("gross_pnl_usdt", self.gross_pnl_usdt),
            ("modeled_net_pnl_usdt", self.modeled_net_pnl_usdt),
        ):
            if value is not None and not value.is_finite():
                raise ValueError(f"shadow horizon {name} must be finite")
        if self.close_price is not None and self.close_price <= 0:
            raise ValueError("shadow horizon close price must be positive")

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "horizon_minutes": self.horizon_minutes,
            "complete": self.complete,
            "close_time": self.close_time,
            "close_price": _decimal_text(self.close_price),
            "directional_return_fraction": _decimal_text(
                self.directional_return_fraction
            ),
            "gross_pnl_usdt": _decimal_text(self.gross_pnl_usdt),
            "modeled_net_pnl_usdt": _decimal_text(self.modeled_net_pnl_usdt),
        }


@dataclass(frozen=True)
class CryptoShadowOutcome:
    seed_id: str
    source_snapshot_id: str
    source_qualification_state: str
    symbol: str
    side: str
    signal_available_at: str
    observed_through: str
    first_touch_state: str
    target_hit_at: str | None
    stop_hit_at: str | None
    first_touch_modeled_net_pnl_usdt: Decimal | None
    mfe_r: Decimal | None
    mae_r: Decimal | None
    completed_bar_count: int
    horizons: tuple[CryptoShadowHorizonOutcome, ...]
    prospective: bool = True
    operator_review_required: bool = True
    trade_actionable: bool = False
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False
    bybit_live_order_routing_allowed: bool = False

    def validate(self) -> None:
        _validate_sha(self.seed_id, "shadow seed")
        _validate_sha(self.source_snapshot_id, "source snapshot")
        if self.source_qualification_state not in _TRACKABLE_STATES:
            raise ValueError("shadow outcome source state is not trackable")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("shadow outcome symbol is invalid")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("shadow outcome side is invalid")
        available = _parse_time(self.signal_available_at)
        observed = _parse_time(self.observed_through)
        if observed < available:
            raise ValueError("shadow outcome cannot be observed before signal availability")
        if self.first_touch_state not in _FIRST_TOUCH_STATES:
            raise ValueError("shadow outcome first-touch state is invalid")
        if self.target_hit_at is not None:
            _parse_time(self.target_hit_at)
        if self.stop_hit_at is not None:
            _parse_time(self.stop_hit_at)
        if self.first_touch_state == "TARGET_FIRST":
            if self.target_hit_at is None or self.stop_hit_at is not None:
                raise ValueError("TARGET_FIRST shadow outcome has inconsistent timestamps")
        if self.first_touch_state == "STOP_FIRST":
            if self.stop_hit_at is None or self.target_hit_at is not None:
                raise ValueError("STOP_FIRST shadow outcome has inconsistent timestamps")
        if self.first_touch_state == "AMBIGUOUS_SAME_BAR":
            if self.target_hit_at is None or self.target_hit_at != self.stop_hit_at:
                raise ValueError("ambiguous shadow touch requires one shared bar timestamp")
        if self.first_touch_state in {"NEITHER", "INCOMPLETE"}:
            if self.target_hit_at is not None or self.stop_hit_at is not None:
                raise ValueError("untouched shadow outcome cannot carry hit timestamps")
        if self.first_touch_modeled_net_pnl_usdt is not None:
            if not self.first_touch_modeled_net_pnl_usdt.is_finite():
                raise ValueError("shadow first-touch modeled PnL must be finite")
            if self.first_touch_state not in {"TARGET_FIRST", "STOP_FIRST"}:
                raise ValueError("shadow first-touch PnL requires an ordered touch")
        for name, value in (("mfe_r", self.mfe_r), ("mae_r", self.mae_r)):
            if value is not None and not value.is_finite():
                raise ValueError(f"shadow outcome {name} must be finite")
        if self.mfe_r is not None and self.mfe_r < 0:
            raise ValueError("shadow outcome MFE R cannot be negative")
        if self.mae_r is not None and self.mae_r > 0:
            raise ValueError("shadow outcome MAE R cannot be positive")
        if self.completed_bar_count < 0:
            raise ValueError("shadow outcome completed bar count cannot be negative")
        if tuple(item.horizon_minutes for item in self.horizons) != _HORIZONS:
            raise ValueError("shadow outcome horizons must be 15m/60m/240m")
        for item in self.horizons:
            item.validate()
        _validate_safety_flags(
            prospective=self.prospective,
            operator_review_required=self.operator_review_required,
            trade_actionable=self.trade_actionable,
            demo_activation_allowed=self.demo_activation_allowed,
            live_activation_allowed=self.live_activation_allowed,
            bybit_live_order_routing_allowed=self.bybit_live_order_routing_allowed,
        )

    @property
    def final(self) -> bool:
        return self.horizons[-1].complete

    @property
    def evaluation_id(self) -> str:
        canonical = json.dumps(
            self.to_payload(include_evaluation_id=False),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def to_payload(self, *, include_evaluation_id: bool = True) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "schema": "BYBIT_PROSPECTIVE_SHADOW_OUTCOME_V112",
            "seed_id": self.seed_id,
            "source_snapshot_id": self.source_snapshot_id,
            "source_qualification_state": self.source_qualification_state,
            "symbol": self.symbol,
            "side": self.side,
            "signal_available_at": self.signal_available_at,
            "observed_through": self.observed_through,
            "first_touch_state": self.first_touch_state,
            "target_hit_at": self.target_hit_at,
            "stop_hit_at": self.stop_hit_at,
            "first_touch_modeled_net_pnl_usdt": _decimal_text(
                self.first_touch_modeled_net_pnl_usdt
            ),
            "mfe_r": _decimal_text(self.mfe_r),
            "mae_r": _decimal_text(self.mae_r),
            "completed_bar_count": self.completed_bar_count,
            "horizons": [item.to_payload() for item in self.horizons],
            "final": self.final,
            "prospective": self.prospective,
            "operator_review_required": self.operator_review_required,
            "trade_actionable": self.trade_actionable,
            "demo_activation_allowed": self.demo_activation_allowed,
            "live_activation_allowed": self.live_activation_allowed,
            "bybit_live_order_routing_allowed": self.bybit_live_order_routing_allowed,
            "causal_claim_allowed": False,
            "predictive_guarantee_allowed": False,
        }
        if include_evaluation_id:
            payload["evaluation_id"] = self.evaluation_id
        return payload


def source_candidates_from_live_snapshot(
    snapshot: CryptoLiveOpportunitySnapshot,
) -> tuple[CryptoShadowSourceCandidate, ...]:
    snapshot.validate()
    result: list[CryptoShadowSourceCandidate] = []
    for item in snapshot.opportunities:
        if item.qualification_state not in _TRACKABLE_STATES:
            continue
        values = (
            item.signal_side,
            item.decision_time,
            item.signal_quality_score,
            item.planned_notional_usdt,
            item.risk_budget_usdt,
            item.estimated_round_trip_cost_usdt,
        )
        if any(value is None for value in values):
            continue
        source = CryptoShadowSourceCandidate(
            source_snapshot_id=snapshot.snapshot_id,
            evidence_rank=item.evidence_rank,
            market_rank=item.market_rank,
            qualification_state=item.qualification_state,
            symbol=item.symbol,
            side=str(item.signal_side),
            decision_time=str(item.decision_time),
            signal_quality_score=_as_decimal(item.signal_quality_score),
            planned_notional_usdt=_as_decimal(item.planned_notional_usdt),
            risk_budget_usdt=_as_decimal(item.risk_budget_usdt),
            estimated_round_trip_cost_usdt=_as_decimal(
                item.estimated_round_trip_cost_usdt
            ),
        )
        source.validate()
        result.append(source)
    return tuple(result)


def reconstruct_crypto_shadow_seed(
    source: CryptoShadowSourceCandidate,
    bars: Sequence[BybitKlineBar],
    *,
    strategy_config: CryptoPerpStrategyConfig | None = None,
) -> CryptoShadowSeed:
    """Rebuild fixed-strategy levels at the source decision without using future bars."""

    source.validate()
    config = _fixed_config(strategy_config)
    decision = _parse_time(source.decision_time)
    ordered = _validated_symbol_bars(bars, symbol=source.symbol)
    history = tuple(bar for bar in ordered if bar.start_time <= decision)
    if not history or history[-1].start_time != decision:
        raise ValueError("shadow seed requires the exact source decision bar")
    evaluation = evaluate_crypto_signal(history, config)
    if not evaluation.eligible or evaluation.signal is None:
        raise ValueError("shadow seed fixed strategy no longer reproduces the source signal")
    signal = evaluation.signal
    if signal.decision_time != source.decision_time or signal.side.value != source.side:
        raise ValueError("shadow seed source signal identity does not reproduce exactly")
    if signal.quality_score != source.signal_quality_score:
        raise ValueError("shadow seed source signal quality does not reproduce exactly")
    plan_evaluation = build_trade_plan(
        signal,
        equity_usdt=source.risk_budget_usdt / config.risk_fraction_per_trade,
        config=config,
    )
    if not plan_evaluation.eligible or plan_evaluation.plan is None:
        raise ValueError("shadow seed fixed trade plan no longer reproduces")
    plan = plan_evaluation.plan
    if plan.notional_usdt != source.planned_notional_usdt:
        raise ValueError("shadow seed source notional does not reproduce exactly")
    if plan.risk_budget_usdt != source.risk_budget_usdt:
        raise ValueError("shadow seed source risk budget does not reproduce exactly")
    if plan.estimated_round_trip_cost_usdt != source.estimated_round_trip_cost_usdt:
        raise ValueError("shadow seed source modeled cost does not reproduce exactly")
    levels = execution_levels(plan, entry_price=signal.reference_price, config=config)
    seed = CryptoShadowSeed(
        source_snapshot_id=source.source_snapshot_id,
        source_evidence_rank=source.evidence_rank,
        source_market_rank=source.market_rank,
        source_qualification_state=source.qualification_state,
        symbol=source.symbol,
        side=source.side,
        decision_bar_start_at=source.decision_time,
        signal_available_at=(decision + _INTERVAL).isoformat(),
        entry_price=levels.entry_price,
        stop_price=levels.stop_price,
        target_price=levels.target_price,
        planned_notional_usdt=plan.notional_usdt,
        risk_budget_usdt=plan.risk_budget_usdt,
        estimated_round_trip_cost_usdt=plan.estimated_round_trip_cost_usdt,
        target_net_profit_usd=plan.target_net_profit_usd,
        signal_quality_score=signal.quality_score,
    )
    seed.validate()
    return seed


def evaluate_crypto_shadow_outcome(
    seed: CryptoShadowSeed,
    bars: Sequence[BybitKlineBar],
    *,
    observed_through: datetime,
) -> CryptoShadowOutcome:
    """Evaluate only completed bars available after the prospective signal became knowable."""

    seed.validate()
    if observed_through.tzinfo is None or observed_through.utcoffset() is None:
        raise ValueError("shadow observed_through must be timezone-aware")
    observed = observed_through.astimezone(UTC)
    available = _parse_time(seed.signal_available_at)
    if observed < available:
        raise ValueError("shadow evaluation cannot precede signal availability")
    maximum_end = available + timedelta(minutes=max(_HORIZONS))
    ordered = _validated_symbol_bars(bars, symbol=seed.symbol)
    completed = tuple(
        bar
        for bar in ordered
        if bar.start_time >= available
        and bar.start_time + _INTERVAL <= observed
        and bar.start_time < maximum_end
    )
    first_touch, target_at, stop_at = _first_touch(seed, completed)
    first_touch_pnl = _first_touch_modeled_pnl(seed, first_touch)
    mfe_r, mae_r = _excursions_r(seed, completed)
    horizons = tuple(
        _horizon_outcome(seed, completed, horizon_minutes=horizon)
        for horizon in _HORIZONS
    )
    outcome = CryptoShadowOutcome(
        seed_id=seed.seed_id,
        source_snapshot_id=seed.source_snapshot_id,
        source_qualification_state=seed.source_qualification_state,
        symbol=seed.symbol,
        side=seed.side,
        signal_available_at=seed.signal_available_at,
        observed_through=observed.isoformat(),
        first_touch_state=first_touch,
        target_hit_at=target_at,
        stop_hit_at=stop_at,
        first_touch_modeled_net_pnl_usdt=first_touch_pnl,
        mfe_r=mfe_r,
        mae_r=mae_r,
        completed_bar_count=len(completed),
        horizons=horizons,
    )
    outcome.validate()
    return outcome


def _first_touch(
    seed: CryptoShadowSeed,
    bars: Sequence[BybitKlineBar],
) -> tuple[str, str | None, str | None]:
    for bar in bars:
        if seed.side == "LONG":
            target = bar.high >= seed.target_price
            stop = bar.low <= seed.stop_price
        else:
            target = bar.low <= seed.target_price
            stop = bar.high >= seed.stop_price
        timestamp = bar.start_time.isoformat()
        if target and stop:
            return "AMBIGUOUS_SAME_BAR", timestamp, timestamp
        if target:
            return "TARGET_FIRST", timestamp, None
        if stop:
            return "STOP_FIRST", None, timestamp
    return ("NEITHER", None, None) if bars else ("INCOMPLETE", None, None)


def _first_touch_modeled_pnl(
    seed: CryptoShadowSeed,
    state: str,
) -> Decimal | None:
    if state == "TARGET_FIRST":
        return seed.target_net_profit_usd
    if state != "STOP_FIRST":
        return None
    directional = _directional_return(seed, seed.stop_price)
    return seed.planned_notional_usdt * directional - seed.estimated_round_trip_cost_usdt


def _excursions_r(
    seed: CryptoShadowSeed,
    bars: Sequence[BybitKlineBar],
) -> tuple[Decimal | None, Decimal | None]:
    if not bars:
        return None, None
    risk_distance = abs(seed.entry_price - seed.stop_price)
    if risk_distance <= 0:
        raise ValueError("shadow seed stop distance must be positive")
    if seed.side == "LONG":
        favorable = max(bar.high for bar in bars) - seed.entry_price
        adverse = min(bar.low for bar in bars) - seed.entry_price
    else:
        favorable = seed.entry_price - min(bar.low for bar in bars)
        adverse = seed.entry_price - max(bar.high for bar in bars)
    return max(favorable, _ZERO) / risk_distance, min(adverse, _ZERO) / risk_distance


def _horizon_outcome(
    seed: CryptoShadowSeed,
    bars: Sequence[BybitKlineBar],
    *,
    horizon_minutes: int,
) -> CryptoShadowHorizonOutcome:
    available = _parse_time(seed.signal_available_at)
    horizon_end = available + timedelta(minutes=horizon_minutes)
    expected_count = horizon_minutes // 5
    expected_starts = tuple(available + index * _INTERVAL for index in range(expected_count))
    by_start = {bar.start_time: bar for bar in bars if bar.start_time < horizon_end}
    if any(start not in by_start for start in expected_starts):
        return CryptoShadowHorizonOutcome(
            horizon_minutes=horizon_minutes,
            complete=False,
            close_time=None,
            close_price=None,
            directional_return_fraction=None,
            gross_pnl_usdt=None,
            modeled_net_pnl_usdt=None,
        )
    closing_bar = by_start[expected_starts[-1]]
    directional = _directional_return(seed, closing_bar.close)
    gross = seed.planned_notional_usdt * directional
    result = CryptoShadowHorizonOutcome(
        horizon_minutes=horizon_minutes,
        complete=True,
        close_time=horizon_end.isoformat(),
        close_price=closing_bar.close,
        directional_return_fraction=directional,
        gross_pnl_usdt=gross,
        modeled_net_pnl_usdt=gross - seed.estimated_round_trip_cost_usdt,
    )
    result.validate()
    return result


def _directional_return(seed: CryptoShadowSeed, price: Decimal) -> Decimal:
    if price <= 0:
        raise ValueError("shadow evaluation price must be positive")
    raw = price / seed.entry_price - Decimal("1")
    return raw if seed.side == "LONG" else -raw


def _fixed_config(
    strategy_config: CryptoPerpStrategyConfig | None,
) -> CryptoPerpStrategyConfig:
    default = CryptoPerpStrategyConfig()
    active = default if strategy_config is None else strategy_config
    active.validate()
    if active != default:
        raise ValueError("prospective shadow tracking requires the qualified fixed strategy")
    return active


def _validated_symbol_bars(
    bars: Sequence[BybitKlineBar],
    *,
    symbol: str,
) -> tuple[BybitKlineBar, ...]:
    ordered = tuple(sorted(bars, key=lambda item: item.start_time))
    if tuple(bars) != ordered:
        raise ValueError("shadow bars must be chronological")
    seen: set[datetime] = set()
    for bar in ordered:
        bar.validate()
        if bar.symbol != symbol:
            raise ValueError("shadow bars must belong to the source symbol")
        if bar.start_time in seen:
            raise ValueError("shadow bars cannot contain duplicate timestamps")
        seen.add(bar.start_time)
    return ordered


def _validate_safety_flags(
    *,
    prospective: bool,
    operator_review_required: bool,
    trade_actionable: bool,
    demo_activation_allowed: bool,
    live_activation_allowed: bool,
    bybit_live_order_routing_allowed: bool,
) -> None:
    if (
        not prospective
        or not operator_review_required
        or trade_actionable
        or demo_activation_allowed
        or live_activation_allowed
        or bybit_live_order_routing_allowed
    ):
        raise ValueError("prospective shadow evidence cannot activate trading")


def _validate_sha(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in _HEX for character in value):
        raise ValueError(f"{label} id must be SHA-256 hex")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("shadow timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _as_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    parsed = Decimal(str(value))
    if not parsed.is_finite():
        raise ValueError("shadow numeric value must be finite")
    return parsed


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
