from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.strategy.crypto_prospective_ranking_calibration import (
    CryptoProspectiveCalibrationDataset,
    CryptoProspectiveCalibrationObservation,
)
from tools.report_bybit_prospective_ranking_calibration import (
    build_prospective_ranking_calibration_report,
)

_OBSERVED = datetime(2026, 8, 24, 12, tzinfo=UTC)


def _observation() -> CryptoProspectiveCalibrationObservation:
    return CryptoProspectiveCalibrationObservation(
        seed_id="1" * 64,
        evidence_rank=1,
        market_rank=2,
        qualification_state="QUALIFIED_POSITIVE_EVIDENCE",
        symbol="BTCUSDT",
        side="LONG",
        signal_available_at=datetime(2026, 8, 20, tzinfo=UTC).isoformat(),
        signal_quality_score=Decimal("1.8"),
        first_touch_state="TARGET_FIRST",
        first_touch_modeled_net_pnl_usdt=Decimal("20"),
        mfe_r=Decimal("1.5"),
        mae_r=Decimal("-0.4"),
        horizon_15_directional_return_fraction=Decimal("0.003"),
        horizon_15_modeled_net_pnl_usdt=Decimal("3"),
        horizon_60_directional_return_fraction=Decimal("0.008"),
        horizon_60_modeled_net_pnl_usdt=Decimal("8"),
        horizon_240_directional_return_fraction=Decimal("0.02"),
        horizon_240_modeled_net_pnl_usdt=Decimal("20"),
    )


class _Reader:
    def __init__(self) -> None:
        self.start = None
        self.limit = None

    def load_dataset(self, *, signal_available_at_or_after=None, maximum_final_seeds=100_000):
        self.start = signal_available_at_or_after
        self.limit = maximum_final_seeds
        return CryptoProspectiveCalibrationDataset(
            raw_final_seed_count=1,
            observations=(_observation(),),
        )


def test_report_passes_rolling_window_and_keeps_safety_boundary() -> None:
    reader = _Reader()
    report = build_prospective_ranking_calibration_report(
        reader,  # type: ignore[arg-type]
        observed_at=_OBSERVED,
        since_days=30,
        maximum_final_seeds=1234,
    )

    assert reader.start == datetime(2026, 7, 25, 12, tzinfo=UTC)
    assert reader.limit == 1234
    assert report["observed_at"] == _OBSERVED.isoformat()
    assert report["window_mode"] == "ROLLING_DAYS"
    assert report["calibration"]["deduplicated_signal_observation_count"] == 1
    assert report["trade_actionable"] is False
    assert report["ranking_weights_changed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["demo_activation_allowed"] is False
    assert report["live_activation_allowed"] is False
    assert report["bybit_live_order_routing_allowed"] is False
