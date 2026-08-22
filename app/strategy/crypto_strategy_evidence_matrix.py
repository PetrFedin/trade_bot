from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.strategy.crypto_derivatives_context import CryptoTradeDerivativesContext
from app.strategy.crypto_historical_diagnostics import CryptoHistoricalTradeCondition
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSignal

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS = Decimal("10000")


@dataclass(frozen=True)
class CryptoStrategyEvidencePolicy:
    minimum_cell_trades: int = 5
    open_interest_impulse_fraction: Decimal = Decimal("0.01")
    high_stress_feature_count: int = 3
    elevated_stress_feature_count: int = 1

    def validate(self) -> None:
        if not 1 <= self.minimum_cell_trades <= 100_000:
            raise ValueError("crypto evidence minimum cell trades must be within [1, 100000]")
        if (
            not self.open_interest_impulse_fraction.is_finite()
            or self.open_interest_impulse_fraction <= 0
        ):
            raise ValueError("crypto evidence OI impulse fraction must be positive and finite")
        if not 1 <= self.elevated_stress_feature_count <= self.high_stress_feature_count:
            raise ValueError("crypto evidence stress feature thresholds are invalid")
        if self.high_stress_feature_count > 5:
            raise ValueError("crypto evidence high stress threshold cannot exceed feature count")


@dataclass(frozen=True)
class CryptoTradeExecutionEconomics:
    symbol: str
    side: str
    decision_time: str
    entry_time: str
    entry_price: Decimal
    quantity: Decimal
    notional_usdt: Decimal
    expected_net_edge_usd: Decimal
    minimum_entry_net_edge_usd: Decimal
    risk_budget_usdt: Decimal
    modeled_round_trip_cost_usdt: Decimal
    cost_to_expected_edge: Decimal
    expected_edge_to_risk: Decimal

    def validate(self) -> None:
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("crypto execution economics side is invalid")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("crypto execution economics symbol is invalid")
        _parse_time(self.decision_time)
        _parse_time(self.entry_time)
        for name, value in (
            ("entry_price", self.entry_price),
            ("quantity", self.quantity),
            ("notional_usdt", self.notional_usdt),
            ("expected_net_edge_usd", self.expected_net_edge_usd),
            ("minimum_entry_net_edge_usd", self.minimum_entry_net_edge_usd),
            ("risk_budget_usdt", self.risk_budget_usdt),
            ("modeled_round_trip_cost_usdt", self.modeled_round_trip_cost_usdt),
            ("cost_to_expected_edge", self.cost_to_expected_edge),
            ("expected_edge_to_risk", self.expected_edge_to_risk),
        ):
            if not value.is_finite():
                raise ValueError(f"crypto execution economics {name} must be finite")
        if self.entry_price <= 0 or self.quantity <= 0 or self.notional_usdt <= 0:
            raise ValueError("crypto execution economics price/quantity/notional must be positive")
        if self.expected_net_edge_usd <= 0 or self.minimum_entry_net_edge_usd <= 0:
            raise ValueError("crypto execution economics expected edge must be positive")
        if self.risk_budget_usdt <= 0:
            raise ValueError("crypto execution economics risk budget must be positive")
        if self.modeled_round_trip_cost_usdt < 0:
            raise ValueError("crypto execution economics modeled cost cannot be negative")
        if self.notional_usdt != self.entry_price * self.quantity:
            raise ValueError("crypto execution economics notional is inconsistent")
        if self.cost_to_expected_edge != (
            self.modeled_round_trip_cost_usdt / self.expected_net_edge_usd
        ):
            raise ValueError("crypto execution economics cost/edge ratio is inconsistent")
        if self.expected_edge_to_risk != self.expected_net_edge_usd / self.risk_budget_usdt:
            raise ValueError("crypto execution economics edge/risk ratio is inconsistent")


