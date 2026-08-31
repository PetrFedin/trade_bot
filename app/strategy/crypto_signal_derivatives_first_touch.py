from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, TypeVar

from app.marketdata.bybit_derivatives_history import BybitDerivativesHistory
from app.strategy.crypto_perp import CryptoPerpStrategyConfig

_ZERO = Decimal("0")
_ONE = Decimal("1")
_INTERVAL = timedelta(minutes=5)
_Z_95 = 1.959963984540054
_LONG_HEAVY = Decimal("0.55")
_SHORT_HEAVY = Decimal("0.45")
_FIRST_TOUCH_STATES = frozenset(
    {"TARGET_FIRST", "STOP_FIRST", "AMBIGUOUS_SAME_BAR", "NEITHER", "INCOMPLETE"}
)
TPoint = TypeVar("TPoint", bound="_TimestampPoint")


class _TimestampPoint(Protocol):
    timestamp_ms: int


@dataclass(frozen=True)
class CryptoSignalDerivativesFirstTouchPolicy:
    minimum_pattern_observations: int = 5
    sample_sufficient_observations: int = 30
    minimum_cross_symbol_count: int = 2
    minimum_distinct_days: int = 3
    open_interest_impulse_fraction: Decimal = Decimal("0.01")
    high_stress_feature_count: int = 3
    elevated_stress_feature_count: int = 1

    def validate(self) -> None:
        if not 1 <= self.minimum_pattern_observations <= self.sample_sufficient_observations:
            raise ValueError("derivatives first-touch minimum observations are invalid")
        if self.sample_sufficient_observations > 100_000:
            raise ValueError("derivatives first-touch sufficient observations are unreasonable")
        if not 1 <= self.minimum_cross_symbol_count <= 1000:
            raise ValueError("derivatives first-touch cross-symbol minimum is invalid")
        if not 1 <= self.minimum_distinct_days <= 3650:
            raise ValueError("derivatives first-touch distinct-day minimum is invalid")
        if (
            not self.open_interest_impulse_fraction.is_finite()
            or self.open_interest_impulse_fraction <= 0
        ):
            raise ValueError("derivatives first-touch OI impulse must be positive and finite")
        if not 1 <= self.elevated_stress_feature_count <= self.high_stress_feature_count:
            raise ValueError("derivatives first-touch stress thresholds are invalid")
        if self.high_stress_feature_count > 5:
            raise ValueError("derivatives first-touch high-stress threshold is too large")


