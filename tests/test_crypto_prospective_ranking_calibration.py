from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.strategy.crypto_prospective_ranking_calibration import (
    CryptoProspectiveCalibrationDataset,
    CryptoProspectiveCalibrationObservation,
    CryptoProspectiveCalibrationPolicy,
    diagnose_crypto_prospective_ranking_calibration,
)

_START = datetime(2026, 8, 1, tzinfo=UTC)


def _observation(
    index: int,
    *,
    state: str,
    evidence_rank: int,
    pnl: Decimal,
    touch: str,
) -> CryptoProspectiveCalibrationObservation:
    touch_pnl = pnl if touch in {"TARGET_FIRST", "STOP_FIRST"} else None
    sign = Decimal("0.01") if pnl > 0 else Decimal("-0.01")
    return CryptoProspectiveCalibrationObservation(
        seed_id=f"{index + 1:064x}",
        evidence_rank=evidence_rank,
        market_rank=min(index + 1, 50),
        qualification_state=state,
        symbol=f"C{index:02d}USDT",
        side="LONG" if index % 2 == 0 else "SHORT",
        signal_available_at=(_START + index * timedelta(hours=1)).isoformat(),
        signal_quality_score=Decimal("1.5") + Decimal(index) / Decimal("100"),
        first_touch_state=touch,
        first_touch_modeled_net_pnl_usdt=touch_pnl,
        mfe_r=Decimal("1.4") if pnl > 0 else Decimal("0.6"),
        mae_r=Decimal("-0.4") if pnl > 0 else Decimal("-1.1"),
        horizon_15_directional_return_fraction=sign / Decimal("2"),
        horizon_15_modeled_net_pnl_usdt=pnl / Decimal("2"),
        horizon_60_directional_return_fraction=sign,
        horizon_60_modeled_net_pnl_usdt=pnl,
        horizon_240_directional_return_fraction=sign * Decimal("1.5"),
        horizon_240_modeled_net_pnl_usdt=pnl * Decimal("1.5"),
    )


def test_positive_evidence_is_compared_prospectively_against_controls() -> None:
    observations = (
        _observation(
            0,
            state="QUALIFIED_POSITIVE_EVIDENCE",
            evidence_rank=1,
            pnl=Decimal("12"),
            touch="TARGET_FIRST",
        ),
        _observation(
            1,
            state="QUALIFIED_POSITIVE_EVIDENCE",
            evidence_rank=2,
            pnl=Decimal("8"),
            touch="TARGET_FIRST",
        ),
        _observation(
            2,
            state="QUALIFIED_MIXED_EVIDENCE",
            evidence_rank=8,
            pnl=Decimal("-5"),
            touch="STOP_FIRST",
        ),
        _observation(
            3,
            state="QUALIFIED_MIXED_EVIDENCE",
            evidence_rank=9,
            pnl=Decimal("-3"),
            touch="STOP_FIRST",
        ),
    )
    dataset = CryptoProspectiveCalibrationDataset(
        raw_final_seed_count=5,
        observations=observations,
    )
    report = diagnose_crypto_prospective_ranking_calibration(
        dataset,
        policy=CryptoProspectiveCalibrationPolicy(
            minimum_group_observations=2,
            minimum_comparison_observations=2,
        ),
    )

    assert report["deduplicated_signal_observation_count"] == 4
    assert report["duplicate_signal_observation_count"] == 1
    assert report["overall"]["observation_count"] == 4
    assert report["by_qualification_state"]["QUALIFIED_POSITIVE_EVIDENCE"][
        "sample_sufficient"
    ] is True
    comparison = report["positive_evidence_vs_controls"]["QUALIFIED_MIXED_EVIDENCE"]
    assert comparison["comparison_sample_sufficient"] is True
    assert Decimal(
        comparison["horizon_deltas"]["240"]["average_modeled_net_pnl_usdt_delta"]
    ) > 0
    assert Decimal(
        comparison["horizon_deltas"]["240"]["positive_net_pnl_rate_delta"]
    ) > 0
    assert report["ranking_weights_changed"] is False
    assert report["strategy_promotion_allowed"] is False
    assert report["trade_actionable"] is False
    assert report["bybit_live_order_routing_allowed"] is False


def test_ambiguous_same_bar_is_not_counted_as_ordered_target_or_stop() -> None:
    observations = (
        _observation(
            0,
            state="QUALIFIED_POSITIVE_EVIDENCE",
            evidence_rank=1,
            pnl=Decimal("10"),
            touch="TARGET_FIRST",
        ),
        _observation(
            1,
            state="QUALIFIED_POSITIVE_EVIDENCE",
            evidence_rank=2,
            pnl=Decimal("-8"),
            touch="STOP_FIRST",
        ),
        _observation(
            2,
            state="QUALIFIED_POSITIVE_EVIDENCE",
            evidence_rank=3,
            pnl=Decimal("2"),
            touch="AMBIGUOUS_SAME_BAR",
        ),
    )
    report = diagnose_crypto_prospective_ranking_calibration(
        CryptoProspectiveCalibrationDataset(
            raw_final_seed_count=3,
            observations=observations,
        ),
        policy=CryptoProspectiveCalibrationPolicy(
            minimum_group_observations=2,
            minimum_comparison_observations=2,
        ),
    )
    positive = report["by_qualification_state"]["QUALIFIED_POSITIVE_EVIDENCE"]
    assert positive["ordered_touch_count"] == 2
    assert positive["ambiguous_same_bar_count"] == 1
    assert Decimal(positive["target_first_rate_of_ordered_touches"]) == Decimal("0.5")
    assert Decimal(positive["ambiguous_same_bar_rate"]) == Decimal("1") / Decimal("3")


def test_rank_buckets_are_mutually_exclusive_and_preserve_all_observations() -> None:
    ranks = (1, 2, 4, 6, 11, 21)
    observations = tuple(
        _observation(
            index,
            state="QUALIFIED_POSITIVE_EVIDENCE",
            evidence_rank=rank,
            pnl=Decimal("1"),
            touch="NEITHER",
        )
        for index, rank in enumerate(ranks)
    )
    report = diagnose_crypto_prospective_ranking_calibration(
        CryptoProspectiveCalibrationDataset(
            raw_final_seed_count=len(observations),
            observations=observations,
        )
    )
    buckets = report["by_evidence_rank_bucket"]
    assert set(buckets) == {
        "RANK_01",
        "RANK_02_03",
        "RANK_04_05",
        "RANK_06_10",
        "RANK_11_20",
        "RANK_21_50",
    }
    assert sum(item["observation_count"] for item in buckets.values()) == len(observations)