@dataclass(frozen=True)
class CryptoStrategyEvidenceRow:
    symbol: str
    side: str
    decision_time: str
    entry_time: str
    exit_time: str
    exit_reason: str
    net_pnl_usdt: Decimal
    maximum_favorable_r: Decimal
    maximum_adverse_r: Decimal
    market_regime: str
    volatility_regime: str
    trend_regime: str
    breakout_regime: str
    turnover_regime: str
    open_interest_regime: str
    crowding_regime: str
    prior_funding_regime: str
    stress_regime: str
    stress_score: int
    stress_feature_complete: bool
    stress_reasons: tuple[str, ...]
    open_interest_delta_fraction: Decimal | None
    long_account_ratio: Decimal | None
    prior_funding_rate: Decimal | None
    atr_fraction: Decimal
    one_bar_atr_multiple: Decimal
    quality_score: Decimal
    average_turnover_usdt: Decimal
    expected_net_edge_usd: Decimal
    modeled_round_trip_cost_usdt: Decimal
    cost_to_expected_edge: Decimal
    expected_edge_to_risk: Decimal
    liquidation_history_available: bool = False
    liquidation_event_source: str = "NOT_RECONSTRUCTED"

    @property
    def cell_key(self) -> str:
        return "|".join(
            (
                self.symbol,
                self.side,
                self.market_regime,
                self.open_interest_regime,
                self.crowding_regime,
                self.prior_funding_regime,
                self.stress_regime,
            )
        )

    def validate(self) -> None:
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("crypto evidence row side is invalid")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("crypto evidence row symbol is invalid")
        decision = _parse_time(self.decision_time)
        entry = _parse_time(self.entry_time)
        exit_at = _parse_time(self.exit_time)
        if not decision < entry <= exit_at:
            raise ValueError("crypto evidence row timestamps are not monotonic")
        for name, value in (
            ("net_pnl_usdt", self.net_pnl_usdt),
            ("maximum_favorable_r", self.maximum_favorable_r),
            ("maximum_adverse_r", self.maximum_adverse_r),
            ("atr_fraction", self.atr_fraction),
            ("one_bar_atr_multiple", self.one_bar_atr_multiple),
            ("quality_score", self.quality_score),
            ("average_turnover_usdt", self.average_turnover_usdt),
            ("expected_net_edge_usd", self.expected_net_edge_usd),
            ("modeled_round_trip_cost_usdt", self.modeled_round_trip_cost_usdt),
            ("cost_to_expected_edge", self.cost_to_expected_edge),
            ("expected_edge_to_risk", self.expected_edge_to_risk),
        ):
            if not value.is_finite():
                raise ValueError(f"crypto evidence row {name} must be finite")
        if self.average_turnover_usdt < 0:
            raise ValueError("crypto evidence row average turnover cannot be negative")
        for name, value in (
            ("open_interest_delta_fraction", self.open_interest_delta_fraction),
            ("long_account_ratio", self.long_account_ratio),
            ("prior_funding_rate", self.prior_funding_rate),
        ):
            if value is not None and not value.is_finite():
                raise ValueError(f"crypto evidence row {name} must be finite when present")
        if self.stress_score < 0 or self.stress_score > 5:
            raise ValueError("crypto evidence row stress score must be within [0, 5]")
        if self.stress_feature_complete and self.stress_regime == "STRESS_UNKNOWN":
            raise ValueError("complete crypto evidence stress cannot be unknown")
        if not self.stress_feature_complete and self.stress_regime != "STRESS_UNKNOWN":
            raise ValueError("incomplete crypto evidence stress must be unknown")
        if self.liquidation_history_available:
            raise ValueError("crypto evidence cannot claim unavailable liquidation history")
        if self.liquidation_event_source != "NOT_RECONSTRUCTED":
            raise ValueError("crypto evidence liquidation source must remain explicit")


