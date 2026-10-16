"""Negative and consistency tests for cross-PR research source linkage."""
import copy
import hashlib

import pytest

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tools.summarize_mark_price_hourly_sensitivity import SIX
from tools.verify_mark_funding_replay_linkage import (
    MarkFundingLinkageError,
    _validate_funded_replay,
)

START = "2023-10-09T00:00:00+00:00"
END_DAY = "2026-06-14T00:00:00+00:00"
END_HOUR = "2026-06-14T23:00:00+00:00"
COSTS = (("8", "5"), ("10", "10"))
MODELS = {
    f"{a}:{b}": {} for a in ("DAILY_STOP_FIRST", "HOURLY_COLLISION_REFINED")
    for b in ("NO_FUNDING", "UPPER_COST", "LOWER_COST")
}


def _case():
    mark_summary = {"start": START, "end": END_HOUR}
    manifests = {}
    hashes = {}
    for symbol in SIX:
        manifests[symbol] = {
            "funding_source_sha256": hashlib.sha256(symbol.encode()).hexdigest(),
            "funding_manifest_sha256": hashlib.sha256((symbol + "m").encode()).hexdigest(),
        }
        hashes[f"funding/{symbol}_funding.csv"] = manifests[symbol][
            "funding_source_sha256"
        ]
        hashes[f"funding/{symbol}_funding.manifest.json"] = manifests[symbol][
            "funding_manifest_sha256"
        ]
    comparisons = []
    for period in ("six_full_history", "six_matched_window", "broad21_matched_window"):
        for fees, slip in COSTS:
            comparisons.append({
                "period_id": period, "fee_bps_per_fill": fees,
                "slippage_bps_per_side": slip,
                "symbols": list(SIX) if period != "broad21_matched_window"
                else list(SIX) + [f"TEST{i}" for i in range(15)],
                "first_source_day_utc": START,
                "last_source_day_utc": END_DAY,
                "models": copy.deepcopy(MODELS),
            })
    report = {
        "schema_version": "astra-daily-funding-cash-feedback-v1",
        "evaluation_class": "REUSED_HISTORY_DAILY_CASH_FEEDBACK_NOT_VENUE_SETTLEMENT",
        "research_only": True,
        "funding_cash_feedback_modelled": True,
        "mark_price_used": False,
        "notional_model": "CONSTANT_ENTRY_EXECUTION_NOTIONAL",
        "untouched_holdout": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "funding_exact_exchange_timing": False,
        "margin_or_liquidation_modelled": False,
        "source_daily_and_funding_sha256": hashes,
        "comparisons": comparisons,
    }
    report["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes(report)
    ).hexdigest()
    verifier = {
        "arithmetic_verification": "PASS", "configurations": 6,
        "model_replays": 36, "matched_dates_verified": True,
        "evidence_sha256": report["evidence_sha256"],
        "execution_or_venue_account_truth_verified": False,
        "independent_human_or_agent_review": False,
        "live_trading_allowed": False,
    }
    return report, verifier, mark_summary, manifests


def _check(case):
    return _validate_funded_replay(*case)


def test_coherent_cross_pr_research_identity():
    report, verifier, summary, manifests = _case()
    assert _check((report, verifier, summary, manifests)) == report["evidence_sha256"]


def test_mixed_funding_source_hash_fails_even_with_rehashed_report():
    report, verifier, summary, manifests = _case()
    report["source_daily_and_funding_sha256"]["funding/BTCUSDT_funding.csv"] = "0" * 64
    report["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes({k: v for k, v in report.items() if k != "evidence_sha256"})
    ).hexdigest()
    verifier["evidence_sha256"] = report["evidence_sha256"]
    with pytest.raises(MarkFundingLinkageError, match="funding archive SHA"):
        _check((report, verifier, summary, manifests))


def test_mismatched_utc_window_fails():
    report, verifier, summary, manifests = _case()
    summary["end"] = "2026-06-15T23:00:00+00:00"
    with pytest.raises(MarkFundingLinkageError, match="windows differ"):
        _check((report, verifier, summary, manifests))


def test_missing_negative_funding_scenario_fails():
    report, verifier, summary, manifests = _case()
    del report["comparisons"][2]["models"]["HOURLY_COLLISION_REFINED:LOWER_COST"]
    report["evidence_sha256"] = hashlib.sha256(
        canonical_json_bytes({k: v for k, v in report.items() if k != "evidence_sha256"})
    ).hexdigest()
    verifier["evidence_sha256"] = report["evidence_sha256"]
    with pytest.raises(MarkFundingLinkageError, match="missing signed funding"):
        _check((report, verifier, summary, manifests))


def test_fake_account_settlement_verification_rejected():
    report, verifier, summary, manifests = _case()
    verifier["execution_or_venue_account_truth_verified"] = True
    with pytest.raises(MarkFundingLinkageError, match="verification receipt mismatch"):
        _check((report, verifier, summary, manifests))


def test_unrehashed_report_tampering_rejected():
    report, verifier, summary, manifests = _case()
    report["strategy_promotion_allowed"] = True
    with pytest.raises(MarkFundingLinkageError, match="canonical SHA"):
        _check((report, verifier, summary, manifests))
