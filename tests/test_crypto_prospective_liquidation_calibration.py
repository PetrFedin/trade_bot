from __future__ import annotations

from decimal import Decimal

from app.strategy.crypto_prospective_liquidation_calibration import (
    CryptoLiquidationCalibrationPolicy,
    CryptoLiquidationCalibrationWindow,
    CryptoProspectiveLiquidationCalibrationDataset,
    CryptoProspectiveLiquidationCalibrationObservation,
    diagnose_crypto_prospective_liquidation_calibration,
)
from app.strategy.crypto_prospective_ranking_calibration import (
    CryptoProspectiveCalibrationDataset,
    CryptoProspectiveCalibrationObservation,
)


def _base(
    index: int,
    *,
    side: str,
    pnl_15: str,
    pnl_60: str,
    pnl_240: str,
    first_touch: str = "TARGET_FIRST",
) -> CryptoProspectiveCalibrationObservation:
    first_touch_pnl = Decimal("2") if first_touch == "TARGET_FIRST" else Decimal("-1")
    return CryptoProspectiveCalibrationObservation(
        seed_id=f"{index:064x}",
        evidence_rank=1,
        market_rank=1,
        qualification_state="QUALIFIED_POSITIVE_EVIDENCE",
        symbol="BTCUSDT" if index % 2 else "ETHUSDT",
        side=side,
        signal_available_at=f"2026-08-24T{index % 24:02d}:00:00+00:00",
        signal_quality_score=Decimal("0.9"),
        first_touch_state=first_touch,
        first_touch_modeled_net_pnl_usdt=first_touch_pnl,
        mfe_r=Decimal("1.5") if first_touch == "TARGET_FIRST" else Decimal("0.5"),
        mae_r=Decimal("-0.4") if first_touch == "TARGET_FIRST" else Decimal("-1"),
        horizon_15_directional_return_fraction=Decimal("0.01"),
        horizon_15_modeled_net_pnl_usdt=Decimal(pnl_15),
        horizon_60_directional_return_fraction=Decimal("0.02"),
        horizon_60_modeled_net_pnl_usdt=Decimal(pnl_60),
        horizon_240_directional_return_fraction=Decimal("0.03"),
        horizon_240_modeled_net_pnl_usdt=Decimal(pnl_240),
    )


def _window(
    minutes: int,
    *,
    signed: str,
    total: str = "100",
    known_zero: bool = False,
) -> CryptoLiquidationCalibrationWindow:
    if known_zero:
        return CryptoLiquidationCalibrationWindow(
            window_minutes=minutes,
            event_count=0,
            long_liquidation_count=0,
            short_liquidation_count=0,
            long_estimated_notional_usdt=Decimal("0"),
            short_estimated_notional_usdt=Decimal("0"),
            total_estimated_notional_usdt=Decimal("0"),
            signed_long_minus_short_notional_usdt=Decimal("0"),
            normalized_long_minus_short_imbalance=Decimal("0"),
            largest_event_estimated_notional_usdt=Decimal("0"),
            known_zero=True,
        )
    signed_value = Decimal(signed)
    total_value = Decimal(total)
    long_value = (total_value + signed_value) / Decimal("2")
    short_value = total_value - long_value
    return CryptoLiquidationCalibrationWindow(
        window_minutes=minutes,
        event_count=2,
        long_liquidation_count=1,
        short_liquidation_count=1,
        long_estimated_notional_usdt=long_value,
        short_estimated_notional_usdt=short_value,
        total_estimated_notional_usdt=total_value,
        signed_long_minus_short_notional_usdt=signed_value,
        normalized_long_minus_short_imbalance=signed_value / total_value,
        largest_event_estimated_notional_usdt=max(long_value, short_value),
        known_zero=False,
    )


def _qualified(
    base: CryptoProspectiveCalibrationObservation,
    *,
    signed: str,
) -> CryptoProspectiveLiquidationCalibrationObservation:
    return CryptoProspectiveLiquidationCalibrationObservation(
        base=base,
        context_state="COVERAGE_QUALIFIED",
        coverage_reason_codes=(),
        windows=tuple(_window(minutes, signed=signed) for minutes in (5, 15, 60)),
    )