def classify_crypto_signal_market_regime(
    signal: CryptoSignal,
    *,
    turnover_reference_usdt: Decimal,
    strategy_config: CryptoPerpStrategyConfig | None = None,
) -> tuple[str, str, str, str, str]:
    """Classify a current signal with the same regime contract used by historical evidence."""

    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    if not turnover_reference_usdt.is_finite() or turnover_reference_usdt < 0:
        raise ValueError("crypto evidence turnover reference must be finite and non-negative")
    if not signal.atr_fraction.is_finite() or signal.atr_fraction <= 0:
        raise ValueError("crypto evidence current signal ATR fraction must be positive")
    atr = signal.reference_price * signal.atr_fraction
    if not atr.is_finite() or atr <= 0:
        raise ValueError("crypto evidence current signal ATR must be positive")

    volatility_span = config.maximum_atr_fraction - config.minimum_atr_fraction
    lower = config.minimum_atr_fraction + volatility_span / Decimal("3")
    upper = config.minimum_atr_fraction + volatility_span * Decimal("2") / Decimal("3")
    if signal.atr_fraction <= lower:
        volatility_regime = "VOL_LOW_NORMAL"
    elif signal.atr_fraction <= upper:
        volatility_regime = "VOL_MID_NORMAL"
    else:
        volatility_regime = "VOL_HIGH_NORMAL"

    trend_strength = abs(signal.fast_ema - signal.slow_ema) / atr
    trend_regime = "TREND_STRONG" if trend_strength >= _ONE else "TREND_MODERATE"
    breakout_regime = (
        "BREAKOUT_CONFIRMED"
        if signal.breakout_strength_atr >= _ZERO
        else "BREAKOUT_PULLBACK"
    )
    turnover_regime = (
        "TURNOVER_HIGH"
        if signal.average_turnover_usdt >= turnover_reference_usdt
        else "TURNOVER_LOW"
    )
    market_regime = "|".join(
        (
            volatility_regime,
            trend_regime,
            breakout_regime,
            turnover_regime,
        )
    )
    return (
        market_regime,
        volatility_regime,
        trend_regime,
        breakout_regime,
        turnover_regime,
    )


def classify_crypto_stress_regime(
    *,
    volatility_regime: str,
    one_bar_atr_multiple: Decimal,
    open_interest_delta_fraction: Decimal | None,
    crowding_regime: str,
    prior_funding_regime: str,
    decision_context_complete: bool,
    missing_reasons: Sequence[str] = (),
    strategy_config: CryptoPerpStrategyConfig | None = None,
    policy: CryptoStrategyEvidencePolicy | None = None,
) -> tuple[str, int, bool, tuple[str, ...]]:
    """Apply one shared stress definition to historical and current point-in-time evidence."""

    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    active = CryptoStrategyEvidencePolicy() if policy is None else policy
    active.validate()
    if not one_bar_atr_multiple.is_finite() or one_bar_atr_multiple < 0:
        raise ValueError("crypto evidence one-bar ATR multiple must be finite and non-negative")
    if (
        open_interest_delta_fraction is not None
        and not open_interest_delta_fraction.is_finite()
    ):
        raise ValueError("crypto evidence OI delta fraction must be finite when present")

    reasons: list[str] = []
    score = 0
    if volatility_regime == "VOL_HIGH_NORMAL":
        score += 1
        reasons.append("HIGH_NORMAL_ATR_REGIME")
    price_shock_threshold = config.maximum_one_bar_atr_multiple / Decimal("2")
    if one_bar_atr_multiple >= price_shock_threshold:
        score += 1
        reasons.append("ONE_BAR_MOVE_AT_LEAST_HALF_STRATEGY_LIMIT")
    if (
        open_interest_delta_fraction is not None
        and abs(open_interest_delta_fraction) >= active.open_interest_impulse_fraction
    ):
        score += 1
        reasons.append("OPEN_INTEREST_IMPULSE")
    if crowding_regime in {"LONG_HEAVY", "SHORT_HEAVY"}:
        score += 1
        reasons.append("POSITION_HOLDER_CROWDING")
    funding_pressure = (
        crowding_regime == "LONG_HEAVY"
        and prior_funding_regime == "FUNDING_POSITIVE"
    ) or (
        crowding_regime == "SHORT_HEAVY"
        and prior_funding_regime == "FUNDING_NEGATIVE"
    )
    if funding_pressure:
        score += 1
        reasons.append("CROWDED_SIDE_PAYS_PRIOR_FUNDING")

    if not decision_context_complete:
        return (
            "STRESS_UNKNOWN",
            score,
            False,
            tuple(sorted(set(reasons) | set(missing_reasons))),
        )
    if score >= active.high_stress_feature_count:
        regime = "STRESS_HIGH"
    elif score >= active.elevated_stress_feature_count:
        regime = "STRESS_ELEVATED"
    else:
        regime = "STRESS_CALM"
    return regime, score, True, tuple(reasons)