@dataclass(frozen=True)
class CryptoSignalDerivativesFirstTouchRow:
    symbol: str
    side: str
    decision_time: str
    signal_available_at: str
    utc_day: str
    price_pattern: str
    enriched_pattern: str
    exact_cell_key: str
    clarity_band: str
    quality_ratio_to_entry_gate: Decimal
    expected_net_edge_usd: Decimal
    atr_fraction: Decimal
    one_bar_atr_multiple: Decimal
    maximum_favorable_r: Decimal | None
    maximum_adverse_r: Decimal | None
    first_touch_state: str
    first_touch_bar: str | None
    open_interest_regime: str
    crowding_regime: str
    prior_funding_regime: str
    stress_regime: str
    stress_score: int
    derivatives_context_complete: bool
    derivatives_missing_reasons: tuple[str, ...]
    open_interest_timestamp_ms: int | None
    open_interest_delta_fraction: Decimal | None
    account_ratio_timestamp_ms: int | None
    long_account_ratio: Decimal | None
    prior_funding_timestamp_ms: int | None
    prior_funding_rate: Decimal | None

    def validate(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("derivatives first-touch symbol is invalid")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("derivatives first-touch side is invalid")
        decision = _parse_time(self.decision_time)
        available = _parse_time(self.signal_available_at)
        if available != decision + _INTERVAL:
            raise ValueError("derivatives first-touch signal timing is inconsistent")
        if self.utc_day != available.date().isoformat():
            raise ValueError("derivatives first-touch UTC day is inconsistent")
        if self.first_touch_state not in _FIRST_TOUCH_STATES:
            raise ValueError("derivatives first-touch state is invalid")
        if self.first_touch_bar is not None:
            _parse_time(self.first_touch_bar)
        if not self.price_pattern or not self.enriched_pattern or not self.exact_cell_key:
            raise ValueError("derivatives first-touch pattern identities are required")
        values = (
            self.quality_ratio_to_entry_gate,
            self.expected_net_edge_usd,
            self.atr_fraction,
            self.one_bar_atr_multiple,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("derivatives first-touch signal metrics must be finite")
        for name, value in (
            ("maximum_favorable_r", self.maximum_favorable_r),
            ("maximum_adverse_r", self.maximum_adverse_r),
            ("open_interest_delta_fraction", self.open_interest_delta_fraction),
            ("long_account_ratio", self.long_account_ratio),
            ("prior_funding_rate", self.prior_funding_rate),
        ):
            if value is not None and not value.is_finite():
                raise ValueError(f"derivatives first-touch {name} must be finite")
        if self.maximum_favorable_r is not None and self.maximum_favorable_r < 0:
            raise ValueError("derivatives first-touch MFE cannot be negative")
        if self.maximum_adverse_r is not None and self.maximum_adverse_r < 0:
            raise ValueError("derivatives first-touch MAE cannot be negative")
        if not 0 <= self.stress_score <= 5:
            raise ValueError("derivatives first-touch stress score must be within [0, 5]")
        if self.derivatives_context_complete and self.stress_regime == "STRESS_UNKNOWN":
            raise ValueError("complete derivatives context cannot have unknown stress")
        if not self.derivatives_context_complete and self.stress_regime != "STRESS_UNKNOWN":
            raise ValueError("incomplete derivatives context must have unknown stress")
        decision_ms = int(decision.timestamp() * 1000)
        for name, timestamp in (
            ("open_interest", self.open_interest_timestamp_ms),
            ("account_ratio", self.account_ratio_timestamp_ms),
            ("prior_funding", self.prior_funding_timestamp_ms),
        ):
            if timestamp is not None and timestamp > decision_ms:
                raise ValueError(f"derivatives first-touch {name} contains lookahead")

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "symbol": self.symbol,
            "side": self.side,
            "decision_time": self.decision_time,
            "signal_available_at": self.signal_available_at,
            "utc_day": self.utc_day,
            "price_pattern": self.price_pattern,
            "enriched_pattern": self.enriched_pattern,
            "exact_cell_key": self.exact_cell_key,
            "clarity_band": self.clarity_band,
            "quality_ratio_to_entry_gate": float(self.quality_ratio_to_entry_gate),
            "expected_net_edge_usd": float(self.expected_net_edge_usd),
            "atr_fraction": float(self.atr_fraction),
            "one_bar_atr_multiple": float(self.one_bar_atr_multiple),
            "maximum_favorable_r": _optional_float(self.maximum_favorable_r),
            "maximum_adverse_r": _optional_float(self.maximum_adverse_r),
            "first_touch_state": self.first_touch_state,
            "first_touch_bar": self.first_touch_bar,
            "open_interest_regime": self.open_interest_regime,
            "crowding_regime": self.crowding_regime,
            "prior_funding_regime": self.prior_funding_regime,
            "stress_regime": self.stress_regime,
            "stress_score": self.stress_score,
            "derivatives_context_complete": self.derivatives_context_complete,
            "derivatives_missing_reasons": list(self.derivatives_missing_reasons),
            "open_interest_timestamp_ms": self.open_interest_timestamp_ms,
            "open_interest_delta_fraction": _optional_float(
                self.open_interest_delta_fraction
            ),
            "account_ratio_timestamp_ms": self.account_ratio_timestamp_ms,
            "long_account_ratio": _optional_float(self.long_account_ratio),
            "prior_funding_timestamp_ms": self.prior_funding_timestamp_ms,
            "prior_funding_rate": _optional_float(self.prior_funding_rate),
        }


def audit_crypto_signal_derivatives_first_touch(
    first_touch_report: Mapping[str, Any],
    histories: Mapping[str, BybitDerivativesHistory],
    *,
    strategy_config: CryptoPerpStrategyConfig | None = None,
    policy: CryptoSignalDerivativesFirstTouchPolicy | None = None,
) -> dict[str, Any]:
    """Join point-in-time derivatives evidence to every plan-eligible first-touch signal."""

    _validate_first_touch_boundary(first_touch_report)
    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    active = CryptoSignalDerivativesFirstTouchPolicy() if policy is None else policy
    active.validate()

    raw_rows = first_touch_report.get("outcome_rows")
    if not isinstance(raw_rows, list):
        raise ValueError("derivatives first-touch requires raw first-touch outcome rows")
    symbols = {_required_text(row, "symbol") for row in raw_rows if isinstance(row, Mapping)}
    if set(histories) != symbols:
        raise ValueError("derivatives first-touch history symbols do not match signal universe")
    for symbol, history in histories.items():
        history.validate()
        if history.symbol != symbol:
            raise ValueError("derivatives first-touch history key/symbol mismatch")

    enriched = tuple(
        _enrich_row(row, histories=histories, config=config, policy=active)
        for row in raw_rows
        if isinstance(row, Mapping)
    )
    if len(enriched) != len(raw_rows):
        raise ValueError("derivatives first-touch raw outcome row is not an object")
    ordered = tuple(
        sorted(enriched, key=lambda item: (_parse_time(item.signal_available_at), item.symbol))
    )
    episodes = _deduplicate_enriched_episodes(ordered)
    complete_episodes = tuple(item for item in episodes if item.derivatives_context_complete)

    transferable_rows = _pattern_rows(
        complete_episodes,
        key=lambda item: item.enriched_pattern,
        policy=active,
        require_cross_symbol=True,
    )
    exact_cell_rows = _pattern_rows(
        complete_episodes,
        key=lambda item: item.exact_cell_key,
        policy=active,
        require_cross_symbol=False,
    )
    qualified_transferable = [row for row in transferable_rows if row["qualified"]]
    qualified_exact = [row for row in exact_cell_rows if row["qualified"]]
    perfect_transferable = [
        row for row in qualified_transferable if row["observed_perfect_target_first"]
    ]
    perfect_exact = [
        row for row in qualified_exact if row["observed_perfect_target_first"]
    ]
    oos_ready_exact = [row for row in perfect_exact if row["sample_sufficient"]]

    return {
        "audit": "BYBIT_CRYPTO_DERIVATIVES_FIRST_TOUCH_V1",
        "source_first_touch_audit": first_touch_report["audit"],
        "raw_signal_count": len(ordered),
        "independent_episode_count": len(episodes),
        "complete_derivatives_episode_count": len(complete_episodes),
        "incomplete_derivatives_episode_count": len(episodes) - len(complete_episodes),
        "aggregate": _summary(ordered),
        "episode_aggregate": _summary(episodes),
        "complete_episode_aggregate": _summary(complete_episodes),
        "by_symbol": _group_summary(complete_episodes, lambda item: item.symbol),
        "by_side": _group_summary(complete_episodes, lambda item: item.side),
        "by_clarity_band": _group_summary(
            complete_episodes, lambda item: item.clarity_band
        ),
        "by_open_interest_regime": _group_summary(
            complete_episodes, lambda item: item.open_interest_regime
        ),
        "by_crowding_regime": _group_summary(
            complete_episodes, lambda item: item.crowding_regime
        ),
        "by_prior_funding_regime": _group_summary(
            complete_episodes, lambda item: item.prior_funding_regime
        ),
        "by_stress_regime": _group_summary(
            complete_episodes, lambda item: item.stress_regime
        ),
        "transferable_pattern_rows": transferable_rows,
        "qualified_transferable_pattern_rows": qualified_transferable,
        "retrospective_perfect_transferable_patterns": perfect_transferable,
        "perfect_transferable_pattern_count": len(perfect_transferable),
        "exact_cell_rows": exact_cell_rows,
        "qualified_exact_cell_rows": qualified_exact,
        "retrospective_perfect_exact_cells": perfect_exact,
        "perfect_exact_cell_count": len(perfect_exact),
        "oos_ready_retrospective_exact_cells": oos_ready_exact,
        "oos_ready_retrospective_exact_cell_count": len(oos_ready_exact),
        "raw_rows": [item.to_payload() for item in ordered],
        "episode_rows": [item.to_payload() for item in episodes],
        "minimum_pattern_observations": active.minimum_pattern_observations,
        "sample_sufficient_observations": active.sample_sufficient_observations,
        "minimum_cross_symbol_count": active.minimum_cross_symbol_count,
        "minimum_distinct_days": active.minimum_distinct_days,
        "open_interest_impulse_fraction": str(active.open_interest_impulse_fraction),
        "success_definition": (
            "TARGET_FIRST within the frozen first-touch horizon; STOP_FIRST, NEITHER, "
            "AMBIGUOUS_SAME_BAR and INCOMPLETE are not successes"
        ),
        "transferable_pattern_definition": (
            "price-pattern|OI-regime|crowding|prior-funding|stress; symbol excluded so "
            "cross-token transfer can be tested"
        ),
        "exact_cell_definition": (
            "symbol|price-pattern|OI-regime|crowding|prior-funding|stress"
        ),
        "derivatives_timing_contract": (
            "OI, account ratio and prior funding use only observations timestamped at or before "
            "the original signal decision_time; future first-touch outcome is joined afterward"
        ),
        "episode_dedup_contract": (
            "retain earliest signal in each uninterrupted 5m run sharing exact "
            "symbol|side|enriched-pattern; a gap over 5m starts a new episode"
        ),
        "candidate_contract": (
            "retrospective perfect candidates require complete derivatives context, minimum "
            "episode/day support and, for transferable patterns, cross-symbol support; exact "
            "cells need N>=sample_sufficient_observations before being marked OOS-ready"
        ),
        "stress_policy": {
            "feature_count": 5,
            "high_stress_feature_count": active.high_stress_feature_count,
            "elevated_stress_feature_count": active.elevated_stress_feature_count,
            "open_interest_impulse_fraction": str(active.open_interest_impulse_fraction),
            "price_shock_atr_threshold": str(
                config.maximum_one_bar_atr_multiple / Decimal("2")
            ),
        },
        "thresholds_fitted_to_outcomes": False,
        "retrospective_only": True,
        "counterfactual_portfolio_pnl_claim_allowed": False,
        "parameter_retuning_performed": False,
        "ranking_weights_changed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "trade_actionable": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _enrich_row(
    raw: Mapping[str, Any],
    *,
    histories: Mapping[str, BybitDerivativesHistory],
    config: CryptoPerpStrategyConfig,
    policy: CryptoSignalDerivativesFirstTouchPolicy,
) -> CryptoSignalDerivativesFirstTouchRow:
    symbol = _required_text(raw, "symbol")
    side = _required_text(raw, "side")
    decision_time = _required_text(raw, "decision_time")
    signal_available_at = _required_text(raw, "signal_available_at")
    decision = _parse_time(decision_time)
    history = histories[symbol]
    decision_ms = int(decision.timestamp() * 1000)
    if not history.start_ms <= decision_ms <= history.end_ms:
        raise ValueError("derivatives first-touch decision falls outside history range")

    current_oi = _latest_at_or_before(history.open_interest, decision_ms)
    previous_oi = (
        None
        if current_oi is None
        else _latest_before(history.open_interest, current_oi.timestamp_ms)
    )
    ratio = _latest_at_or_before(history.account_ratio, decision_ms)
    prior_funding = _latest_at_or_before(history.funding, decision_ms)
    missing: list[str] = []
    if current_oi is None:
        missing.append("OPEN_INTEREST_AT_DECISION_MISSING")
    if previous_oi is None:
        missing.append("OPEN_INTEREST_PREVIOUS_POINT_MISSING")
    if ratio is None:
        missing.append("ACCOUNT_RATIO_AT_DECISION_MISSING")
    if prior_funding is None:
        missing.append("PRIOR_FUNDING_RATE_MISSING")

    oi_delta_fraction: Decimal | None = None
    if current_oi is not None and previous_oi is not None:
        if previous_oi.open_interest > 0:
            oi_delta_fraction = (
                current_oi.open_interest - previous_oi.open_interest
            ) / previous_oi.open_interest
        else:
            missing.append("PREVIOUS_OPEN_INTEREST_ZERO")
    oi_regime = _open_interest_regime(oi_delta_fraction)
    long_ratio = None if ratio is None else ratio.buy_ratio
    crowding = _crowding_regime(long_ratio)
    funding_rate = None if prior_funding is None else prior_funding.funding_rate
    funding_regime = _funding_regime(funding_rate)

    price_pattern = _required_text(raw, "pattern")
    atr_fraction = _required_decimal(raw, "atr_fraction")
    one_bar_atr_multiple = _required_decimal(raw, "one_bar_atr_multiple")
    stress_regime, stress_score, stress_reasons = _stress_context(
        price_pattern=price_pattern,
        one_bar_atr_multiple=one_bar_atr_multiple,
        oi_delta_fraction=oi_delta_fraction,
        crowding_regime=crowding,
        prior_funding_regime=funding_regime,
        context_complete=not missing,
        missing_reasons=missing,
        config=config,
        policy=policy,
    )
    enriched_pattern = "|".join(
        (
            price_pattern,
            oi_regime,
            crowding,
            funding_regime,
            stress_regime,
        )
    )
    row = CryptoSignalDerivativesFirstTouchRow(
        symbol=symbol,
        side=side,
        decision_time=decision_time,
        signal_available_at=signal_available_at,
        utc_day=_parse_time(signal_available_at).date().isoformat(),
        price_pattern=price_pattern,
        enriched_pattern=enriched_pattern,
        exact_cell_key=f"{symbol}|{enriched_pattern}",
        clarity_band=_required_text(raw, "clarity_band"),
        quality_ratio_to_entry_gate=_required_decimal(
            raw, "quality_ratio_to_entry_gate"
        ),
        expected_net_edge_usd=_required_decimal(raw, "expected_net_edge_usd"),
        atr_fraction=atr_fraction,
        one_bar_atr_multiple=one_bar_atr_multiple,
        maximum_favorable_r=_optional_decimal(raw.get("maximum_favorable_r")),
        maximum_adverse_r=_optional_decimal(raw.get("maximum_adverse_r")),
        first_touch_state=_required_text(raw, "first_touch_state"),
        first_touch_bar=_optional_text(raw.get("first_touch_bar")),
        open_interest_regime=oi_regime,
        crowding_regime=crowding,
        prior_funding_regime=funding_regime,
        stress_regime=stress_regime,
        stress_score=stress_score,
        derivatives_context_complete=not missing,
        derivatives_missing_reasons=tuple(sorted(set(missing) | set(stress_reasons))),
        open_interest_timestamp_ms=(
            None if current_oi is None else current_oi.timestamp_ms
        ),
        open_interest_delta_fraction=oi_delta_fraction,
        account_ratio_timestamp_ms=None if ratio is None else ratio.timestamp_ms,
        long_account_ratio=long_ratio,
        prior_funding_timestamp_ms=(
            None if prior_funding is None else prior_funding.timestamp_ms
        ),
        prior_funding_rate=funding_rate,
    )
    row.validate()
    return row


def _stress_context(
    *,
    price_pattern: str,
    one_bar_atr_multiple: Decimal,
    oi_delta_fraction: Decimal | None,
    crowding_regime: str,
    prior_funding_regime: str,
    context_complete: bool,
    missing_reasons: Sequence[str],
    config: CryptoPerpStrategyConfig,
    policy: CryptoSignalDerivativesFirstTouchPolicy,
) -> tuple[str, int, tuple[str, ...]]:
    parts = price_pattern.split("|")
    if len(parts) != 5:
        raise ValueError("derivatives first-touch price pattern shape is invalid")
    volatility_regime = parts[2]
    score = 0
    reasons: list[str] = []
    if volatility_regime == "VOL_HIGH_NORMAL":
        score += 1
        reasons.append("HIGH_NORMAL_ATR_REGIME")
    if one_bar_atr_multiple >= config.maximum_one_bar_atr_multiple / Decimal("2"):
        score += 1
        reasons.append("ONE_BAR_MOVE_AT_LEAST_HALF_STRATEGY_LIMIT")
    if (
        oi_delta_fraction is not None
        and abs(oi_delta_fraction) >= policy.open_interest_impulse_fraction
    ):
        score += 1
        reasons.append("OPEN_INTEREST_IMPULSE")
    if crowding_regime in {"LONG_HEAVY", "SHORT_HEAVY"}:
        score += 1
        reasons.append("POSITION_HOLDER_CROWDING")
    funding_pressure = (
        crowding_regime == "LONG_HEAVY" and prior_funding_regime == "FUNDING_POSITIVE"
    ) or (
        crowding_regime == "SHORT_HEAVY" and prior_funding_regime == "FUNDING_NEGATIVE"
    )
    if funding_pressure:
        score += 1
        reasons.append("CROWDED_SIDE_PAYS_PRIOR_FUNDING")
    if not context_complete:
        return "STRESS_UNKNOWN", score, tuple(sorted(set(reasons) | set(missing_reasons)))
    if score >= policy.high_stress_feature_count:
        regime = "STRESS_HIGH"
    elif score >= policy.elevated_stress_feature_count:
        regime = "STRESS_ELEVATED"
    else:
        regime = "STRESS_CALM"
    return regime, score, tuple(reasons)


def _deduplicate_enriched_episodes(
    rows: Sequence[CryptoSignalDerivativesFirstTouchRow],
) -> tuple[CryptoSignalDerivativesFirstTouchRow, ...]:
    grouped: dict[tuple[str, str, str], list[CryptoSignalDerivativesFirstTouchRow]] = (
        defaultdict(list)
    )
    for item in rows:
        grouped[(item.symbol, item.side, item.enriched_pattern)].append(item)
    retained: list[CryptoSignalDerivativesFirstTouchRow] = []
    for key in sorted(grouped):
        ordered = sorted(grouped[key], key=lambda item: _parse_time(item.signal_available_at))
        previous: datetime | None = None
        for item in ordered:
            current = _parse_time(item.signal_available_at)
            if previous is None or current - previous > _INTERVAL:
                retained.append(item)
            previous = current
    return tuple(
        sorted(retained, key=lambda item: (_parse_time(item.signal_available_at), item.symbol))
    )


def _pattern_rows(
    rows: Sequence[CryptoSignalDerivativesFirstTouchRow],
    *,
    key: Any,
    policy: CryptoSignalDerivativesFirstTouchPolicy,
    require_cross_symbol: bool,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[CryptoSignalDerivativesFirstTouchRow]] = defaultdict(list)
    for item in rows:
        grouped[str(key(item))].append(item)
    result = [
        _pattern_summary(
            pattern,
            members,
            policy=policy,
            require_cross_symbol=require_cross_symbol,
        )
        for pattern, members in sorted(grouped.items())
    ]
    return sorted(result, key=_pattern_sort_key, reverse=True)


def _pattern_summary(
    pattern: str,
    rows: Sequence[CryptoSignalDerivativesFirstTouchRow],
    *,
    policy: CryptoSignalDerivativesFirstTouchPolicy,
    require_cross_symbol: bool,
) -> dict[str, Any]:
    summary = _summary(rows)
    symbols = sorted({item.symbol for item in rows})
    days = sorted({item.utc_day for item in rows})
    minimum_support = len(rows) >= policy.minimum_pattern_observations
    cross_symbol_support = (
        len(symbols) >= policy.minimum_cross_symbol_count if require_cross_symbol else True
    )
    day_support = len(days) >= policy.minimum_distinct_days
    qualified = minimum_support and cross_symbol_support and day_support
    perfect = bool(rows) and summary["target_first_count"] == len(rows)
    if perfect and qualified:
        tier = (
            "RETROSPECTIVE_PERFECT_SAMPLE_SUFFICIENT"
            if len(rows) >= policy.sample_sufficient_observations
            else "RETROSPECTIVE_PERFECT_SMALL_SAMPLE"
        )
    elif qualified:
        tier = "RETROSPECTIVE_MIXED"
    else:
        tier = "INSUFFICIENT_SUPPORT"
    return {
        "pattern": pattern,
        "symbol_count": len(symbols),
        "symbols": symbols,
        "distinct_day_count": len(days),
        "utc_days": days,
        "minimum_support_met": minimum_support,
        "cross_symbol_support_met": cross_symbol_support,
        "distinct_day_support_met": day_support,
        "qualified": qualified,
        "sample_sufficient": len(rows) >= policy.sample_sufficient_observations,
        "observed_perfect_target_first": perfect,
        "candidate_tier": tier,
        "chronological_halves": _chronological_halves(rows),
        **summary,
    }


def _summary(rows: Sequence[CryptoSignalDerivativesFirstTouchRow]) -> dict[str, Any]:
    counts = Counter(item.first_touch_state for item in rows)
    target = counts["TARGET_FIRST"]
    mfe = [item.maximum_favorable_r for item in rows if item.maximum_favorable_r is not None]
    mae = [item.maximum_adverse_r for item in rows if item.maximum_adverse_r is not None]
    complete_context = sum(item.derivatives_context_complete for item in rows)
    return {
        "observation_count": len(rows),
        "target_first_count": target,
        "stop_first_count": counts["STOP_FIRST"],
        "ambiguous_same_bar_count": counts["AMBIGUOUS_SAME_BAR"],
        "neither_count": counts["NEITHER"],
        "incomplete_count": counts["INCOMPLETE"],
        "target_first_rate": None if not rows else target / len(rows),
        "target_first_wilson_lower_95": _wilson_lower(target, len(rows)),
        "complete_derivatives_context_count": complete_context,
        "complete_derivatives_context_rate": (
            None if not rows else complete_context / len(rows)
        ),
        "median_quality_ratio": _median(rows, "quality_ratio_to_entry_gate"),
        "median_expected_net_edge_usd": _median(rows, "expected_net_edge_usd"),
        "median_mfe_r": None if not mfe else float(statistics.median(mfe)),
        "median_mae_r": None if not mae else float(statistics.median(mae)),
        "first_touch_state_counts": dict(sorted(counts.items())),
    }


def _group_summary(
    rows: Sequence[CryptoSignalDerivativesFirstTouchRow],
    key: Any,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[CryptoSignalDerivativesFirstTouchRow]] = defaultdict(list)
    for item in rows:
        grouped[str(key(item))].append(item)
    return {
        group: _summary(members)
        for group, members in sorted(grouped.items())
    }


def _chronological_halves(
    rows: Sequence[CryptoSignalDerivativesFirstTouchRow],
) -> dict[str, Any]:
    ordered = tuple(sorted(rows, key=lambda item: _parse_time(item.signal_available_at)))
    if len(ordered) < 2:
        return {"early": _summary(ordered), "late": _summary(())}
    midpoint = len(ordered) // 2
    return {
        "early": _summary(ordered[:midpoint]),
        "late": _summary(ordered[midpoint:]),
    }


def _pattern_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        bool(row["observed_perfect_target_first"]),
        bool(row["sample_sufficient"]),
        bool(row["qualified"]),
        float(row["target_first_rate"] or 0.0),
        int(row["observation_count"]),
        float(row["target_first_wilson_lower_95"] or 0.0),
        str(row["pattern"]),
    )


