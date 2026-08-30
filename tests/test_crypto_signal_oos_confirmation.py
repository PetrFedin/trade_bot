from __future__ import annotations

from decimal import Decimal

from app.strategy.crypto_prospective_exact_cell_matrix import (
    CryptoProspectiveExactCellDataset,
    CryptoProspectiveExactCellObservation,
    CryptoProspectiveSourceEvidenceCell,
)
from app.strategy.crypto_prospective_liquidation_calibration import (
    CryptoProspectiveLiquidationCalibrationObservation,
)
from app.strategy.crypto_prospective_ranking_calibration import (
    CryptoProspectiveCalibrationObservation,
)
from app.strategy.crypto_signal_oos_confirmation import (
    CryptoHistoricalPerfectEvidenceCell,
    CryptoHistoricalPerfectEvidenceSnapshot,
    CryptoSignalOosConfirmationPolicy,
    confirm_crypto_historical_perfect_cells_oos,
)

_SNAPSHOT_ID = "a" * 64
_CUTOFF = "2026-08-01T00:00:00+00:00"


def _candidate() -> CryptoHistoricalPerfectEvidenceCell:
    return CryptoHistoricalPerfectEvidenceCell(
        evidence_snapshot_id=_SNAPSHOT_ID,
        evidence_snapshot_observed_at=_CUTOFF,
        cell_key="BTCUSDT|LONG|REGIME|OI_RISING|BALANCED|FUNDING_POSITIVE|STRESS_CALM",
        symbol="BTCUSDT",
        side="LONG",
        market_regime="REGIME",
        open_interest_regime="OI_RISING",
        crowding_regime="BALANCED",
        prior_funding_regime="FUNDING_POSITIVE",
        stress_regime="STRESS_CALM",
        historical_trade_count=12,
        historical_win_rate=Decimal("1"),
        historical_total_net_pnl_usdt=Decimal("240"),
        historical_average_net_pnl_usdt=Decimal("20"),
        historical_profit_factor=None,
        historical_average_mfe_r=Decimal("2.1"),
        historical_average_mae_r=Decimal("0.4"),
    )


def _snapshot() -> CryptoHistoricalPerfectEvidenceSnapshot:
    return CryptoHistoricalPerfectEvidenceSnapshot(
        evidence_snapshot_id=_SNAPSHOT_ID,
        observed_at=_CUTOFF,
        minimum_cell_trades=5,
        candidates=(_candidate(),),
    )


def _observation(
    index: int,
    *,
    first_touch_state: str = "TARGET_FIRST",
    pnl_240: str = "20",
    signal_available_at: str | None = None,
    cell_key: str | None = None,
) -> CryptoProspectiveExactCellObservation:
    if signal_available_at is None:
        signal_available_at = f"2026-08-{index + 2:02d}T00:05:00+00:00"
    ordered_pnl = Decimal("20") if first_touch_state == "TARGET_FIRST" else Decimal("-10")
    base = CryptoProspectiveCalibrationObservation(
        seed_id=f"{index + 1:064x}",
        evidence_rank=1,
        market_rank=1,
        qualification_state="QUALIFIED_POSITIVE_EVIDENCE",
        symbol="BTCUSDT",
        side="LONG",
        signal_available_at=signal_available_at,
        signal_quality_score=Decimal("2.0"),
        first_touch_state=first_touch_state,
        first_touch_modeled_net_pnl_usdt=ordered_pnl,
        mfe_r=Decimal("2"),
        mae_r=Decimal("-0.5"),
        horizon_15_directional_return_fraction=Decimal("0.01"),
        horizon_15_modeled_net_pnl_usdt=Decimal("5"),
        horizon_60_directional_return_fraction=Decimal("0.02"),
        horizon_60_modeled_net_pnl_usdt=Decimal("10"),
        horizon_240_directional_return_fraction=Decimal("0.03"),
        horizon_240_modeled_net_pnl_usdt=Decimal(pnl_240),
    )
    prospective = CryptoProspectiveLiquidationCalibrationObservation(
        base=base,
        context_state="NOT_MATERIALIZED",
        coverage_reason_codes=(),
        windows=(),
    )
    source = CryptoProspectiveSourceEvidenceCell(
        evidence_cell_key=(
            _candidate().cell_key if cell_key is None else cell_key
        ),
        market_regime="REGIME",
        open_interest_regime="OI_RISING",
        crowding_regime="BALANCED",
        prior_funding_regime="FUNDING_POSITIVE",
        stress_regime="STRESS_CALM",
        stress_score=0,
        historical_trade_count=12,
        historical_sample_sufficient=True,
        historical_profit_factor=None,
        historical_win_rate=Decimal("1"),
        historical_total_net_pnl_usdt=Decimal("240"),
        historical_average_net_pnl_usdt=Decimal("20"),
        historical_average_mfe_r=Decimal("2.1"),
        historical_average_mae_r=Decimal("0.4"),
        historical_drawdown_usdt=Decimal("0"),
        positive_historical_evidence=True,
    )
    return CryptoProspectiveExactCellObservation(
        prospective=prospective,
        cell_context_state="CELL_COMPLETE",
        cell_unavailable_reason=None,
        source_cell=source,
    )