def build_crypto_trade_execution_economics(
    replay: Mapping[str, Any],
    *,
    strategy_config: CryptoPerpStrategyConfig | None = None,
) -> tuple[CryptoTradeExecutionEconomics, ...]:
    """Reconstruct modeled entry economics from persisted replay ENTRY events."""

    _validate_research_boundary(replay)
    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    raw_events = replay.get("decision_events")
    if not isinstance(raw_events, list):
        raise ValueError("crypto execution economics requires replay decision_events")
    one_way_rate = config.taker_fee_rate + config.slippage_bps_per_fill / _BPS
    result: list[CryptoTradeExecutionEconomics] = []
    seen: set[tuple[str, str]] = set()
    for event in raw_events:
        if not isinstance(event, Mapping) or event.get("event") != "ENTRY":
            continue
        symbol = _required_text(event, "symbol")
        side = _required_text(event, "side")
        decision_time = _required_text(event, "decision_time")
        entry_time = _required_text(event, "execution_time")
        key = (symbol, entry_time)
        if key in seen:
            raise ValueError("crypto execution economics duplicate ENTRY event key")
        seen.add(key)
        entry_price = _required_positive_decimal(event, "entry_price")
        quantity = _required_positive_decimal(event, "quantity")
        expected_edge = _required_positive_decimal(event, "expected_net_edge_usd")
        minimum_edge = _required_positive_decimal(event, "minimum_entry_net_edge_usd")
        risk_budget = _required_positive_decimal(event, "risk_budget_usdt")
        notional = entry_price * quantity
        modeled_cost = notional * one_way_rate * Decimal("2")
        economics = CryptoTradeExecutionEconomics(
            symbol=symbol,
            side=side,
            decision_time=decision_time,
            entry_time=entry_time,
            entry_price=entry_price,
            quantity=quantity,
            notional_usdt=notional,
            expected_net_edge_usd=expected_edge,
            minimum_entry_net_edge_usd=minimum_edge,
            risk_budget_usdt=risk_budget,
            modeled_round_trip_cost_usdt=modeled_cost,
            cost_to_expected_edge=modeled_cost / expected_edge,
            expected_edge_to_risk=expected_edge / risk_budget,
        )
        economics.validate()
        result.append(economics)
    return tuple(result)