def test_relative_pressure_is_side_aware_without_fitted_threshold() -> None:
    positive = _window(15, signed="40")
    negative = _window(15, signed="-40")
    zero = _window(15, signed="0", known_zero=True)

    assert positive.absolute_pressure == "LONG_LIQUIDATIONS_DOMINANT"
    assert positive.relative_pressure("LONG") == "SAME_SIDE_LIQUIDATIONS_DOMINANT"
    assert positive.relative_pressure("SHORT") == "OPPOSITE_SIDE_LIQUIDATIONS_DOMINANT"
    assert negative.relative_pressure("SHORT") == "SAME_SIDE_LIQUIDATIONS_DOMINANT"
    assert zero.relative_pressure("LONG") == "KNOWN_ZERO"


def test_diagnostic_retains_unavailable_context_in_dataset_accounting() -> None:
    bases = (
        _base(1, side="LONG", pnl_15="1", pnl_60="2", pnl_240="3"),
        _base(2, side="SHORT", pnl_15="-1", pnl_60="-2", pnl_240="-3", first_touch="STOP_FIRST"),
        _base(3, side="LONG", pnl_15="1", pnl_60="1", pnl_240="1"),
    )
    base_dataset = CryptoProspectiveCalibrationDataset(
        raw_final_seed_count=3,
        observations=bases,
    )
    rows = (
        _qualified(bases[0], signed="50"),
        CryptoProspectiveLiquidationCalibrationObservation(
            base=bases[1],
            context_state="COVERAGE_UNQUALIFIED",
            coverage_reason_codes=("STATUS_GAP_IN_WINDOW",),
            windows=(),
        ),
        CryptoProspectiveLiquidationCalibrationObservation(
            base=bases[2],
            context_state="NOT_MATERIALIZED",
            coverage_reason_codes=(),
            windows=(),
        ),
    )
    report = diagnose_crypto_prospective_liquidation_calibration(
        CryptoProspectiveLiquidationCalibrationDataset(
            base_dataset=base_dataset,
            observations=rows,
        )
    )

    assert report["base_deduplicated_signal_count"] == 3
    assert report["coverage_qualified_count"] == 1
    assert report["coverage_unavailable_count"] == 2
    assert report["coverage_reason_counts"] == {"STATUS_GAP_IN_WINDOW": 1}
    assert report["ranking_weights_changed"] is False
    assert report["trade_actionable"] is False


def test_report_emits_pnl_pf_win_mfe_mae_drawdown_and_sample_size() -> None:
    bases: list[CryptoProspectiveCalibrationObservation] = []
    rows: list[CryptoProspectiveLiquidationCalibrationObservation] = []
    for index in range(1, 6):
        base = _base(
            index,
            side="LONG",
            pnl_15="2",
            pnl_60="3",
            pnl_240="4",
        )
        bases.append(base)
        rows.append(_qualified(base, signed="40"))
    for index in range(6, 11):
        base = _base(
            index,
            side="LONG",
            pnl_15="-1",
            pnl_60="-2",
            pnl_240="-3",
            first_touch="STOP_FIRST",
        )
        bases.append(base)
        rows.append(_qualified(base, signed="-40"))
    dataset = CryptoProspectiveLiquidationCalibrationDataset(
        base_dataset=CryptoProspectiveCalibrationDataset(
            raw_final_seed_count=10,
            observations=tuple(bases),
        ),
        observations=tuple(rows),
    )
    report = diagnose_crypto_prospective_liquidation_calibration(
        dataset,
        policy=CryptoLiquidationCalibrationPolicy(
            minimum_group_observations=5,
            minimum_comparison_observations=5,
        ),
    )

    groups = report["by_window_relative_pressure"]["15"]
    same = groups["SAME_SIDE_LIQUIDATIONS_DOMINANT"]
    opposite = groups["OPPOSITE_SIDE_LIQUIDATIONS_DOMINANT"]
    assert same["observation_count"] == 5
    assert same["sample_sufficient"] is True
    assert same["horizons"]["240"]["win_rate"] == "1"
    assert same["horizons"]["240"]["pnl"]["profit_factor"] is None
    assert same["horizons"]["240"]["chronological_sequence_drawdown_usdt"] == "0"
    assert opposite["horizons"]["240"]["win_rate"] == "0"
    assert opposite["horizons"]["240"]["chronological_sequence_drawdown_usdt"] == "15"
    assert same["average_mfe_r"] == "1.5"
    assert opposite["average_mae_r"] == "-1"
    comparison = report["relative_pressure_comparisons"]["15"][
        "same_side_vs_opposite_side"
    ]
    assert comparison["comparison_sample_sufficient"] is True
    assert comparison["horizon_deltas"]["240"]["average_pnl_usdt_delta"] == "7"
