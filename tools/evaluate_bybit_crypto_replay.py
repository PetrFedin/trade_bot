from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.strategy.crypto_evidence import CryptoReplayEvidence, evaluate_crypto_replay_evidence


def evaluate_report(report: dict[str, Any]) -> dict[str, Any]:
    duration_days, observation_window_basis = _observed_days(report)
    opening_equity = Decimal(str(report["opening_equity_usdt"]))
    variants = _score_variants(
        report["variants"],
        opening_equity=opening_equity,
        duration_days=duration_days,
    )
    notional_shadow_candidates: dict[str, Any] = {}
    raw_candidates = report.get("notional_cap_shadow_candidates", {})
    if isinstance(raw_candidates, dict):
        for candidate_name, candidate in raw_candidates.items():
            if not isinstance(candidate, dict):
                continue
            candidate_variants = candidate.get("variants", {})
            if not isinstance(candidate_variants, dict):
                continue
            notional_shadow_candidates[candidate_name] = {
                "maximum_notional_to_equity": candidate.get("maximum_notional_to_equity"),
                "risk_fraction_per_trade": candidate.get("risk_fraction_per_trade"),
                "purpose": candidate.get("purpose"),
                "strategy_promotion_allowed": False,
                "demo_order_writes_allowed": False,
                "live_promotion_allowed": False,
                "variants": _score_variants(
                    candidate_variants,
                    opening_equity=opening_equity,
                    duration_days=duration_days,
                ),
            }

    strategy_shadow_candidates: dict[str, Any] = {}
    raw_strategy_candidates = report.get("strategy_shadow_candidates", {})
    if isinstance(raw_strategy_candidates, dict):
        for candidate_name, candidate in raw_strategy_candidates.items():
            if not isinstance(candidate, dict) or not isinstance(candidate.get("metrics"), dict):
                continue
            target = candidate.get(
                "minimum_entry_net_profit_usd",
                candidate.get("target_net_profit_usd"),
            )
            if target is None:
                continue
            scored = _score_payload(
                candidate,
                target_net_profit_usd=Decimal(str(target)),
                opening_equity=opening_equity,
                duration_days=duration_days,
            )
            strategy_shadow_candidates[candidate_name] = {
                **scored,
                "mode": candidate.get("mode"),
                "runner_activation_net_profit_usd": candidate.get(
                    "runner_activation_net_profit_usd"
                ),
                "runner_initial_protected_net_profit_usd": candidate.get(
                    "runner_initial_protected_net_profit_usd"
                ),
                "profit_cap_net_profit_usd": candidate.get("profit_cap_net_profit_usd"),
                "fixed_take_profit_enabled": candidate.get("fixed_take_profit_enabled"),
                "strategy_promotion_allowed": False,
                "demo_order_writes_allowed": False,
                "live_promotion_allowed": False,
            }

    return {
        "qualification": "CRYPTO_REPLAY_EVIDENCE_SCORED",
        "source_qualification": report["qualification"],
        "source": report["source"],
        "observed_days": float(duration_days),
        "observation_window_basis": observation_window_basis,
        "strategy_promotion_allowed": False,
        "live_promotion_allowed": False,
        "variants": variants,
        "shadow_candidates": notional_shadow_candidates,
        "strategy_shadow_candidates": strategy_shadow_candidates,
    }


def _observed_days(report: dict[str, Any]) -> tuple[Decimal, str]:
    archive_dates = report.get("archive_dates")
    if report.get("archive_completed_utc_days_only") is True and isinstance(
        archive_dates, list
    ):
        if not archive_dates:
            raise ValueError("completed archive evidence must include archive_dates")
        parsed_dates = [date.fromisoformat(str(value)) for value in archive_dates]
        if parsed_dates != sorted(parsed_dates) or len(set(parsed_dates)) != len(parsed_dates):
            raise ValueError("completed archive dates must be unique and chronological")
        return Decimal(len(parsed_dates)), "COMPLETED_UTC_ARCHIVE_DATES"

    first = datetime.fromisoformat(str(report["first_completed_bar"]))
    last = datetime.fromisoformat(str(report["last_completed_bar"]))
    duration_days = Decimal(str(max((last - first).total_seconds(), 1.0))) / Decimal("86400")
    return duration_days, "FIRST_TO_LAST_COMPLETED_BAR_ELAPSED_TIME"


def _score_variants(
    variants: dict[str, Any],
    *,
    opening_equity: Decimal,
    duration_days: Decimal,
) -> dict[str, Any]:
    scored: dict[str, Any] = {}
    for name, payload in variants.items():
        scored[name] = _score_payload(
            payload,
            target_net_profit_usd=Decimal(str(payload["target_net_profit_usd"])),
            opening_equity=opening_equity,
            duration_days=duration_days,
        )
    return scored


def _score_payload(
    payload: dict[str, Any],
    *,
    target_net_profit_usd: Decimal,
    opening_equity: Decimal,
    duration_days: Decimal,
) -> dict[str, Any]:
    metrics = payload["metrics"]
    profit_factor_raw = metrics["profit_factor"]
    evidence = CryptoReplayEvidence(
        target_net_profit_usd=target_net_profit_usd,
        opening_equity_usdt=opening_equity,
        closed_trade_count=int(metrics["closed_trade_count"]),
        accepted_trade_plan_event_count=int(payload["accepted_trade_plan_event_count"]),
        total_net_pnl_usdt=Decimal(str(metrics["total_net_pnl_usdt"])),
        profit_factor=(
            None if profit_factor_raw is None else Decimal(str(profit_factor_raw))
        ),
        maximum_drawdown_pct=Decimal(str(metrics["maximum_drawdown_pct"])),
        fees_usdt=Decimal(str(metrics["fees_usdt"])),
        risk_budget_breach_count=int(metrics["risk_budget_breach_count"]),
        observed_days=duration_days,
    )
    decision = evaluate_crypto_replay_evidence(evidence)
    return {
        "posture": decision.posture.value,
        "reasons": list(decision.reasons),
        "demo_observation_allowed": decision.demo_observation_allowed,
        "live_promotion_allowed": decision.live_promotion_allowed,
        "closed_trade_count": evidence.closed_trade_count,
        "accepted_trade_plan_event_count": evidence.accepted_trade_plan_event_count,
        "observed_days": float(evidence.observed_days),
        "total_net_pnl_usdt": float(evidence.total_net_pnl_usdt),
        "maximum_drawdown_pct": float(evidence.maximum_drawdown_pct),
        "fees_usdt": float(evidence.fees_usdt),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Score Bybit historical replay evidence")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = json.loads(args.input.read_text(encoding="utf-8"))
    scored = evaluate_report(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(scored, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("BYBIT_EVIDENCE_SCORECARD=" + json.dumps(scored, sort_keys=True))


if __name__ == "__main__":
    main()