def _confirm(observations: tuple[CryptoProspectiveExactCellObservation, ...]):
    return confirm_crypto_historical_perfect_cells_oos(
        _snapshot(),
        CryptoProspectiveExactCellDataset(observations=observations),
        policy=CryptoSignalOosConfirmationPolicy(
            minimum_historical_trades=5,
            minimum_oos_observations=5,
        ),
    )


def test_perfect_cell_without_future_observation_is_not_observed() -> None:
    report = _confirm(())
    row = report["candidates"][0]
    assert row["oos_status"] == "OOS_NOT_OBSERVED"
    assert row["oos_observation_count"] == 0
    assert report["strategy_promotion_allowed"] is False
    assert report["trade_actionable"] is False


def test_perfect_cell_with_only_successes_below_minimum_is_insufficient() -> None:
    report = _confirm(tuple(_observation(index) for index in range(4)))
    row = report["candidates"][0]
    assert row["oos_status"] == "OOS_INSUFFICIENT"
    assert row["oos_success_count"] == 4
    assert row["perfect_hypothesis_survived_oos"] is True
    assert row["sample_sufficient"] is False


def test_perfect_cell_is_confirmed_only_after_predeclared_minimum() -> None:
    report = _confirm(tuple(_observation(index) for index in range(5)))
    row = report["candidates"][0]
    assert row["oos_status"] == "OOS_CONFIRMED"
    assert row["oos_success_rate"] == 1.0
    assert row["oos_success_wilson_lower_95"] < 1.0
    assert row["sample_sufficient"] is True


def test_one_stop_first_observation_rejects_perfect_hypothesis() -> None:
    rows = tuple(_observation(index) for index in range(4)) + (
        _observation(4, first_touch_state="STOP_FIRST", pnl_240="-12"),
    )
    report = _confirm(rows)
    row = report["candidates"][0]
    assert row["oos_status"] == "OOS_REJECTED"
    assert row["oos_failure_count"] == 1
    assert row["first_failure"]["first_touch_state"] == "STOP_FIRST"
    assert row["perfect_hypothesis_survived_oos"] is False


def test_target_first_with_non_positive_240m_pnl_rejects_perfect_hypothesis() -> None:
    report = _confirm((_observation(0, pnl_240="0"),))
    row = report["candidates"][0]
    assert row["oos_status"] == "OOS_REJECTED"
    assert row["non_positive_240m_count"] == 1


def test_pre_snapshot_and_wrong_cell_observations_do_not_contaminate_oos() -> None:
    rows = (
        _observation(0, signal_available_at="2026-07-31T23:55:00+00:00"),
        _observation(1, cell_key="SOME_OTHER_CELL"),
    )
    report = _confirm(rows)
    row = report["candidates"][0]
    assert row["oos_status"] == "OOS_NOT_OBSERVED"
    assert row["oos_observation_count"] == 0