def build_crypto_strategy_evidence_rows(
    conditions: Sequence[CryptoHistoricalTradeCondition],
    derivatives: Sequence[CryptoTradeDerivativesContext],
    economics: Sequence[CryptoTradeExecutionEconomics],
    *,
    strategy_config: CryptoPerpStrategyConfig | None = None,
    policy: CryptoStrategyEvidencePolicy | None = None,
) -> tuple[CryptoStrategyEvidenceRow, ...]:
    """Join point-in-time price, derivatives and modeled execution evidence per trade."""

    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    active = CryptoStrategyEvidencePolicy() if policy is None else policy
    active.validate()
    derivative_map = {_identity(item): item for item in derivatives}
    economics_map = {_economics_identity(item): item for item in economics}
    if len(derivative_map) != len(derivatives):
        raise ValueError("crypto evidence derivatives contain duplicate trade identities")
    if len(economics_map) != len(economics):
        raise ValueError("crypto evidence economics contain duplicate trade identities")

    rows: list[CryptoStrategyEvidenceRow] = []
    seen_conditions: set[tuple[str, str, str, str, str]] = set()
    for condition in conditions:
        key = _condition_identity(condition)
        if key in seen_conditions:
            raise ValueError("crypto evidence conditions contain duplicate trade identities")
        seen_conditions.add(key)
        derivative = derivative_map.get(key)
        if derivative is None:
            raise ValueError("crypto evidence missing derivatives row for closed trade")
        economics_row = economics_map.get((condition.symbol, condition.entry_time))
        if economics_row is None:
            raise ValueError("crypto evidence missing execution economics for closed trade")
        if (
            economics_row.side != condition.side
            or economics_row.decision_time != condition.decision_time
        ):
            raise ValueError("crypto evidence execution economics identity mismatch")
        if economics_row.expected_net_edge_usd != condition.expected_net_edge_usd:
            raise ValueError("crypto evidence expected-edge sources do not reconcile")

        stress_regime, stress_score, complete, reasons = _stress_context(
            condition,
            derivative,
            config=config,
            policy=active,
        )
        market_regime = "|".join(
            (
                condition.volatility_regime,
                condition.trend_regime,
                condition.breakout_regime,
                condition.turnover_regime,
            )
        )
        row = CryptoStrategyEvidenceRow(
            symbol=condition.symbol,
            side=condition.side,
            decision_time=condition.decision_time,
            entry_time=condition.entry_time,
            exit_time=condition.exit_time,
            exit_reason=condition.exit_reason,
            net_pnl_usdt=condition.net_pnl_usdt,
            maximum_favorable_r=condition.maximum_favorable_r,
            maximum_adverse_r=condition.maximum_adverse_r,
            market_regime=market_regime,
            volatility_regime=condition.volatility_regime,
            trend_regime=condition.trend_regime,
            breakout_regime=condition.breakout_regime,
            turnover_regime=condition.turnover_regime,
            open_interest_regime=derivative.open_interest_regime,
            crowding_regime=derivative.crowding_regime,
            prior_funding_regime=derivative.prior_funding_regime,
            stress_regime=stress_regime,
            stress_score=stress_score,
            stress_feature_complete=complete,
            stress_reasons=reasons,
            open_interest_delta_fraction=derivative.open_interest_delta_fraction,
            long_account_ratio=derivative.long_account_ratio,
            prior_funding_rate=derivative.prior_funding_rate,
            atr_fraction=condition.atr_fraction,
            one_bar_atr_multiple=condition.one_bar_atr_multiple,
            quality_score=condition.quality_score,
            average_turnover_usdt=condition.average_turnover_usdt,
            expected_net_edge_usd=condition.expected_net_edge_usd,
            modeled_round_trip_cost_usdt=economics_row.modeled_round_trip_cost_usdt,
            cost_to_expected_edge=economics_row.cost_to_expected_edge,
            expected_edge_to_risk=economics_row.expected_edge_to_risk,
        )
        row.validate()
        rows.append(row)
    if len(derivative_map) != len(rows):
        raise ValueError("crypto evidence contains unmatched derivatives rows")
    return tuple(rows)


