from __future__ import annotations

import argparse
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.strategy.crypto_evidence import CryptoReplayEvidence, evaluate_crypto_replay_evidence


def evaluate_report(report: dict[str, Any]) -> dict[str, Any]:
    first = datetime.fromisoformat(str(report["first_completed_bar"]))
    last = datetime.fromisoformat(str(report["last_completed_bar"]))
    duration_days = Decimal(str(max((last - first).total_seconds(), 1.0))) / Decimal("86400")
    opening_equity = Decimal(str(report["opening_equity_usdt"]))
    variants: dict[str, Any] = {}
    for name, payload in report["variants"].items():
        metrics = payload["metrics"]
        profit_factor_raw = metrics["profit_factor"]
        evidence = CryptoReplayEvidence(
            target_net_profit_usd=Decimal(str(payload["target_net_profit_usd"])),
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
        variants[name] = {
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
    return {
        "qualification": "CRYPTO_REPLAY_EVIDENCE_SCORED",
        "source_qualification": report["qualification"],
        "source": report["source"],
        "strategy_promotion_allowed": False,
        "live_promotion_allowed": False,
        "variants": variants,
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