def _open_interest_regime(delta_fraction: Decimal | None) -> str:
    if delta_fraction is None:
        return "OI_UNKNOWN"
    if delta_fraction > 0:
        return "OI_RISING"
    if delta_fraction < 0:
        return "OI_FALLING"
    return "OI_FLAT"


def _crowding_regime(long_ratio: Decimal | None) -> str:
    if long_ratio is None:
        return "CROWDING_UNKNOWN"
    if long_ratio >= _LONG_HEAVY:
        return "LONG_HEAVY"
    if long_ratio <= _SHORT_HEAVY:
        return "SHORT_HEAVY"
    return "BALANCED"


def _funding_regime(rate: Decimal | None) -> str:
    if rate is None:
        return "FUNDING_UNKNOWN"
    if rate > 0:
        return "FUNDING_POSITIVE"
    if rate < 0:
        return "FUNDING_NEGATIVE"
    return "FUNDING_ZERO"


def _latest_at_or_before(points: Sequence[TPoint], timestamp_ms: int) -> TPoint | None:
    timestamps = [point.timestamp_ms for point in points]
    index = bisect_right(timestamps, timestamp_ms) - 1
    return None if index < 0 else points[index]


def _latest_before(points: Sequence[TPoint], timestamp_ms: int) -> TPoint | None:
    timestamps = [point.timestamp_ms for point in points]
    index = bisect_right(timestamps, timestamp_ms - 1) - 1
    return None if index < 0 else points[index]