def diagnose_crypto_strategy_evidence_matrix(
    rows: Sequence[CryptoStrategyEvidenceRow],
    *,
    policy: CryptoStrategyEvidencePolicy | None = None,
    strategy_config: CryptoPerpStrategyConfig | None = None,
) -> dict[str, Any]:
    """Build descriptive coin x side x regime evidence cells from closed trades."""

    active = CryptoStrategyEvidencePolicy() if policy is None else policy
    active.validate()
    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    records = tuple(rows)
    for row in records:
        row.validate()
    grouped: dict[str, list[CryptoStrategyEvidenceRow]] = defaultdict(list)
    by_symbol_side: dict[str, list[CryptoStrategyEvidenceRow]] = defaultdict(list)
    by_stress: dict[str, list[CryptoStrategyEvidenceRow]] = defaultdict(list)
    for row in records:
        grouped[row.cell_key].append(row)
        by_symbol_side[f"{row.symbol}|{row.side}"].append(row)
        by_stress[row.stress_regime].append(row)

    matrix = []
    for key, members in sorted(grouped.items()):
        first = members[0]
        matrix.append(
            {
                "cell_key": key,
                "symbol": first.symbol,
                "side": first.side,
                "market_regime": first.market_regime,
                "open_interest_regime": first.open_interest_regime,
                "crowding_regime": first.crowding_regime,
                "prior_funding_regime": first.prior_funding_regime,
                "stress_regime": first.stress_regime,
                **_summary(members, minimum_trades=active.minimum_cell_trades),
            }
        )

    turnover_reference = (
        None
        if not records
        else _median_decimal([item.average_turnover_usdt for item in records])
    )
    return {
        "diagnostic": "BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX",
        "trade_count": len(records),
        "cell_count": len(matrix),
        "minimum_cell_trades": active.minimum_cell_trades,
        "turnover_reference_usdt": (
            None if turnover_reference is None else str(turnover_reference)
        ),
        "dimensions": [
            "symbol",
            "side",
            "market_regime",
            "open_interest_regime",
            "crowding_regime",
            "prior_funding_regime",
            "stress_regime",
        ],
        "aggregate": _summary(records, minimum_trades=active.minimum_cell_trades),
        "by_symbol_side": {
            key: _summary(members, minimum_trades=active.minimum_cell_trades)
            for key, members in sorted(by_symbol_side.items())
        },
        "by_stress_regime": {
            key: _summary(members, minimum_trades=active.minimum_cell_trades)
            for key, members in sorted(by_stress.items())
        },
        "matrix": matrix,
        "stress_policy": {
            "open_interest_impulse_fraction": str(active.open_interest_impulse_fraction),
            "price_shock_atr_threshold": str(
                config.maximum_one_bar_atr_multiple / Decimal("2")
            ),
            "high_stress_feature_count": active.high_stress_feature_count,
            "elevated_stress_feature_count": active.elevated_stress_feature_count,
            "feature_count": 5,
        },
        "execution_economics": {
            "source": "REPLAY_ENTRY_PLUS_FIXED_STRATEGY_FEE_SLIPPAGE_ASSUMPTIONS",
            "broker_fee_ledger_reconciled": False,
            "historical_order_book_depth_reconstructed": False,
        },
        "liquidation_context": {
            "historical_market_wide_liquidation_events_available": False,
            "source": "NOT_RECONSTRUCTED",
            "stress_proxy_used_instead": True,
        },
        "feature_timing_contract": (
            "price/EMA/ATR and derivatives features are known at or before decision_time; "
            "realized PnL/MFE/MAE are outcomes used only for retrospective grouping"
        ),
        "interpretation_contract": (
            "historical associations and execution-model evidence only; cells do not establish "
            "causality, do not guarantee future profit and cannot promote a strategy"
        ),
        "parameter_retuning_performed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _stress_context(
    condition: CryptoHistoricalTradeCondition,
    derivative: CryptoTradeDerivativesContext,
    *,
    config: CryptoPerpStrategyConfig,
    policy: CryptoStrategyEvidencePolicy,
) -> tuple[str, int, bool, tuple[str, ...]]:
    if _condition_identity(condition) != _identity(derivative):
        raise ValueError("crypto evidence condition/derivatives identity mismatch")
    return classify_crypto_stress_regime(
        volatility_regime=condition.volatility_regime,
        one_bar_atr_multiple=condition.one_bar_atr_multiple,
        open_interest_delta_fraction=derivative.open_interest_delta_fraction,
        crowding_regime=derivative.crowding_regime,
        prior_funding_regime=derivative.prior_funding_regime,
        decision_context_complete=derivative.decision_context_complete,
        missing_reasons=derivative.missing_reasons,
        strategy_config=config,
        policy=policy,
    )


