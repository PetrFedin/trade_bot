from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.bybit_public_archive import (
    BybitPublicTradeArchiveClient,
    completed_archive_dates,
)
from app.marketdata.bybit_v5 import BybitKlineAcquisition
from tools.research_bybit_crypto_strategy_v2 import run_crypto_strategy_v2_suite

_ZERO = Decimal("0")
_DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "ADAUSDT",
)
_SIDES = ("LONG", "SHORT")


@dataclass(frozen=True)
class CryptoWalkForwardPolicy:
    fold_days: int = 7
    minimum_folds: int = 4
    minimum_total_closed_trades: int = 30
    minimum_positive_fold_fraction: Decimal = Decimal("0.75")
    minimum_aggregate_profit_factor: Decimal = Decimal("1.20")
    maximum_worst_fold_drawdown_pct: Decimal = Decimal("5.0")
    require_zero_risk_budget_breaches: bool = True

    def validate(self) -> None:
        if self.fold_days < 1 or self.minimum_folds < 2:
            raise ValueError("walk-forward fold days must be positive and folds >= 2")
        if self.minimum_total_closed_trades < 1:
            raise ValueError("walk-forward minimum closed trades must be positive")
        if not _ZERO <= self.minimum_positive_fold_fraction <= Decimal("1"):
            raise ValueError("walk-forward positive-fold fraction must be within [0, 1]")
        if self.minimum_aggregate_profit_factor <= 0:
            raise ValueError("walk-forward minimum profit factor must be positive")
        if self.maximum_worst_fold_drawdown_pct <= 0:
            raise ValueError("walk-forward drawdown ceiling must be positive")


@dataclass(frozen=True)
class CryptoWalkForwardCandidateDecision:
    candidate: str
    research_stability_pass: bool
    reasons: tuple[str, ...]
    fold_count: int
    positive_fold_count: int
    positive_fold_fraction: Decimal
    total_closed_trades: int
    total_net_pnl_usdt: Decimal
    aggregate_profit_factor: Decimal | None
    worst_fold_drawdown_pct: Decimal
    total_fees_usdt: Decimal
    total_risk_budget_breaches: int
    strategy_promotion_allowed: bool = False
    demo_observation_allowed: bool = False
    live_promotion_allowed: bool = False