def _validate_first_touch_boundary(report: Mapping[str, Any]) -> None:
    if report.get("audit") != "BYBIT_CRYPTO_PLAN_ELIGIBLE_FIRST_TOUCH_V2":
        raise ValueError("derivatives first-touch requires canonical first-touch V2 evidence")
    for field in (
        "retrospective_only",
        "counterfactual_portfolio_pnl_claim_allowed",
        "strategy_selection_allowed",
        "strategy_promotion_allowed",
        "trade_actionable",
        "demo_activation_allowed",
        "live_activation_allowed",
        "bybit_live_order_routing_allowed",
        "predictive_guarantee_allowed",
    ):
        expected = field == "retrospective_only"
        if report.get(field) is not expected:
            raise ValueError(f"derivatives first-touch requires explicit {field}={expected}")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("derivatives first-touch timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"derivatives first-touch {field} must be non-empty text")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("derivatives first-touch optional text is invalid")
    return value


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = _as_decimal(row.get(field), field=field)
    if value is None:
        raise ValueError(f"derivatives first-touch {field} is required")
    return value


def _optional_decimal(value: object) -> Decimal | None:
    return _as_decimal(value, field="optional_decimal")


def _as_decimal(value: object, *, field: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"derivatives first-touch {field} cannot be boolean")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"derivatives first-touch {field} must be decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"derivatives first-touch {field} must be finite")
    return parsed


def _median(
    rows: Sequence[CryptoSignalDerivativesFirstTouchRow],
    field: str,
) -> float | None:
    if not rows:
        return None
    values = [getattr(item, field) for item in rows]
    return float(statistics.median(values))


def _optional_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _wilson_lower(successes: int, total: int) -> float | None:
    if total <= 0:
        return None
    p = successes / total
    z2 = _Z_95 * _Z_95
    denominator = 1.0 + z2 / total
    center = p + z2 / (2.0 * total)
    margin = _Z_95 * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return max(0.0, (center - margin) / denominator)


__all__ = [
    "CryptoSignalDerivativesFirstTouchPolicy",
    "CryptoSignalDerivativesFirstTouchRow",
    "audit_crypto_signal_derivatives_first_touch",
]
