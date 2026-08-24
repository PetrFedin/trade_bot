from __future__ import annotations

from decimal import Decimal

import pytest

from app.strategy.crypto_prospective_exact_cell_matrix import (
    CryptoProspectiveExactCellDataset,
    CryptoProspectiveExactCellObservation,
    CryptoProspectiveExactCellPolicy,
    CryptoProspectiveSourceEvidenceCell,
    diagnose_crypto_prospective_exact_cell_matrix,
)
from app.strategy.crypto_prospective_liquidation_calibration import (
    CryptoLiquidationCalibrationWindow,
    CryptoProspectiveLiquidationCalibrationObservation,
)
from app.strategy.crypto_prospective_ranking_calibration import (
    CryptoProspectiveCalibrationObservation,
)


def _base(index: int, *, side: str = "LONG", pnl_240: str = "3"):
    return CryptoProspectiveCalibrationObservation(
        seed_id=f"{index:064x}",
        evidence_rank=index,
        market_rank=index,
        qualification_state="QUALIFIED_POSITIVE_EVIDENCE",
        symbol="BTCUSDT",
        side=side,
        signal_available_at=f"2026-08-24T{index:02d}:00:00+00:00",
        signal_quality_score=Decimal("0.9"),
        first_touch_state="TARGET_FIRST" if Decimal(pnl_240) > 0 else "STOP_FIRST",
        first_touch_modeled_net_pnl_usdt=(
            Decimal("2") if Decimal(pnl_240) > 0 else Decimal("-1")
        ),
        mfe_r=Decimal("1.5") if Decimal(pnl_240) > 0 else Decimal("0.5"),
        mae_r=Decimal("-0.4") if Decimal(pnl_240) > 0 else Decimal("-1"),
        horizon_15_directional_return_fraction=Decimal("0.01"),
        horizon_15_modeled_net_pnl_usdt=Decimal("1"),
        horizon_60_directional_return_fraction=Decimal("0.02"),
        horizon_60_modeled_net_pnl_usdt=Decimal("2"),
        horizon_240_directional_return_fraction=Decimal("0.03"),
        horizon_240_modeled_net_pnl_usdt=Decimal(pnl_240),
    )


def _source(*, key: str = "CELL-A", market: str = "BULL"):
    return CryptoProspectiveSourceEvidenceCell(
        evidence_cell_key=key,
        market_regime=market,
        open_interest_regime="OI_RISING",
        crowding_regime="BALANCED",
        prior_funding_regime="FUNDING_POSITIVE",
        stress_regime="STRESS_NORMAL",
        stress_score=1,
        historical_trade_count=40,
        historical_sample_sufficient=True,
        historical_profit_factor=Decimal("1.4"),
        historical_win_rate=Decimal("0.6"),
        historical_total_net_pnl_usdt=Decimal("20"),
        historical_average_net_pnl_usdt=Decimal("0.5"),
        historical_average_mfe_r=Decimal("1.2"),
        historical_average_mae_r=Decimal("-0.6"),
        historical_drawdown_usdt=Decimal("5"),
        positive_historical_evidence=True,
    )


def _liquidation_window(minutes: int):
    return CryptoLiquidationCalibrationWindow(
        window_minutes=minutes,
        event_count=2,
        long_liquidation_count=2,
        short_liquidation_count=0,
        long_estimated_notional_usdt=Decimal("100"),
        short_estimated_notional_usdt=Decimal("0"),
        total_estimated_notional_usdt=Decimal("100"),
        signed_long_minus_short_notional_usdt=Decimal("100"),
        normalized_long_minus_short_imbalance=Decimal("1"),
        largest_event_estimated_notional_usdt=Decimal("60"),
        known_zero=False,
    )


def _observation(index: int, *, pnl_240: str = "3", liquidation: bool = False):
    base = _base(index, pnl_240=pnl_240)
    prospective = CryptoProspectiveLiquidationCalibrationObservation(
        base=base,
        context_state=("COVERAGE_QUALIFIED" if liquidation else "NOT_MATERIALIZED"),
        coverage_reason_codes=(),
        windows=(
            tuple(_liquidation_window(minutes) for minutes in (5, 15, 60))
            if liquidation
            else ()
        ),
    )
    return CryptoProspectiveExactCellObservation(
        prospective=prospective,
        cell_context_state="CELL_COMPLETE",
        cell_unavailable_reason=None,
        source_cell=_source(),
    )