def _summary(
    records: Sequence[CryptoStrategyEvidenceRow],
    *,
    minimum_trades: int,
) -> dict[str, Any]:
    count = len(records)
    if count == 0:
        return {
            "trade_count": 0,
            "sample_sufficient": False,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": None,
            "total_net_pnl_usdt": 0.0,
            "average_net_pnl_usdt": None,
            "profit_factor": None,
            "average_mfe_r": None,
            "average_mae_r": None,
            "maximum_trade_sequence_drawdown_usdt": None,
            "average_turnover_usdt": None,
            "average_expected_net_edge_usd": None,
            "average_modeled_round_trip_cost_usdt": None,
            "average_cost_to_expected_edge": None,
            "average_expected_edge_to_risk": None,
        }
    ordered = sorted(
        records,
        key=lambda item: (_parse_time(item.exit_time), item.symbol, item.side),
    )
    wins = [item for item in records if item.net_pnl_usdt > 0]
    losses = [item for item in records if item.net_pnl_usdt < 0]
    gross_profit = sum((item.net_pnl_usdt for item in wins), start=_ZERO)
    gross_loss = -sum((item.net_pnl_usdt for item in losses), start=_ZERO)
    total = sum((item.net_pnl_usdt for item in records), start=_ZERO)
    profit_factor = None if gross_loss == 0 else gross_profit / gross_loss
    denominator = Decimal(count)
    return {
        "trade_count": count,
        "sample_sufficient": count >= minimum_trades,
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": float(Decimal(len(wins)) / denominator),
        "total_net_pnl_usdt": float(total),
        "average_net_pnl_usdt": float(total / denominator),
        "profit_factor": None if profit_factor is None else float(profit_factor),
        "average_mfe_r": float(
            sum((item.maximum_favorable_r for item in records), start=_ZERO) / denominator
        ),
        "average_mae_r": float(
            sum((item.maximum_adverse_r for item in records), start=_ZERO) / denominator
        ),
        "maximum_trade_sequence_drawdown_usdt": float(_trade_sequence_drawdown(ordered)),
        "average_turnover_usdt": float(
            sum((item.average_turnover_usdt for item in records), start=_ZERO) / denominator
        ),
        "average_expected_net_edge_usd": float(
            sum((item.expected_net_edge_usd for item in records), start=_ZERO) / denominator
        ),
        "average_modeled_round_trip_cost_usdt": float(
            sum((item.modeled_round_trip_cost_usdt for item in records), start=_ZERO)
            / denominator
        ),
        "average_cost_to_expected_edge": float(
            sum((item.cost_to_expected_edge for item in records), start=_ZERO) / denominator
        ),
        "average_expected_edge_to_risk": float(
            sum((item.expected_edge_to_risk for item in records), start=_ZERO) / denominator
        ),
    }


def _trade_sequence_drawdown(records: Sequence[CryptoStrategyEvidenceRow]) -> Decimal:
    cumulative = _ZERO
    peak = _ZERO
    maximum = _ZERO
    for item in records:
        cumulative += item.net_pnl_usdt
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum


def _median_decimal(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("crypto evidence median requires values")
    if any(not value.is_finite() for value in values):
        raise ValueError("crypto evidence median values must be finite")
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")


def _condition_identity(
    item: CryptoHistoricalTradeCondition,
) -> tuple[str, str, str, str, str]:
    return item.symbol, item.side, item.decision_time, item.entry_time, item.exit_time


def _identity(
    item: CryptoTradeDerivativesContext,
) -> tuple[str, str, str, str, str]:
    return item.symbol, item.side, item.decision_time, item.entry_time, item.exit_time


def _economics_identity(item: CryptoTradeExecutionEconomics) -> tuple[str, str]:
    return item.symbol, item.entry_time


def _validate_research_boundary(replay: Mapping[str, Any]) -> None:
    for field in ("strategy_promotion_allowed", "bybit_live_order_routing_allowed"):
        if replay.get(field) is not False:
            raise ValueError(f"crypto evidence rejected replay without explicit {field}=false")


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("crypto evidence timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("crypto evidence timestamp must be timezone-aware")
    return parsed


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"crypto evidence missing {field}")
    return value


def _required_positive_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise ValueError(f"crypto evidence missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"crypto evidence invalid {field}") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"crypto evidence {field} must be positive and finite")
    return parsed
