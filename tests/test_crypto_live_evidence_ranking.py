from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.marketdata.bybit_derivatives_history import (
    BybitAccountRatioPoint,
    BybitDerivativesHistory,
    BybitHistoricalFundingPoint,
    BybitOpenInterestPoint,
)
from app.marketdata.bybit_opportunity_registry import build_bybit_opportunity_snapshot
from app.marketdata.bybit_research_universe import (
    BybitResearchInstrument,
    BybitResearchTicker,
    BybitResearchUniversePolicy,
)
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_live_evidence_ranking import (
    build_crypto_live_opportunity_snapshot,
    build_current_derivatives_context,
)
from app.strategy.crypto_perp import CryptoPerpStrategyConfig, evaluate_crypto_signal
from app.strategy.crypto_strategy_evidence_matrix import (
    CryptoStrategyEvidencePolicy,
    classify_crypto_signal_market_regime,
    classify_crypto_stress_regime,
)

_NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
_NOW_MS = int(_NOW.timestamp() * 1000)
_DAY_MS = 86_400_000
_HOUR_MS = 3_600_000


def _instrument(symbol: str, index: int) -> BybitResearchInstrument:
    return BybitResearchInstrument(
        symbol=symbol,
        base_coin=symbol.removesuffix("USDT"),
        quote_coin="USDT",
        settle_coin="USDT",
        contract_type="LinearPerpetual",
        status="Trading",
        symbol_type="innovation",
        launch_time_ms=_NOW_MS - (500 + index) * _DAY_MS,
        delivery_time_ms=0,
        is_pre_listing=False,
    )


def _ticker(symbol: str, index: int) -> BybitResearchTicker:
    return BybitResearchTicker(
        symbol=symbol,
        last_price=Decimal("200") + index,
        bid_price=Decimal("199.95") + index,
        ask_price=Decimal("200.05") + index,
        turnover_24h_usdt=Decimal("500000000") - index * Decimal("10000000"),
        volume_24h=Decimal("1000000"),
        open_interest=Decimal("500000"),
        open_interest_value_usdt=Decimal("100000000") - index * Decimal("1000000"),
        funding_rate=Decimal("0.0001"),
        price_24h_fraction=Decimal("0.02"),
    )


def _market_snapshot():
    symbols = tuple(f"C{index:02d}USDT" for index in range(10))
    return build_bybit_opportunity_snapshot(
        tuple(_instrument(symbol, index) for index, symbol in enumerate(symbols)),
        tuple(_ticker(symbol, index) for index, symbol in enumerate(symbols)),
        observed_at_ms=_NOW_MS,
        host="api.bybit.eu",
        universe_policy=BybitResearchUniversePolicy(
            top_n=10,
            minimum_listing_days=30,
            minimum_turnover_24h_usdt=Decimal("1000000"),
            minimum_open_interest_value_usdt=Decimal("1000000"),
            maximum_spread_bps=Decimal("100"),
            maximum_abs_funding_rate=Decimal("0.01"),
        ),
        registry_limit=10,
    )


def _bars(symbol: str) -> tuple[BybitKlineBar, ...]:
    start = _NOW - timedelta(minutes=5 * 121)
    rows: list[BybitKlineBar] = []
    for index in range(120):
        timestamp = start + timedelta(minutes=5 * index)
        close = Decimal("100") + Decimal(index)
        previous = Decimal("99") + Decimal(index)
        rows.append(
            BybitKlineBar(
                symbol=symbol,
                start_time=timestamp,
                open=previous,
                high=close + Decimal("0.4"),
                low=previous - Decimal("0.4"),
                close=close,
                volume=Decimal("10000"),
                turnover=Decimal("2000000") + Decimal(index * 1000),
            )
        )
    return tuple(rows)


def _derivatives(symbol: str, decision_time: str) -> BybitDerivativesHistory:
    decision_ms = int(datetime.fromisoformat(decision_time).timestamp() * 1000)
    open_interest = (
        BybitOpenInterestPoint(
            symbol=symbol,
            timestamp_ms=decision_ms - 2 * _HOUR_MS,
            open_interest=Decimal("100"),
            single_open_interest=None,
        ),
        BybitOpenInterestPoint(
            symbol=symbol,
            timestamp_ms=decision_ms - _HOUR_MS,
            open_interest=Decimal("102"),
            single_open_interest=None,
        ),
        BybitOpenInterestPoint(
            symbol=symbol,
            timestamp_ms=decision_ms + _HOUR_MS,
            open_interest=Decimal("999"),
            single_open_interest=None,
        ),
    )
    account_ratio = (
        BybitAccountRatioPoint(
            symbol=symbol,
            timestamp_ms=decision_ms - _HOUR_MS,
            buy_ratio=Decimal("0.60"),
            sell_ratio=Decimal("0.40"),
        ),
        BybitAccountRatioPoint(
            symbol=symbol,
            timestamp_ms=decision_ms + _HOUR_MS,
            buy_ratio=Decimal("0.10"),
            sell_ratio=Decimal("0.90"),
        ),
    )
    funding = (
        BybitHistoricalFundingPoint(
            symbol=symbol,
            timestamp_ms=decision_ms - 4 * _HOUR_MS,
            funding_rate=Decimal("0.0001"),
        ),
        BybitHistoricalFundingPoint(
            symbol=symbol,
            timestamp_ms=decision_ms + 4 * _HOUR_MS,
            funding_rate=Decimal("-0.001"),
        ),
    )
    return BybitDerivativesHistory(
        symbol=symbol,
        start_ms=decision_ms - 8 * _HOUR_MS,
        end_ms=decision_ms + 8 * _HOUR_MS,
        interval="1h",
        open_interest=open_interest,
        account_ratio=account_ratio,
        funding=funding,
        request_count=3,
        host="api.bybit.eu",
    )