def run_crypto_walk_forward(
    acquisition: BybitKlineAcquisition,
    *,
    opening_equity_usdt: Decimal = Decimal("1000"),
    policy: CryptoWalkForwardPolicy | None = None,
) -> dict[str, Any]:
    """Run fixed-parameter strategy-v2 candidates over non-overlapping chronological folds."""

    active = CryptoWalkForwardPolicy() if policy is None else policy
    active.validate()
    if opening_equity_usdt <= 0:
        raise ValueError("walk-forward opening equity must be positive")

    folds = _calendar_folds(acquisition, active)
    fold_reports: list[dict[str, Any]] = []
    candidate_fold_reports: dict[str, list[dict[str, Any]]] = {}
    for fold_index, (fold_dates, fold_acquisition) in enumerate(folds, start=1):
        suite = run_crypto_strategy_v2_suite(
            fold_acquisition,
            opening_equity_usdt=opening_equity_usdt,
        )
        candidate_metrics: dict[str, Any] = {}
        for candidate_name, candidate in suite["candidates"].items():
            candidate_fold_reports.setdefault(candidate_name, []).append(candidate)
            candidate_metrics[candidate_name] = {
                "metrics": candidate["metrics"],
                "side_metrics": _single_fold_side_metrics(candidate),
                "accepted_trade_plan_event_count": candidate[
                    "accepted_trade_plan_event_count"
                ],
                "runner_selected_trade_count": candidate[
                    "runner_selected_trade_count"
                ],
                "fixed_target_selected_trade_count": candidate[
                    "fixed_target_selected_trade_count"
                ],
                "session_risk": candidate["session_risk"],
                "correlation_diversification": candidate[
                    "correlation_diversification"
                ],
                "execution_risk": candidate["execution_risk"],
            }
        fold_reports.append(
            {
                "fold": fold_index,
                "dates": [value.isoformat() for value in fold_dates],
                "first_date": fold_dates[0].isoformat(),
                "last_date": fold_dates[-1].isoformat(),
                "candidate_metrics": candidate_metrics,
            }
        )

    decisions = {
        candidate_name: _evaluate_candidate(
            candidate_name,
            reports,
            policy=active,
        )
        for candidate_name, reports in candidate_fold_reports.items()
    }
    decision_payload = {
        name: _decision_dict(decision) for name, decision in decisions.items()
    }
    side_diagnostics = {
        name: _aggregate_side_diagnostics(reports)
        for name, reports in candidate_fold_reports.items()
    }
    baseline = decisions.get("CONDITIONAL_1_5X")
    combined = decisions.get("CONDITIONAL_COMBINED_RISK")
    comparison = _combined_vs_baseline(baseline, combined)

    return {
        "qualification": "CRYPTO_CHRONOLOGICAL_WALK_FORWARD_RESEARCH",
        "method": "NON_OVERLAPPING_FIXED_PARAMETER_COLD_START_FOLDS",
        "opening_equity_usdt_per_fold": float(opening_equity_usdt),
        "policy": {
            "fold_days": active.fold_days,
            "minimum_folds": active.minimum_folds,
            "minimum_total_closed_trades": active.minimum_total_closed_trades,
            "minimum_positive_fold_fraction": float(
                active.minimum_positive_fold_fraction
            ),
            "minimum_aggregate_profit_factor": float(
                active.minimum_aggregate_profit_factor
            ),
            "maximum_worst_fold_drawdown_pct": float(
                active.maximum_worst_fold_drawdown_pct
            ),
            "require_zero_risk_budget_breaches": (
                active.require_zero_risk_budget_breaches
            ),
        },
        "fold_count": len(folds),
        "folds": fold_reports,
        "candidate_decisions": decision_payload,
        "candidate_side_diagnostics": side_diagnostics,
        "directional_filter_selection_allowed": False,
        "combined_vs_baseline": comparison,
        "parameter_tuning_between_folds": False,
        "cross_fold_position_state_carried": False,
        "cross_fold_signal_history_carried": False,
        "strategy_promotion_allowed": False,
        "demo_observation_allowed": False,
        "live_promotion_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def acquire_and_run_crypto_walk_forward(
    *,
    lookback_days: int = 28,
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS,
    opening_equity_usdt: Decimal = Decimal("1000"),
    policy: CryptoWalkForwardPolicy | None = None,
    client: BybitPublicTradeArchiveClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    active = CryptoWalkForwardPolicy() if policy is None else policy
    active.validate()
    if opening_equity_usdt <= 0:
        raise ValueError("walk-forward opening equity must be positive")
    minimum_days = active.fold_days * active.minimum_folds
    if lookback_days < minimum_days:
        raise ValueError(
            f"walk-forward requires at least {minimum_days} completed archive days"
        )
    cutoff = datetime.now(UTC) if now is None else now
    dates = completed_archive_dates(now=cutoff, lookback_days=lookback_days)
    archive = BybitPublicTradeArchiveClient() if client is None else client
    acquisition = archive.fetch_klines(
        symbols=symbols,
        dates=dates,
        interval_minutes=5,
    )
    acquisition.validate(requested_symbols=symbols, minimum_bars=25)
    report = run_crypto_walk_forward(
        acquisition.klines,
        opening_equity_usdt=opening_equity_usdt,
        policy=active,
    )
    report.update(
        source="BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        requested_archive_dates=[value.isoformat() for value in dates],
        symbols=list(symbols),
        archive_completed_utc_days_only=True,
        raw_trade_archive_committed_to_repository=False,
        strategy_promotion_allowed=False,
        demo_observation_allowed=False,
        live_promotion_allowed=False,
        bybit_live_order_routing_allowed=False,
    )
    return report


def _calendar_folds(
    acquisition: BybitKlineAcquisition,
    policy: CryptoWalkForwardPolicy,
) -> tuple[tuple[tuple[date, ...], BybitKlineAcquisition], ...]:
    dates = sorted({bar.start_time.date() for bar in acquisition.bars})
    required_days = policy.fold_days * policy.minimum_folds
    if len(dates) < required_days:
        raise ValueError(
            f"walk-forward acquisition has {len(dates)} days; requires {required_days}"
        )
    usable_days = len(dates) - (len(dates) % policy.fold_days)
    dates = dates[-usable_days:]
    folds: list[tuple[tuple[date, ...], BybitKlineAcquisition]] = []
    for offset in range(0, len(dates), policy.fold_days):
        fold_dates = tuple(dates[offset : offset + policy.fold_days])
        if len(fold_dates) != policy.fold_days:
            continue
        date_set = set(fold_dates)
        bars = tuple(bar for bar in acquisition.bars if bar.start_time.date() in date_set)
        symbols = sorted({bar.symbol for bar in bars})
        fold_acquisition = BybitKlineAcquisition(
            bars=bars,
            pages_by_symbol={symbol: 1 for symbol in symbols},
        )
        folds.append((fold_dates, fold_acquisition))
    if len(folds) < policy.minimum_folds:
        raise ValueError("walk-forward could not construct enough complete folds")
    return tuple(folds)


def _evaluate_candidate(
    candidate: str,
    reports: list[dict[str, Any]],
    *,
    policy: CryptoWalkForwardPolicy,
) -> CryptoWalkForwardCandidateDecision:
    total_closed = 0
    total_pnl = _ZERO
    total_fees = _ZERO
    total_breaches = 0
    positive_folds = 0
    worst_drawdown = _ZERO
    gross_positive_net = _ZERO
    gross_negative_net = _ZERO

    for report in reports:
        metrics = report["metrics"]
        total_closed += int(metrics["closed_trade_count"])
        fold_pnl = Decimal(str(metrics["total_net_pnl_usdt"]))
        total_pnl += fold_pnl
        total_fees += Decimal(str(metrics["fees_usdt"]))
        total_breaches += int(metrics["risk_budget_breach_count"])
        worst_drawdown = max(
            worst_drawdown,
            Decimal(str(metrics["maximum_drawdown_pct"])),
        )
        if fold_pnl > 0:
            positive_folds += 1
        for trade in report["closed_trades"]:
            net = Decimal(str(trade["net_pnl_usdt"]))
            if net > 0:
                gross_positive_net += net
            elif net < 0:
                gross_negative_net += -net

    fold_count = len(reports)
    positive_fraction = Decimal(positive_folds) / Decimal(fold_count)
    aggregate_pf = (
        None
        if gross_negative_net == 0
        else gross_positive_net / gross_negative_net
    )
    reasons: list[str] = []
    if fold_count < policy.minimum_folds:
        reasons.append("INSUFFICIENT_WALK_FORWARD_FOLDS")
    if total_closed < policy.minimum_total_closed_trades:
        reasons.append("INSUFFICIENT_WALK_FORWARD_CLOSED_TRADES")
    if positive_fraction < policy.minimum_positive_fold_fraction:
        reasons.append("POSITIVE_FOLD_FRACTION_TOO_LOW")
    if aggregate_pf is None or aggregate_pf < policy.minimum_aggregate_profit_factor:
        reasons.append("AGGREGATE_PROFIT_FACTOR_TOO_LOW")
    if worst_drawdown > policy.maximum_worst_fold_drawdown_pct:
        reasons.append("WORST_FOLD_DRAWDOWN_TOO_HIGH")
    if policy.require_zero_risk_budget_breaches and total_breaches > 0:
        reasons.append("WALK_FORWARD_RISK_BUDGET_BREACH")
    if total_pnl <= 0:
        reasons.append("WALK_FORWARD_TOTAL_NET_PNL_NOT_POSITIVE")

    return CryptoWalkForwardCandidateDecision(
        candidate=candidate,
        research_stability_pass=not reasons,
        reasons=tuple(reasons),
        fold_count=fold_count,
        positive_fold_count=positive_folds,
        positive_fold_fraction=positive_fraction,
        total_closed_trades=total_closed,
        total_net_pnl_usdt=total_pnl,
        aggregate_profit_factor=aggregate_pf,
        worst_fold_drawdown_pct=worst_drawdown,
        total_fees_usdt=total_fees,
        total_risk_budget_breaches=total_breaches,
    )


def _single_fold_side_metrics(report: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for side in _SIDES:
        trades = [
            trade
            for trade in report["closed_trades"]
            if str(trade["side"]).upper() == side
        ]
        nets = [Decimal(str(trade["net_pnl_usdt"])) for trade in trades]
        total_net = sum(nets, start=_ZERO)
        fees = sum(
            (Decimal(str(trade["fees_usdt"])) for trade in trades),
            start=_ZERO,
        )
        gross_profit = sum((net for net in nets if net > 0), start=_ZERO)
        gross_loss = sum((-net for net in nets if net < 0), start=_ZERO)
        output[side] = {
            "closed_trade_count": len(trades),
            "win_count": sum(1 for net in nets if net > 0),
            "loss_count": sum(1 for net in nets if net < 0),
            "total_net_pnl_usdt": float(total_net),
            "profit_factor": (
                None if gross_loss == 0 else float(gross_profit / gross_loss)
            ),
            "fees_usdt": float(fees),
        }
    observed_sides = {str(trade["side"]).upper() for trade in report["closed_trades"]}
    unexpected = observed_sides - set(_SIDES)
    if unexpected:
        raise ValueError(f"unexpected crypto trade sides in walk-forward: {sorted(unexpected)}")
    return output


def _aggregate_side_diagnostics(reports: list[dict[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for side in _SIDES:
        total_closed = 0
        wins = 0
        losses = 0
        total_net = _ZERO
        total_fees = _ZERO
        gross_profit = _ZERO
        gross_loss = _ZERO
        folds_with_trades = 0
        positive_folds = 0
        fold_net_pnl_usdt: list[float] = []
        for report in reports:
            metrics = _single_fold_side_metrics(report)[side]
            closed = int(metrics["closed_trade_count"])
            net = Decimal(str(metrics["total_net_pnl_usdt"]))
            total_closed += closed
            wins += int(metrics["win_count"])
            losses += int(metrics["loss_count"])
            total_net += net
            total_fees += Decimal(str(metrics["fees_usdt"]))
            fold_net_pnl_usdt.append(float(net))
            if closed > 0:
                folds_with_trades += 1
                if net > 0:
                    positive_folds += 1
            for trade in report["closed_trades"]:
                if str(trade["side"]).upper() != side:
                    continue
                trade_net = Decimal(str(trade["net_pnl_usdt"]))
                if trade_net > 0:
                    gross_profit += trade_net
                elif trade_net < 0:
                    gross_loss += -trade_net
        output[side] = {
            "fold_count": len(reports),
            "folds_with_trades": folds_with_trades,
            "positive_net_pnl_fold_count": positive_folds,
            "positive_net_pnl_fold_fraction_among_active_folds": (
                None
                if folds_with_trades == 0
                else float(Decimal(positive_folds) / Decimal(folds_with_trades))
            ),
            "closed_trade_count": total_closed,
            "win_count": wins,
            "loss_count": losses,
            "total_net_pnl_usdt": float(total_net),
            "aggregate_profit_factor": (
                None if gross_loss == 0 else float(gross_profit / gross_loss)
            ),
            "fees_usdt": float(total_fees),
            "fold_net_pnl_usdt": fold_net_pnl_usdt,
            "directional_filter_selection_allowed": False,
        }
    return output


def _decision_dict(decision: CryptoWalkForwardCandidateDecision) -> dict[str, Any]:
    return {
        "candidate": decision.candidate,
        "research_stability_pass": decision.research_stability_pass,
        "reasons": list(decision.reasons),
        "fold_count": decision.fold_count,
        "positive_fold_count": decision.positive_fold_count,
        "positive_fold_fraction": float(decision.positive_fold_fraction),
        "total_closed_trades": decision.total_closed_trades,
        "total_net_pnl_usdt": float(decision.total_net_pnl_usdt),
        "aggregate_profit_factor": (
            None
            if decision.aggregate_profit_factor is None
            else float(decision.aggregate_profit_factor)
        ),
        "worst_fold_drawdown_pct": float(decision.worst_fold_drawdown_pct),
        "total_fees_usdt": float(decision.total_fees_usdt),
        "total_risk_budget_breaches": decision.total_risk_budget_breaches,
        "strategy_promotion_allowed": False,
        "demo_observation_allowed": False,
        "live_promotion_allowed": False,
    }


def _combined_vs_baseline(
    baseline: CryptoWalkForwardCandidateDecision | None,
    combined: CryptoWalkForwardCandidateDecision | None,
) -> dict[str, Any] | None:
    if baseline is None or combined is None:
        return None
    return {
        "total_net_pnl_delta_usdt": float(
            combined.total_net_pnl_usdt - baseline.total_net_pnl_usdt
        ),
        "closed_trade_delta": combined.total_closed_trades - baseline.total_closed_trades,
        "worst_fold_drawdown_delta_pct": float(
            combined.worst_fold_drawdown_pct - baseline.worst_fold_drawdown_pct
        ),
        "risk_budget_breach_delta": (
            combined.total_risk_budget_breaches - baseline.total_risk_budget_breaches
        ),
        "positive_fold_fraction_delta": float(
            combined.positive_fold_fraction - baseline.positive_fold_fraction
        ),
        "automatic_strategy_selection_allowed": False,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run fixed-parameter Bybit crypto chronological walk-forward research"
    )
    parser.add_argument("--lookback-days", type=int, default=28)
    parser.add_argument("--fold-days", type=int, default=7)
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    symbols = tuple(
        symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()
    )
    report = acquire_and_run_crypto_walk_forward(
        lookback_days=args.lookback_days,
        symbols=symbols,
        opening_equity_usdt=Decimal(args.opening_equity),
        policy=CryptoWalkForwardPolicy(fold_days=args.fold_days),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("BYBIT_CRYPTO_WALK_FORWARD=" + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
