from __future__ import annotations

import hashlib
import json
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.marketdata.bybit_derivatives_history import BybitDerivativesHistory
from app.marketdata.bybit_opportunity_registry import (
    BybitOpportunityCandidate,
    BybitOpportunitySnapshot,
)
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSignal,
    build_trade_plan,
    evaluate_crypto_signal,
)
from app.strategy.crypto_strategy_evidence_matrix import (
    CryptoStrategyEvidencePolicy,
    classify_crypto_signal_market_regime,
    classify_crypto_stress_regime,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_LONG_HEAVY = Decimal("0.55")
_SHORT_HEAVY = Decimal("0.45")
_HEX = frozenset("0123456789abcdef")

_STATE_PRIORITY = {
    "QUALIFIED_POSITIVE_EVIDENCE": 0,
    "QUALIFIED_MIXED_EVIDENCE": 1,
    "NO_SAMPLE_SUFFICIENT_EXACT_CELL": 2,
    "DERIVATIVES_CONTEXT_INCOMPLETE": 3,
    "TRADE_PLAN_REJECTED": 4,
    "NO_FIXED_STRATEGY_SIGNAL": 5,
    "MARKET_HISTORY_UNAVAILABLE": 6,
}


@dataclass(frozen=True)
class CryptoCurrentDerivativesContext:
    symbol: str
    decision_time: str
    open_interest_timestamp_ms: int | None
    open_interest: Decimal | None
    previous_open_interest: Decimal | None
    open_interest_delta: Decimal | None
    open_interest_delta_fraction: Decimal | None
    account_ratio_timestamp_ms: int | None
    long_account_ratio: Decimal | None
    short_account_ratio: Decimal | None
    prior_funding_timestamp_ms: int | None
    prior_funding_rate: Decimal | None
    decision_context_complete: bool
    missing_reasons: tuple[str, ...]

    @property
    def open_interest_regime(self) -> str:
        if self.open_interest_delta is None:
            return "OI_UNKNOWN"
        if self.open_interest_delta > 0:
            return "OI_RISING"
        if self.open_interest_delta < 0:
            return "OI_FALLING"
        return "OI_FLAT"

    @property
    def crowding_regime(self) -> str:
        if self.long_account_ratio is None:
            return "CROWDING_UNKNOWN"
        if self.long_account_ratio >= _LONG_HEAVY:
            return "LONG_HEAVY"
        if self.long_account_ratio <= _SHORT_HEAVY:
            return "SHORT_HEAVY"
        return "BALANCED"

    @property
    def prior_funding_regime(self) -> str:
        if self.prior_funding_rate is None:
            return "FUNDING_UNKNOWN"
        if self.prior_funding_rate > 0:
            return "FUNDING_POSITIVE"
        if self.prior_funding_rate < 0:
            return "FUNDING_NEGATIVE"
        return "FUNDING_ZERO"

    def validate(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("live crypto derivatives symbol is invalid")
        _parse_time(self.decision_time)
        for name, value in (
            ("open_interest", self.open_interest),
            ("previous_open_interest", self.previous_open_interest),
            ("open_interest_delta", self.open_interest_delta),
            ("open_interest_delta_fraction", self.open_interest_delta_fraction),
            ("long_account_ratio", self.long_account_ratio),
            ("short_account_ratio", self.short_account_ratio),
            ("prior_funding_rate", self.prior_funding_rate),
        ):
            if value is not None and not value.is_finite():
                raise ValueError(f"live crypto derivatives {name} must be finite")
        for name, value in (
            ("open_interest_timestamp_ms", self.open_interest_timestamp_ms),
            ("account_ratio_timestamp_ms", self.account_ratio_timestamp_ms),
            ("prior_funding_timestamp_ms", self.prior_funding_timestamp_ms),
        ):
            if value is not None and value < 0:
                raise ValueError(f"live crypto derivatives {name} cannot be negative")
        if self.open_interest is not None and self.open_interest <= 0:
            raise ValueError("live crypto derivatives open interest must be positive")
        if self.previous_open_interest is not None and self.previous_open_interest <= 0:
            raise ValueError("live crypto derivatives previous OI must be positive")
        if self.open_interest_delta is not None:
            if self.open_interest is None or self.previous_open_interest is None:
                raise ValueError("live crypto derivatives OI delta requires both OI points")
            if self.open_interest_delta != self.open_interest - self.previous_open_interest:
                raise ValueError("live crypto derivatives OI delta is inconsistent")
        if self.open_interest_delta_fraction is not None:
            if self.open_interest_delta is None or self.previous_open_interest is None:
                raise ValueError("live crypto derivatives OI fraction requires OI delta")
            expected = self.open_interest_delta / self.previous_open_interest
            if self.open_interest_delta_fraction != expected:
                raise ValueError("live crypto derivatives OI fraction is inconsistent")
        if self.long_account_ratio is not None:
            if not _ZERO <= self.long_account_ratio <= _ONE:
                raise ValueError("live crypto derivatives long ratio must be within [0, 1]")
        if self.short_account_ratio is not None:
            if not _ZERO <= self.short_account_ratio <= _ONE:
                raise ValueError("live crypto derivatives short ratio must be within [0, 1]")
        if self.decision_context_complete and self.missing_reasons:
            raise ValueError("complete live derivatives context cannot carry missing reasons")
        if not self.decision_context_complete and not self.missing_reasons:
            raise ValueError("incomplete live derivatives context requires missing reasons")


@dataclass(frozen=True)
class CryptoLiveOpportunity:
    evidence_rank: int
    market_rank: int
    symbol: str
    market_universe_score: Decimal
    qualification_state: str
    qualification_reasons: tuple[str, ...]
    signal_side: str | None
    decision_time: str | None
    signal_quality_score: Decimal | None
    current_market_regime: str | None
    current_open_interest_regime: str | None
    current_crowding_regime: str | None
    current_prior_funding_regime: str | None
    current_stress_regime: str | None
    current_stress_score: int | None
    expected_net_edge_usd: Decimal | None
    planned_notional_usdt: Decimal | None
    risk_budget_usdt: Decimal | None
    estimated_round_trip_cost_usdt: Decimal | None
    evidence_cell_key: str | None
    evidence_trade_count: int | None
    evidence_sample_sufficient: bool
    evidence_profit_factor: Decimal | None
    evidence_win_rate: Decimal | None
    evidence_total_net_pnl_usdt: Decimal | None
    evidence_average_net_pnl_usdt: Decimal | None
    evidence_average_mfe_r: Decimal | None
    evidence_average_mae_r: Decimal | None
    evidence_drawdown_usdt: Decimal | None
    positive_historical_evidence: bool
    operator_review_required: bool = True
    trade_actionable: bool = False
    strategy_promotion_allowed: bool = False
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False
    bybit_live_order_routing_allowed: bool = False

    def validate(self) -> None:
        if not 1 <= self.evidence_rank <= 50:
            raise ValueError("live crypto opportunity evidence rank must be within [1, 50]")
        if not 1 <= self.market_rank <= 50:
            raise ValueError("live crypto opportunity market rank must be within [1, 50]")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("live crypto opportunity symbol is invalid")
        if self.qualification_state not in _STATE_PRIORITY:
            raise ValueError("live crypto opportunity qualification state is invalid")
        if (
            not self.market_universe_score.is_finite()
            or not _ZERO <= self.market_universe_score <= _ONE
        ):
            raise ValueError("live crypto opportunity market score must be within [0, 1]")
        if self.signal_side is not None and self.signal_side not in {"LONG", "SHORT"}:
            raise ValueError("live crypto opportunity signal side is invalid")
        if (self.signal_side is None) != (self.decision_time is None):
            raise ValueError("live crypto opportunity signal identity is inconsistent")
        if self.decision_time is not None:
            _parse_time(self.decision_time)
        for name, value in (
            ("signal_quality_score", self.signal_quality_score),
            ("expected_net_edge_usd", self.expected_net_edge_usd),
            ("planned_notional_usdt", self.planned_notional_usdt),
            ("risk_budget_usdt", self.risk_budget_usdt),
            ("estimated_round_trip_cost_usdt", self.estimated_round_trip_cost_usdt),
            ("evidence_profit_factor", self.evidence_profit_factor),
            ("evidence_win_rate", self.evidence_win_rate),
            ("evidence_total_net_pnl_usdt", self.evidence_total_net_pnl_usdt),
            ("evidence_average_net_pnl_usdt", self.evidence_average_net_pnl_usdt),
            ("evidence_average_mfe_r", self.evidence_average_mfe_r),
            ("evidence_average_mae_r", self.evidence_average_mae_r),
            ("evidence_drawdown_usdt", self.evidence_drawdown_usdt),
        ):
            if value is not None and not value.is_finite():
                raise ValueError(f"live crypto opportunity {name} must be finite")
        if self.evidence_trade_count is not None and self.evidence_trade_count < 0:
            raise ValueError("live crypto opportunity evidence trade count cannot be negative")
        if self.current_stress_score is not None:
            if not 0 <= self.current_stress_score <= 5:
                raise ValueError("live crypto opportunity stress score must be within [0, 5]")
        if self.evidence_sample_sufficient and self.evidence_cell_key is None:
            raise ValueError("sample-sufficient live opportunity requires evidence cell")
        if self.positive_historical_evidence and not self.evidence_sample_sufficient:
            raise ValueError("positive live historical evidence requires sufficient sample")
        if (
            not self.operator_review_required
            or self.trade_actionable
            or self.strategy_promotion_allowed
            or self.demo_activation_allowed
            or self.live_activation_allowed
            or self.bybit_live_order_routing_allowed
        ):
            raise ValueError("live crypto opportunity cannot activate trading")

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "evidence_rank": self.evidence_rank,
            "market_rank": self.market_rank,
            "symbol": self.symbol,
            "market_universe_score": str(self.market_universe_score),
            "qualification_state": self.qualification_state,
            "qualification_reasons": list(self.qualification_reasons),
            "signal_side": self.signal_side,
            "decision_time": self.decision_time,
            "signal_quality_score": _decimal_text(self.signal_quality_score),
            "current_market_regime": self.current_market_regime,
            "current_open_interest_regime": self.current_open_interest_regime,
            "current_crowding_regime": self.current_crowding_regime,
            "current_prior_funding_regime": self.current_prior_funding_regime,
            "current_stress_regime": self.current_stress_regime,
            "current_stress_score": self.current_stress_score,
            "expected_net_edge_usd": _decimal_text(self.expected_net_edge_usd),
            "planned_notional_usdt": _decimal_text(self.planned_notional_usdt),
            "risk_budget_usdt": _decimal_text(self.risk_budget_usdt),
            "estimated_round_trip_cost_usdt": _decimal_text(
                self.estimated_round_trip_cost_usdt
            ),
            "evidence_cell_key": self.evidence_cell_key,
            "evidence_trade_count": self.evidence_trade_count,
            "evidence_sample_sufficient": self.evidence_sample_sufficient,
            "evidence_profit_factor": _decimal_text(self.evidence_profit_factor),
            "evidence_win_rate": _decimal_text(self.evidence_win_rate),
            "evidence_total_net_pnl_usdt": _decimal_text(
                self.evidence_total_net_pnl_usdt
            ),
            "evidence_average_net_pnl_usdt": _decimal_text(
                self.evidence_average_net_pnl_usdt
            ),
            "evidence_average_mfe_r": _decimal_text(self.evidence_average_mfe_r),
            "evidence_average_mae_r": _decimal_text(self.evidence_average_mae_r),
            "evidence_drawdown_usdt": _decimal_text(self.evidence_drawdown_usdt),
            "positive_historical_evidence": self.positive_historical_evidence,
            "operator_review_required": self.operator_review_required,
            "trade_actionable": self.trade_actionable,
            "strategy_promotion_allowed": self.strategy_promotion_allowed,
            "demo_activation_allowed": self.demo_activation_allowed,
            "live_activation_allowed": self.live_activation_allowed,
            "bybit_live_order_routing_allowed": self.bybit_live_order_routing_allowed,
        }


@dataclass(frozen=True)
class CryptoLiveOpportunitySnapshot:
    observed_at_ms: int
    market_snapshot_id: str
    evidence_snapshot_id: str
    equity_usdt: Decimal
    equity_source: str
    opportunities: tuple[CryptoLiveOpportunity, ...]
    qualified_positive_count: int
    qualified_mixed_count: int
    operator_review_required: bool = True
    trade_actionable: bool = False
    strategy_parameters_changed: bool = False
    strategy_promotion_allowed: bool = False
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False
    bybit_live_order_routing_allowed: bool = False

    def validate(self) -> None:
        if self.observed_at_ms < 0:
            raise ValueError("live opportunity snapshot timestamp cannot be negative")
        for name, value in (
            ("market_snapshot_id", self.market_snapshot_id),
            ("evidence_snapshot_id", self.evidence_snapshot_id),
        ):
            if len(value) != 64 or any(char not in _HEX for char in value):
                raise ValueError(f"live opportunity snapshot {name} must be SHA-256 hex")
        if not self.equity_usdt.is_finite() or self.equity_usdt <= 0:
            raise ValueError("live opportunity snapshot equity must be positive and finite")
        if not self.equity_source:
            raise ValueError("live opportunity snapshot equity source is required")
        if len(self.opportunities) > 50:
            raise ValueError("live opportunity snapshot cannot exceed 50 candidates")
        for expected_rank, opportunity in enumerate(self.opportunities, start=1):
            opportunity.validate()
            if opportunity.evidence_rank != expected_rank:
                raise ValueError("live opportunity evidence ranks must be contiguous")
        positive_count = sum(
            item.qualification_state == "QUALIFIED_POSITIVE_EVIDENCE"
            for item in self.opportunities
        )
        mixed_count = sum(
            item.qualification_state == "QUALIFIED_MIXED_EVIDENCE"
            for item in self.opportunities
        )
        if self.qualified_positive_count != positive_count:
            raise ValueError("live opportunity positive count is inconsistent")
        if self.qualified_mixed_count != mixed_count:
            raise ValueError("live opportunity mixed count is inconsistent")
        if (
            not self.operator_review_required
            or self.trade_actionable
            or self.strategy_parameters_changed
            or self.strategy_promotion_allowed
            or self.demo_activation_allowed
            or self.live_activation_allowed
            or self.bybit_live_order_routing_allowed
        ):
            raise ValueError("live opportunity snapshot cannot activate trading")

    @property
    def snapshot_id(self) -> str:
        canonical = json.dumps(
            self.to_payload(include_snapshot_id=False),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def to_payload(self, *, include_snapshot_id: bool = True) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "schema": "BYBIT_LIVE_EVIDENCE_OPPORTUNITY_V111",
            "observed_at_ms": self.observed_at_ms,
            "market_snapshot_id": self.market_snapshot_id,
            "evidence_snapshot_id": self.evidence_snapshot_id,
            "equity_usdt": str(self.equity_usdt),
            "equity_source": self.equity_source,
            "qualified_positive_count": self.qualified_positive_count,
            "qualified_mixed_count": self.qualified_mixed_count,
            "opportunities": [item.to_payload() for item in self.opportunities],
            "ranking_semantics": (
                "lexicographic evidence order; no fitted composite score or parameter retuning"
            ),
            "operator_review_required": self.operator_review_required,
            "trade_actionable": self.trade_actionable,
            "strategy_parameters_changed": self.strategy_parameters_changed,
            "strategy_promotion_allowed": self.strategy_promotion_allowed,
            "demo_activation_allowed": self.demo_activation_allowed,
            "live_activation_allowed": self.live_activation_allowed,
            "bybit_live_order_routing_allowed": self.bybit_live_order_routing_allowed,
            "causal_claim_allowed": False,
            "predictive_guarantee_allowed": False,
        }
        if include_snapshot_id:
            payload["snapshot_id"] = self.snapshot_id
        return payload


def build_current_derivatives_context(
    history: BybitDerivativesHistory,
    *,
    decision_time: str,
) -> CryptoCurrentDerivativesContext:
    """Select derivatives observations known at or before the current signal decision."""

    history.validate()
    decision_ms = int(_parse_time(decision_time).timestamp() * 1000)
    current_oi = _latest_at_or_before(history.open_interest, decision_ms)
    previous_oi = (
        None
        if current_oi is None
        else _latest_before(history.open_interest, current_oi.timestamp_ms)
    )
    account_ratio = _latest_at_or_before(history.account_ratio, decision_ms)
    prior_funding = _latest_at_or_before(history.funding, decision_ms)
    missing: list[str] = []
    if current_oi is None:
        missing.append("OPEN_INTEREST_AT_OR_BEFORE_DECISION_MISSING")
    elif previous_oi is None:
        missing.append("OPEN_INTEREST_PREVIOUS_POINT_MISSING")
    if account_ratio is None:
        missing.append("ACCOUNT_RATIO_AT_OR_BEFORE_DECISION_MISSING")
    if prior_funding is None:
        missing.append("PRIOR_FUNDING_AT_OR_BEFORE_DECISION_MISSING")

    oi_delta = None
    oi_delta_fraction = None
    if current_oi is not None and previous_oi is not None:
        oi_delta = current_oi.open_interest - previous_oi.open_interest
        if previous_oi.open_interest <= 0:
            missing.append("OPEN_INTEREST_PREVIOUS_NON_POSITIVE")
        else:
            oi_delta_fraction = oi_delta / previous_oi.open_interest
    context = CryptoCurrentDerivativesContext(
        symbol=history.symbol,
        decision_time=decision_time,
        open_interest_timestamp_ms=(
            None if current_oi is None else current_oi.timestamp_ms
        ),
        open_interest=None if current_oi is None else current_oi.open_interest,
        previous_open_interest=(
            None if previous_oi is None else previous_oi.open_interest
        ),
        open_interest_delta=oi_delta,
        open_interest_delta_fraction=oi_delta_fraction,
        account_ratio_timestamp_ms=(
            None if account_ratio is None else account_ratio.timestamp_ms
        ),
        long_account_ratio=None if account_ratio is None else account_ratio.buy_ratio,
        short_account_ratio=None if account_ratio is None else account_ratio.sell_ratio,
        prior_funding_timestamp_ms=(
            None if prior_funding is None else prior_funding.timestamp_ms
        ),
        prior_funding_rate=(
            None if prior_funding is None else prior_funding.funding_rate
        ),
        decision_context_complete=not missing,
        missing_reasons=tuple(missing),
    )
    context.validate()
    return context


def build_crypto_live_opportunity_snapshot(
    market_snapshot: BybitOpportunitySnapshot,
    *,
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    derivatives_histories: Mapping[str, BybitDerivativesHistory],
    evidence_report: Mapping[str, Any],
    equity_usdt: Decimal,
    equity_source: str,
    strategy_config: CryptoPerpStrategyConfig | None = None,
) -> CryptoLiveOpportunitySnapshot:
    """Rank current fixed-strategy signals against the last qualified evidence matrix."""

    market_snapshot.validate()
    if not equity_usdt.is_finite() or equity_usdt <= 0:
        raise ValueError("live opportunity equity must be positive and finite")
    if not equity_source:
        raise ValueError("live opportunity equity source is required")
    default_config = CryptoPerpStrategyConfig()
    config = default_config if strategy_config is None else strategy_config
    config.validate()
    if config != default_config:
        raise ValueError("live opportunity ranking requires the qualified fixed strategy config")

    matrix = _validated_evidence_report(evidence_report)
    policy = _evidence_policy_from_report(matrix)
    turnover_reference = _required_decimal(matrix, "turnover_reference_usdt")
    if turnover_reference < 0:
        raise ValueError("live opportunity turnover reference cannot be negative")
    evidence_snapshot_id = _evidence_snapshot_id(matrix)
    cells = _evidence_cell_map(matrix)

    preliminary = [
        _build_candidate(
            market_candidate,
            bars=bars_by_symbol.get(market_candidate.symbol),
            derivatives_history=derivatives_histories.get(market_candidate.symbol),
            cells=cells,
            turnover_reference=turnover_reference,
            equity_usdt=equity_usdt,
            config=config,
            policy=policy,
        )
        for market_candidate in market_snapshot.candidates
    ]
    ordered = sorted(preliminary, key=_ranking_key)
    ranked = tuple(
        replace(item, evidence_rank=index)
        for index, item in enumerate(ordered, start=1)
    )
    snapshot = CryptoLiveOpportunitySnapshot(
        observed_at_ms=market_snapshot.observed_at_ms,
        market_snapshot_id=market_snapshot.snapshot_id,
        evidence_snapshot_id=evidence_snapshot_id,
        equity_usdt=equity_usdt,
        equity_source=equity_source,
        opportunities=ranked,
        qualified_positive_count=sum(
            item.qualification_state == "QUALIFIED_POSITIVE_EVIDENCE"
            for item in ranked
        ),
        qualified_mixed_count=sum(
            item.qualification_state == "QUALIFIED_MIXED_EVIDENCE"
            for item in ranked
        ),
    )
    snapshot.validate()
    return snapshot


def _build_candidate(
    market: BybitOpportunityCandidate,
    *,
    bars: Sequence[BybitKlineBar] | None,
    derivatives_history: BybitDerivativesHistory | None,
    cells: Mapping[str, Mapping[str, Any]],
    turnover_reference: Decimal,
    equity_usdt: Decimal,
    config: CryptoPerpStrategyConfig,
    policy: CryptoStrategyEvidencePolicy,
) -> CryptoLiveOpportunity:
    market.validate()
    if not bars:
        return _empty_opportunity(
            market,
            state="MARKET_HISTORY_UNAVAILABLE",
            reasons=("CURRENT_COMPLETED_KLINE_HISTORY_MISSING",),
        )
    ordered_bars = tuple(sorted(bars, key=lambda item: item.start_time))
    evaluation = evaluate_crypto_signal(ordered_bars, config)
    if not evaluation.eligible or evaluation.signal is None:
        return _empty_opportunity(
            market,
            state="NO_FIXED_STRATEGY_SIGNAL",
            reasons=evaluation.reasons or ("NO_FIXED_STRATEGY_SIGNAL",),
        )
    signal = evaluation.signal
    plan_evaluation = build_trade_plan(signal, equity_usdt=equity_usdt, config=config)
    if not plan_evaluation.eligible or plan_evaluation.plan is None:
        return _signal_opportunity(
            market,
            signal,
            state="TRADE_PLAN_REJECTED",
            reasons=plan_evaluation.reasons or ("TRADE_PLAN_REJECTED",),
        )
    plan = plan_evaluation.plan
    if derivatives_history is None:
        return _signal_plan_opportunity(
            market,
            signal,
            plan,
            state="DERIVATIVES_CONTEXT_INCOMPLETE",
            reasons=("CURRENT_DERIVATIVES_HISTORY_MISSING",),
        )

    derivatives = build_current_derivatives_context(
        derivatives_history,
        decision_time=signal.decision_time,
    )
    market_regime, volatility, _trend, _breakout, _turnover = (
        classify_crypto_signal_market_regime(
            signal,
            turnover_reference_usdt=turnover_reference,
            strategy_config=config,
        )
    )
    stress, stress_score, stress_complete, stress_reasons = classify_crypto_stress_regime(
        volatility_regime=volatility,
        one_bar_atr_multiple=signal.one_bar_atr_multiple,
        open_interest_delta_fraction=derivatives.open_interest_delta_fraction,
        crowding_regime=derivatives.crowding_regime,
        prior_funding_regime=derivatives.prior_funding_regime,
        decision_context_complete=derivatives.decision_context_complete,
        missing_reasons=derivatives.missing_reasons,
        strategy_config=config,
        policy=policy,
    )
    if not stress_complete:
        return _full_current_opportunity(
            market,
            signal,
            plan,
            derivatives,
            market_regime=market_regime,
            stress_regime=stress,
            stress_score=stress_score,
            state="DERIVATIVES_CONTEXT_INCOMPLETE",
            reasons=stress_reasons,
        )

    cell_key = "|".join(
        (
            signal.symbol,
            signal.side.value,
            market_regime,
            derivatives.open_interest_regime,
            derivatives.crowding_regime,
            derivatives.prior_funding_regime,
            stress,
        )
    )
    cell = cells.get(cell_key)
    if cell is None:
        return _full_current_opportunity(
            market,
            signal,
            plan,
            derivatives,
            market_regime=market_regime,
            stress_regime=stress,
            stress_score=stress_score,
            state="NO_SAMPLE_SUFFICIENT_EXACT_CELL",
            reasons=("NO_EXACT_HISTORICAL_CELL",),
            cell_key=cell_key,
        )
    if not _required_bool(cell, "sample_sufficient"):
        return _full_current_opportunity(
            market,
            signal,
            plan,
            derivatives,
            market_regime=market_regime,
            stress_regime=stress,
            stress_score=stress_score,
            state="NO_SAMPLE_SUFFICIENT_EXACT_CELL",
            reasons=("HISTORICAL_CELL_SAMPLE_INSUFFICIENT",),
            cell=cell,
            cell_key=cell_key,
        )

    positive = _positive_cell_evidence(cell)
    state = (
        "QUALIFIED_POSITIVE_EVIDENCE"
        if positive
        else "QUALIFIED_MIXED_EVIDENCE"
    )
    reasons = (
        ("SAMPLE_SUFFICIENT_CELL_WITH_POSITIVE_HISTORICAL_EVIDENCE",)
        if positive
        else ("SAMPLE_SUFFICIENT_CELL_WITH_MIXED_HISTORICAL_EVIDENCE",)
    )
    return _full_current_opportunity(
        market,
        signal,
        plan,
        derivatives,
        market_regime=market_regime,
        stress_regime=stress,
        stress_score=stress_score,
        state=state,
        reasons=reasons,
        cell=cell,
        cell_key=cell_key,
        positive=positive,
    )


def _empty_opportunity(
    market: BybitOpportunityCandidate,
    *,
    state: str,
    reasons: tuple[str, ...],
) -> CryptoLiveOpportunity:
    return CryptoLiveOpportunity(
        evidence_rank=market.rank,
        market_rank=market.rank,
        symbol=market.symbol,
        market_universe_score=market.universe_score,
        qualification_state=state,
        qualification_reasons=reasons,
        signal_side=None,
        decision_time=None,
        signal_quality_score=None,
        current_market_regime=None,
        current_open_interest_regime=None,
        current_crowding_regime=None,
        current_prior_funding_regime=None,
        current_stress_regime=None,
        current_stress_score=None,
        expected_net_edge_usd=None,
        planned_notional_usdt=None,
        risk_budget_usdt=None,
        estimated_round_trip_cost_usdt=None,
        evidence_cell_key=None,
        evidence_trade_count=None,
        evidence_sample_sufficient=False,
        evidence_profit_factor=None,
        evidence_win_rate=None,
        evidence_total_net_pnl_usdt=None,
        evidence_average_net_pnl_usdt=None,
        evidence_average_mfe_r=None,
        evidence_average_mae_r=None,
        evidence_drawdown_usdt=None,
        positive_historical_evidence=False,
    )


def _signal_opportunity(
    market: BybitOpportunityCandidate,
    signal: CryptoSignal,
    *,
    state: str,
    reasons: tuple[str, ...],
) -> CryptoLiveOpportunity:
    return replace(
        _empty_opportunity(market, state=state, reasons=reasons),
        signal_side=signal.side.value,
        decision_time=signal.decision_time,
        signal_quality_score=signal.quality_score,
    )


def _signal_plan_opportunity(
    market: BybitOpportunityCandidate,
    signal: CryptoSignal,
    plan: Any,
    *,
    state: str,
    reasons: tuple[str, ...],
) -> CryptoLiveOpportunity:
    return replace(
        _signal_opportunity(market, signal, state=state, reasons=reasons),
        expected_net_edge_usd=plan.expected_net_edge_usd,
        planned_notional_usdt=plan.notional_usdt,
        risk_budget_usdt=plan.risk_budget_usdt,
        estimated_round_trip_cost_usdt=plan.estimated_round_trip_cost_usdt,
    )


def _full_current_opportunity(
    market: BybitOpportunityCandidate,
    signal: CryptoSignal,
    plan: Any,
    derivatives: CryptoCurrentDerivativesContext,
    *,
    market_regime: str,
    stress_regime: str,
    stress_score: int,
    state: str,
    reasons: tuple[str, ...],
    cell: Mapping[str, Any] | None = None,
    cell_key: str | None = None,
    positive: bool = False,
) -> CryptoLiveOpportunity:
    return replace(
        _signal_plan_opportunity(
            market,
            signal,
            plan,
            state=state,
            reasons=reasons,
        ),
        current_market_regime=market_regime,
        current_open_interest_regime=derivatives.open_interest_regime,
        current_crowding_regime=derivatives.crowding_regime,
        current_prior_funding_regime=derivatives.prior_funding_regime,
        current_stress_regime=stress_regime,
        current_stress_score=stress_score,
        evidence_cell_key=cell_key,
        evidence_trade_count=_optional_int(cell, "trade_count"),
        evidence_sample_sufficient=(
            False if cell is None else _required_bool(cell, "sample_sufficient")
        ),
        evidence_profit_factor=_optional_decimal(cell, "profit_factor"),
        evidence_win_rate=_optional_decimal(cell, "win_rate"),
        evidence_total_net_pnl_usdt=_optional_decimal(cell, "total_net_pnl_usdt"),
        evidence_average_net_pnl_usdt=_optional_decimal(cell, "average_net_pnl_usdt"),
        evidence_average_mfe_r=_optional_decimal(cell, "average_mfe_r"),
        evidence_average_mae_r=_optional_decimal(cell, "average_mae_r"),
        evidence_drawdown_usdt=_optional_decimal(
            cell,
            "maximum_trade_sequence_drawdown_usdt",
        ),
        positive_historical_evidence=positive,
    )


def _positive_cell_evidence(cell: Mapping[str, Any]) -> bool:
    total = _required_decimal(cell, "total_net_pnl_usdt")
    average = _required_decimal(cell, "average_net_pnl_usdt")
    loss_count = _required_int(cell, "loss_count")
    profit_factor = _optional_decimal(cell, "profit_factor")
    profitable_factor = loss_count == 0 if profit_factor is None else profit_factor > _ONE
    return total > 0 and average > 0 and profitable_factor


def _ranking_key(item: CryptoLiveOpportunity) -> tuple[Any, ...]:
    priority = _STATE_PRIORITY[item.qualification_state]
    no_loss_pf = (
        item.evidence_sample_sufficient
        and item.evidence_profit_factor is None
        and (item.evidence_total_net_pnl_usdt or _ZERO) > 0
    )
    profit_factor = (
        Decimal("Infinity")
        if no_loss_pf
        else (item.evidence_profit_factor or Decimal("-Infinity"))
    )
    average_pnl = item.evidence_average_net_pnl_usdt or Decimal("-Infinity")
    sample = item.evidence_trade_count or 0
    quality = item.signal_quality_score or Decimal("-Infinity")
    return (
        priority,
        -profit_factor,
        -average_pnl,
        -sample,
        -quality,
        item.market_rank,
        item.symbol,
    )


def _validated_evidence_report(report: Mapping[str, Any]) -> Mapping[str, Any]:
    if report.get("diagnostic") != "BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX":
        raise ValueError("live opportunity requires strategy evidence matrix diagnostic")
    for field in (
        "parameter_retuning_performed",
        "strategy_selection_allowed",
        "strategy_promotion_allowed",
        "demo_activation_allowed",
        "live_activation_allowed",
        "bybit_live_order_routing_allowed",
        "causal_claim_allowed",
        "predictive_guarantee_allowed",
    ):
        if report.get(field) is not False:
            raise ValueError(f"live opportunity rejected unsafe evidence flag:{field}")
    matrix = report.get("matrix")
    if not isinstance(matrix, list):
        raise ValueError("live opportunity evidence matrix rows are missing")
    turnover = _required_decimal(report, "turnover_reference_usdt")
    if turnover < 0:
        raise ValueError("live opportunity evidence turnover reference cannot be negative")
    _evidence_policy_from_report(report)
    return report


def _evidence_policy_from_report(report: Mapping[str, Any]) -> CryptoStrategyEvidencePolicy:
    stress = report.get("stress_policy")
    if not isinstance(stress, Mapping):
        raise ValueError("live opportunity evidence stress policy is missing")
    policy = CryptoStrategyEvidencePolicy(
        minimum_cell_trades=_required_int(report, "minimum_cell_trades"),
        open_interest_impulse_fraction=_required_decimal(
            stress,
            "open_interest_impulse_fraction",
        ),
        high_stress_feature_count=_required_int(stress, "high_stress_feature_count"),
        elevated_stress_feature_count=_required_int(
            stress,
            "elevated_stress_feature_count",
        ),
    )
    policy.validate()
    return policy


def _evidence_cell_map(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = report.get("matrix")
    if not isinstance(raw, list):
        raise ValueError("live opportunity evidence matrix rows are missing")
    result: dict[str, Mapping[str, Any]] = {}
    for cell in raw:
        if not isinstance(cell, Mapping):
            raise ValueError("live opportunity evidence cell must be an object")
        key = cell.get("cell_key")
        if not isinstance(key, str) or not key:
            raise ValueError("live opportunity evidence cell key is missing")
        if key in result:
            raise ValueError("live opportunity evidence matrix has duplicate cell key")
        _required_int(cell, "trade_count")
        _required_bool(cell, "sample_sufficient")
        result[key] = cell
    return result


def _evidence_snapshot_id(report: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _latest_at_or_before(points: Sequence[Any], timestamp_ms: int) -> Any | None:
    timestamps = [item.timestamp_ms for item in points]
    index = bisect_right(timestamps, timestamp_ms) - 1
    return None if index < 0 else points[index]


def _latest_before(points: Sequence[Any], timestamp_ms: int) -> Any | None:
    timestamps = [item.timestamp_ms for item in points]
    index = bisect_left(timestamps, timestamp_ms) - 1
    return None if index < 0 else points[index]


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise ValueError(f"live opportunity evidence missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"live opportunity evidence invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"live opportunity evidence non-finite {field}")
    return parsed


def _optional_decimal(row: Mapping[str, Any] | None, field: str) -> Decimal | None:
    if row is None or row.get(field) is None:
        return None
    return _required_decimal(row, field)


def _required_int(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise ValueError(f"live opportunity evidence missing {field}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"live opportunity evidence invalid {field}") from exc
    if parsed < 0:
        raise ValueError(f"live opportunity evidence negative {field}")
    return parsed


def _optional_int(row: Mapping[str, Any] | None, field: str) -> int | None:
    if row is None:
        return None
    return _required_int(row, field)


def _required_bool(row: Mapping[str, Any], field: str) -> bool:
    value = row.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"live opportunity evidence invalid boolean {field}")
    return value


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("live opportunity timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("live opportunity timestamp must be timezone-aware")
    return parsed