def _current_state(symbol: str):
    config = CryptoPerpStrategyConfig()
    evaluation = evaluate_crypto_signal(_bars(symbol), config)
    assert evaluation.eligible is True
    assert evaluation.signal is not None
    signal = evaluation.signal
    derivatives_history = _derivatives(symbol, signal.decision_time)
    derivatives = build_current_derivatives_context(
        derivatives_history,
        decision_time=signal.decision_time,
    )
    turnover_reference = Decimal("1000000")
    market_regime, volatility, _trend, _breakout, _turnover = (
        classify_crypto_signal_market_regime(
            signal,
            turnover_reference_usdt=turnover_reference,
            strategy_config=config,
        )
    )
    stress, _score, complete, _reasons = classify_crypto_stress_regime(
        volatility_regime=volatility,
        one_bar_atr_multiple=signal.one_bar_atr_multiple,
        open_interest_delta_fraction=derivatives.open_interest_delta_fraction,
        crowding_regime=derivatives.crowding_regime,
        prior_funding_regime=derivatives.prior_funding_regime,
        decision_context_complete=derivatives.decision_context_complete,
        missing_reasons=derivatives.missing_reasons,
        strategy_config=config,
        policy=CryptoStrategyEvidencePolicy(),
    )
    assert complete is True
    cell_key = "|".join(
        (
            symbol,
            signal.side.value,
            market_regime,
            derivatives.open_interest_regime,
            derivatives.crowding_regime,
            derivatives.prior_funding_regime,
            stress,
        )
    )
    return signal, derivatives_history, cell_key


def _cell(cell_key: str, *, positive: bool) -> dict[str, object]:
    if positive:
        total = 60.0
        average = 10.0
        profit_factor = 2.5
        win_rate = 0.67
    else:
        total = -6.0
        average = -1.0
        profit_factor = 0.8
        win_rate = 0.45
    parts = cell_key.split("|")
    return {
        "cell_key": cell_key,
        "symbol": parts[0],
        "side": parts[1],
        "market_regime": "|".join(parts[2:6]),
        "open_interest_regime": parts[6],
        "crowding_regime": parts[7],
        "prior_funding_regime": parts[8],
        "stress_regime": parts[9],
        "trade_count": 6,
        "sample_sufficient": True,
        "win_count": 4 if positive else 3,
        "loss_count": 2 if positive else 3,
        "win_rate": win_rate,
        "total_net_pnl_usdt": total,
        "average_net_pnl_usdt": average,
        "profit_factor": profit_factor,
        "average_mfe_r": 1.4,
        "average_mae_r": -0.5,
        "maximum_trade_sequence_drawdown_usdt": 12.0,
        "average_turnover_usdt": 2000000.0,
        "average_expected_net_edge_usd": 30.0,
        "average_modeled_round_trip_cost_usdt": 0.5,
        "average_cost_to_expected_edge": 0.02,
        "average_expected_edge_to_risk": 2.0,
    }