def test_exact_cell_matrix_emits_requested_prospective_performance_metrics() -> None:
    rows = tuple(
        [_observation(index, pnl_240="3") for index in range(1, 4)]
        + [_observation(index, pnl_240="-1") for index in range(4, 6)]
    )
    report = diagnose_crypto_prospective_exact_cell_matrix(
        CryptoProspectiveExactCellDataset(observations=rows),
        policy=CryptoProspectiveExactCellPolicy(minimum_cell_observations=5),
    )

    assert report["observation_count"] == 5
    assert report["cell_complete_count"] == 5
    assert len(report["exact_cell_matrix"]) == 1
    cell = next(iter(report["exact_cell_matrix"].values()))
    assert cell["sample_size"] == 5
    assert cell["sample_sufficient"] is True
    assert cell["average_mfe_r"] == "1.1"
    assert cell["average_mae_r"] == "-0.64"
    assert cell["prospective_horizons"]["240"]["total_pnl_usdt"] == "7"
    assert cell["prospective_horizons"]["240"]["win_rate"] == "0.6"
    assert cell["prospective_horizons"]["240"]["profit_factor"] == "4.5"
    assert cell["prospective_horizons"]["240"]["sequence_drawdown_usdt"] == "2"
    historical = cell["source_historical_reference"]
    assert historical["historical_profit_factor"]["median"] == "1.4"
    assert historical["positive_historical_evidence_rate"] == "1"
    assert report["ranking_weights_changed"] is False
    assert report["trade_actionable"] is False


def test_cell_unavailable_observation_is_retained_but_not_zero_filled() -> None:
    base = _base(1)
    prospective = CryptoProspectiveLiquidationCalibrationObservation(
        base=base,
        context_state="NOT_MATERIALIZED",
        coverage_reason_codes=(),
        windows=(),
    )
    unavailable = CryptoProspectiveExactCellObservation(
        prospective=prospective,
        cell_context_state="CELL_UNAVAILABLE",
        cell_unavailable_reason="SOURCE_EXACT_CELL_INCOMPLETE:evidence_cell_key",
        source_cell=None,
    )
    report = diagnose_crypto_prospective_exact_cell_matrix(
        CryptoProspectiveExactCellDataset(observations=(unavailable,))
    )

    assert report["observation_count"] == 1
    assert report["cell_complete_count"] == 0
    assert report["cell_unavailable_count"] == 1
    assert report["exact_cell_matrix"] == {}
    assert report["cell_unavailable_reason_counts"] == {
        "SOURCE_EXACT_CELL_INCOMPLETE:evidence_cell_key": 1
    }


def test_evidence_cell_key_cannot_drift_to_different_regime_semantics() -> None:
    first = _observation(1)
    second = CryptoProspectiveExactCellObservation(
        prospective=_observation(2).prospective,
        cell_context_state="CELL_COMPLETE",
        cell_unavailable_reason=None,
        source_cell=_source(key="CELL-A", market="BEAR"),
    )

    with pytest.raises(ValueError, match="divergent regime semantics"):
        CryptoProspectiveExactCellDataset(observations=(first, second)).validate()


def test_liquidation_augmented_matrix_requires_qualified_presignal_context() -> None:
    qualified = _observation(1, liquidation=True)
    missing = _observation(2, liquidation=False)
    report = diagnose_crypto_prospective_exact_cell_matrix(
        CryptoProspectiveExactCellDataset(observations=(qualified, missing)),
        policy=CryptoProspectiveExactCellPolicy(minimum_cell_observations=5),
    )

    augmented = report["liquidation_augmented_exact_cell_matrix_15m"]
    assert len(augmented) == 1
    label = next(iter(augmented))
    assert "LIQ15=SAME_SIDE_LIQUIDATIONS_DOMINANT" in label
    assert augmented[label]["sample_size"] == 1