def _report(cells: list[dict[str, object]]) -> dict[str, object]:
    return {
        "diagnostic": "BYBIT_CRYPTO_STRATEGY_EVIDENCE_MATRIX",
        "trade_count": 12,
        "cell_count": len(cells),
        "minimum_cell_trades": 5,
        "turnover_reference_usdt": "1000000",
        "stress_policy": {
            "open_interest_impulse_fraction": "0.01",
            "price_shock_atr_threshold": "1.5",
            "high_stress_feature_count": 3,
            "elevated_stress_feature_count": 1,
            "feature_count": 5,
        },
        "matrix": cells,
        "parameter_retuning_performed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def test_current_derivatives_context_never_reads_future_points() -> None:
    signal, history, _cell_key = _current_state("C00USDT")
    context = build_current_derivatives_context(
        history,
        decision_time=signal.decision_time,
    )
    assert context.open_interest == Decimal("102")
    assert context.previous_open_interest == Decimal("100")
    assert context.open_interest_delta_fraction == Decimal("0.02")
    assert context.long_account_ratio == Decimal("0.60")
    assert context.prior_funding_rate == Decimal("0.0001")
    assert context.open_interest_regime == "OI_RISING"
    assert context.crowding_regime == "LONG_HEAVY"
    assert context.prior_funding_regime == "FUNDING_POSITIVE"
    assert context.decision_context_complete is True


def test_evidence_ranking_can_promote_lower_market_rank_with_stronger_exact_cell() -> None:
    market = _market_snapshot()
    signal_top, derivatives_top, key_top = _current_state("C00USDT")
    signal_lower, derivatives_lower, key_lower = _current_state("C05USDT")
    assert signal_top.side == signal_lower.side
    report = _report(
        [
            _cell(key_top, positive=False),
            _cell(key_lower, positive=True),
        ]
    )
    snapshot = build_crypto_live_opportunity_snapshot(
        market,
        bars_by_symbol={
            "C00USDT": _bars("C00USDT"),
            "C05USDT": _bars("C05USDT"),
        },
        derivatives_histories={
            "C00USDT": derivatives_top,
            "C05USDT": derivatives_lower,
        },
        evidence_report=report,
        equity_usdt=Decimal("1000"),
        equity_source="RESEARCH_REFERENCE",
    )

    assert snapshot.opportunities[0].symbol == "C05USDT"
    assert snapshot.opportunities[0].market_rank > snapshot.opportunities[1].market_rank
    assert snapshot.opportunities[0].qualification_state == "QUALIFIED_POSITIVE_EVIDENCE"
    assert snapshot.opportunities[0].positive_historical_evidence is True
    assert snapshot.opportunities[0].evidence_sample_sufficient is True
    assert snapshot.opportunities[1].symbol == "C00USDT"
    assert snapshot.opportunities[1].qualification_state == "QUALIFIED_MIXED_EVIDENCE"
    assert snapshot.qualified_positive_count == 1
    assert snapshot.qualified_mixed_count == 1
    assert snapshot.operator_review_required is True
    assert snapshot.trade_actionable is False
    assert snapshot.strategy_parameters_changed is False
    assert snapshot.strategy_promotion_allowed is False
    assert snapshot.demo_activation_allowed is False
    assert snapshot.live_activation_allowed is False
    assert snapshot.bybit_live_order_routing_allowed is False
    assert snapshot.to_payload()["causal_claim_allowed"] is False
    assert snapshot.to_payload()["predictive_guarantee_allowed"] is False


def test_missing_or_small_exact_cell_never_becomes_qualified_opportunity() -> None:
    market = _market_snapshot()
    _signal, derivatives, cell_key = _current_state("C00USDT")
    small = _cell(cell_key, positive=True)
    small["trade_count"] = 3
    small["sample_sufficient"] = False
    snapshot = build_crypto_live_opportunity_snapshot(
        market,
        bars_by_symbol={"C00USDT": _bars("C00USDT")},
        derivatives_histories={"C00USDT": derivatives},
        evidence_report=_report([small]),
        equity_usdt=Decimal("1000"),
        equity_source="RESEARCH_REFERENCE",
    )
    item = next(value for value in snapshot.opportunities if value.symbol == "C00USDT")
    assert item.qualification_state == "NO_SAMPLE_SUFFICIENT_EXACT_CELL"
    assert item.positive_historical_evidence is False
    assert item.trade_actionable is False


def test_unsafe_evidence_or_custom_strategy_config_fails_closed() -> None:
    market = _market_snapshot()
    _signal, derivatives, cell_key = _current_state("C00USDT")
    report = _report([_cell(cell_key, positive=True)])
    report["strategy_promotion_allowed"] = True
    with pytest.raises(ValueError, match="unsafe evidence flag"):
        build_crypto_live_opportunity_snapshot(
            market,
            bars_by_symbol={"C00USDT": _bars("C00USDT")},
            derivatives_histories={"C00USDT": derivatives},
            evidence_report=report,
            equity_usdt=Decimal("1000"),
            equity_source="RESEARCH_REFERENCE",
        )

    safe_report = _report([_cell(cell_key, positive=True)])
    custom = CryptoPerpStrategyConfig(minimum_signal_quality=Decimal("1.01"))
    with pytest.raises(ValueError, match="qualified fixed strategy config"):
        build_crypto_live_opportunity_snapshot(
            market,
            bars_by_symbol={"C00USDT": _bars("C00USDT")},
            derivatives_histories={"C00USDT": derivatives},
            evidence_report=safe_report,
            equity_usdt=Decimal("1000"),
            equity_source="RESEARCH_REFERENCE",
            strategy_config=custom,
        )
