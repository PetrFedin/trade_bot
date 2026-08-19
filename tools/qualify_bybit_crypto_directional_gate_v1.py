from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.bybit_public_archive import BybitPublicTradeArchiveClient
from app.marketdata.bybit_v5 import BybitKlineAcquisition
from app.strategy.crypto_correlation import CryptoCorrelationPolicy
from app.strategy.crypto_execution_risk import CryptoExecutionRiskPolicy
from app.strategy.crypto_perp import CryptoSide
from app.strategy.crypto_runner_admission import CryptoRunnerAdmissionPolicy
from app.strategy.crypto_session_risk import CryptoSessionRiskPolicy
from tools.qualify_bybit_crypto_walk_forward import (
    CryptoWalkForwardPolicy,
    _aggregate_side_diagnostics,
    _calendar_folds,
    _decision_dict,
    _evaluate_candidate,
    _single_fold_side_metrics,
)
from tools.replay_bybit_crypto import default_crypto_config
from tools.replay_bybit_crypto_runner import replay_open_ended_crypto_runner

_POLICY_PATH = Path("research/bybit_crypto_directional_gate_v1_policy.json")
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
_ZERO = Decimal("0")


def load_directional_gate_policy(path: Path = _POLICY_PATH) -> dict[str, Any]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("directional gate policy must be an object")
    if raw.get("status") != "PREDECLARED_PROSPECTIVE_SHADOW_ONLY":
        raise ValueError("directional gate policy must remain prospective shadow-only")
    if raw.get("source_candidate") != "CONDITIONAL_COMBINED_RISK":
        raise ValueError("directional gate source candidate changed")
    if raw.get("discovery_evidence_may_validate_candidate") is not False:
        raise ValueError("discovery evidence cannot validate directional gate")
    if raw.get("same_sample_directional_activation_allowed") is not False:
        raise ValueError("same-sample directional activation must remain disabled")
    hypothesis = _mapping(raw.get("hypothesis"), "hypothesis")
    if hypothesis.get("candidate_action") != "BLOCK_NEW_SHORT_ENTRIES_KEEP_LONG_LOGIC_UNCHANGED":
        raise ValueError("directional gate candidate action changed")
    for key in (
        "long_entry_logic_changes_allowed",
        "exit_logic_changes_allowed_in_same_experiment",
        "risk_threshold_changes_allowed_in_same_experiment",
    ):
        if hypothesis.get(key) is not False:
            raise ValueError(f"directional gate policy unexpectedly allows {key}")
    promotion = _mapping(raw.get("promotion"), "promotion")
    if any(value is not False for value in promotion.values()):
        raise ValueError("directional gate promotion permissions must remain disabled")
    return raw


def prospective_validation_dates(policy: dict[str, Any]) -> tuple[date, ...]:
    evidence = _mapping(policy.get("prospective_evidence"), "prospective_evidence")
    minimum_days = _positive_int(
        evidence.get("minimum_completed_utc_archive_days"),
        "minimum_completed_utc_archive_days",
    )
    start = date.fromisoformat(str(policy["prospective_validation_start_utc"]))
    return tuple(start + timedelta(days=offset) for offset in range(minimum_days))


def prospective_readiness(
    *,
    now: datetime,
    policy: dict[str, Any],
) -> dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("directional gate readiness requires timezone-aware now")
    validation_dates = prospective_validation_dates(policy)
    completed_through = now.astimezone(UTC).date() - timedelta(days=1)
    available = tuple(value for value in validation_dates if value <= completed_through)
    ready = len(available) == len(validation_dates)
    return {
        "qualification": (
            "READY_FOR_FIXED_PROSPECTIVE_28D_EVIDENCE"
            if ready
            else "HOLD_INSUFFICIENT_PROSPECTIVE_DAYS"
        ),
        "ready": ready,
        "completed_utc_through": completed_through.isoformat(),
        "prospective_validation_start_utc": validation_dates[0].isoformat(),
        "prospective_validation_end_utc": validation_dates[-1].isoformat(),
        "required_completed_utc_days": len(validation_dates),
        "available_completed_utc_days": len(available),
        "validation_dates": [value.isoformat() for value in validation_dates],
        "discovery_evidence_may_validate_candidate": False,
        "automatic_strategy_activation_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_observation_automatic_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def run_directional_gate_walk_forward(
    acquisition: BybitKlineAcquisition,
    *,
    policy: dict[str, Any],
    opening_equity_usdt: Decimal = Decimal("1000"),
) -> dict[str, Any]:
    if opening_equity_usdt <= 0:
        raise ValueError("directional gate opening equity must be positive")
    validation_dates = prospective_validation_dates(policy)
    observed_dates = tuple(sorted({bar.start_time.date() for bar in acquisition.bars}))
    if observed_dates != validation_dates:
        raise ValueError("directional gate acquisition must match the fixed prospective window")

    evidence = _mapping(policy.get("prospective_evidence"), "prospective_evidence")
    walk_policy = CryptoWalkForwardPolicy(
        fold_days=_positive_int(evidence.get("fold_days"), "fold_days"),
        minimum_folds=_positive_int(evidence.get("minimum_folds"), "minimum_folds"),
        minimum_total_closed_trades=_positive_int(
            evidence.get("minimum_candidate_closed_trades"),
            "minimum_candidate_closed_trades",
        ),
        minimum_positive_fold_fraction=_decimal(
            evidence.get("minimum_candidate_positive_fold_fraction"),
            "minimum_candidate_positive_fold_fraction",
        ),
        minimum_aggregate_profit_factor=_decimal(
            evidence.get("minimum_candidate_profit_factor"),
            "minimum_candidate_profit_factor",
        ),
        maximum_worst_fold_drawdown_pct=_decimal(
            evidence.get("maximum_candidate_worst_fold_drawdown_pct"),
            "maximum_candidate_worst_fold_drawdown_pct",
        ),
        require_zero_risk_budget_breaches=bool(
            evidence.get("require_zero_candidate_risk_budget_breaches")
        ),
    )
    walk_policy.validate()
    folds = _calendar_folds(acquisition, walk_policy)

    source_config = default_crypto_config()
    candidate_config = replace(
        source_config,
        allowed_entry_sides=(CryptoSide.LONG,),
    )
    source_config.validate()
    candidate_config.validate()
    if source_config.allowed_entry_sides != (CryptoSide.LONG, CryptoSide.SHORT):
        raise ValueError("directional source must retain LONG and SHORT entries")

    source_reports: list[dict[str, Any]] = []
    candidate_reports: list[dict[str, Any]] = []
    fold_payloads: list[dict[str, Any]] = []
    for fold_index, (fold_dates, fold_acquisition) in enumerate(folds, start=1):
        source = _run_combined_risk(
            fold_acquisition,
            opening_equity_usdt=opening_equity_usdt,
            base_config=source_config,
        )
        candidate = _run_combined_risk(
            fold_acquisition,
            opening_equity_usdt=opening_equity_usdt,
            base_config=candidate_config,
        )
        source_reports.append(source)
        candidate_reports.append(candidate)
        fold_payloads.append(
            {
                "fold": fold_index,
                "dates": [value.isoformat() for value in fold_dates],
                "source": {
                    "metrics": source["metrics"],
                    "side_metrics": _single_fold_side_metrics(source),
                },
                "candidate_long_only": {
                    "metrics": candidate["metrics"],
                    "side_metrics": _single_fold_side_metrics(candidate),
                },
            }
        )

    source_decision = _decision_dict(
        _evaluate_candidate(
            "CONDITIONAL_COMBINED_RISK",
            source_reports,
            policy=walk_policy,
        )
    )
    candidate_decision = _decision_dict(
        _evaluate_candidate(
            "CONDITIONAL_COMBINED_RISK_LONG_ONLY_PROSPECTIVE",
            candidate_reports,
            policy=walk_policy,
        )
    )
    source_sides = _aggregate_side_diagnostics(source_reports)
    candidate_sides = _aggregate_side_diagnostics(candidate_reports)
    decision = evaluate_directional_gate_decision(
        policy=policy,
        source_decision=source_decision,
        source_side_diagnostics=source_sides,
        candidate_decision=candidate_decision,
        candidate_side_diagnostics=candidate_sides,
    )
    return {
        "qualification": "CRYPTO_DIRECTIONAL_GATE_V1_PROSPECTIVE_WALK_FORWARD",
        "method": "PAIRED_FIXED_28D_4X7D_COLD_START_SAME_DATA",
        "validation_dates": [value.isoformat() for value in validation_dates],
        "folds": fold_payloads,
        "source_decision": source_decision,
        "source_side_diagnostics": source_sides,
        "candidate_decision": candidate_decision,
        "candidate_side_diagnostics": candidate_sides,
        "directional_gate_decision": decision,
        "candidate_change_contract": {
            "source_allowed_entry_sides": ["LONG", "SHORT"],
            "candidate_allowed_entry_sides": ["LONG"],
            "only_directional_entry_filter_differs": True,
            "long_entry_logic_changed": False,
            "exit_logic_changed": False,
            "risk_thresholds_changed": False,
            "runner_admission_changed": False,
        },
        "discovery_evidence_may_validate_candidate": False,
        "parameter_tuning_between_folds": False,
        "cross_fold_signal_history_carried": False,
        "cross_fold_position_state_carried": False,
        "automatic_strategy_activation_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_observation_automatic_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def evaluate_directional_gate_decision(
    *,
    policy: dict[str, Any],
    source_decision: dict[str, Any],
    source_side_diagnostics: dict[str, Any],
    candidate_decision: dict[str, Any],
    candidate_side_diagnostics: dict[str, Any],
) -> dict[str, Any]:
    evidence = _mapping(policy.get("prospective_evidence"), "prospective_evidence")
    source_short = _mapping(source_side_diagnostics.get("SHORT"), "source SHORT diagnostics")
    candidate_short = _mapping(
        candidate_side_diagnostics.get("SHORT"),
        "candidate SHORT diagnostics",
    )
    reasons: list[str] = []

    active_short_folds = int(source_short.get("folds_with_trades", 0))
    short_positive_fraction = source_short.get(
        "positive_net_pnl_fold_fraction_among_active_folds"
    )
    short_total_pnl = _decimal(source_short.get("total_net_pnl_usdt"), "short total net pnl")
    short_pf = _optional_decimal(
        source_short.get("aggregate_profit_factor"),
        "short aggregate profit factor",
    )
    if active_short_folds < _positive_int(
        evidence.get("minimum_active_short_folds"),
        "minimum_active_short_folds",
    ):
        reasons.append("SOURCE_SHORT_ACTIVE_FOLDS_TOO_FEW")
    maximum_positive_fraction = _decimal(
        evidence.get("maximum_positive_short_fold_fraction"),
        "maximum_positive_short_fold_fraction",
    )
    if short_positive_fraction is None or _decimal(
        short_positive_fraction,
        "short positive fold fraction",
    ) > maximum_positive_fraction:
        reasons.append("SOURCE_SHORT_NOT_PERSISTENTLY_WEAK_BY_FOLD")
    maximum_short_pf = _decimal(
        evidence.get("maximum_short_aggregate_profit_factor"),
        "maximum_short_aggregate_profit_factor",
    )
    if short_pf is None or short_pf > maximum_short_pf:
        reasons.append("SOURCE_SHORT_PROFIT_FACTOR_NOT_WEAK_ENOUGH")
    if evidence.get("require_negative_short_total_net_pnl") is True and short_total_pnl >= _ZERO:
        reasons.append("SOURCE_SHORT_TOTAL_NET_PNL_NOT_NEGATIVE")

    if candidate_short.get("closed_trade_count") != 0:
        reasons.append("LONG_ONLY_CANDIDATE_TRADED_SHORT")
    if candidate_decision.get("research_stability_pass") is not True:
        reasons.append("LONG_ONLY_CANDIDATE_FAILED_ABSOLUTE_QUALITY")

    source_pnl = _decimal(source_decision.get("total_net_pnl_usdt"), "source total net pnl")
    candidate_pnl = _decimal(
        candidate_decision.get("total_net_pnl_usdt"),
        "candidate total net pnl",
    )
    if (
        evidence.get("require_candidate_net_pnl_not_worse_than_combined_baseline") is True
        and candidate_pnl < source_pnl
    ):
        reasons.append("LONG_ONLY_NET_PNL_WORSE_THAN_SOURCE")

    source_pf = _optional_decimal(
        source_decision.get("aggregate_profit_factor"),
        "source aggregate profit factor",
    )
    candidate_pf = _optional_decimal(
        candidate_decision.get("aggregate_profit_factor"),
        "candidate aggregate profit factor",
    )
    if (
        evidence.get("require_candidate_profit_factor_not_worse_than_combined_baseline") is True
        and not _profit_factor_not_worse(candidate_pf, source_pf)
    ):
        reasons.append("LONG_ONLY_PROFIT_FACTOR_WORSE_THAN_SOURCE")

    source_dd = _decimal(
        source_decision.get("worst_fold_drawdown_pct"),
        "source worst fold drawdown",
    )
    candidate_dd = _decimal(
        candidate_decision.get("worst_fold_drawdown_pct"),
        "candidate worst fold drawdown",
    )
    if (
        evidence.get("require_candidate_drawdown_not_worse_than_combined_baseline") is True
        and candidate_dd > source_dd
    ):
        reasons.append("LONG_ONLY_DRAWDOWN_WORSE_THAN_SOURCE")

    return {
        "prospective_research_pass": not reasons,
        "reasons": reasons,
        "source_short_persistence": {
            "active_fold_count": active_short_folds,
            "positive_fold_fraction_among_active_folds": short_positive_fraction,
            "aggregate_profit_factor": None if short_pf is None else float(short_pf),
            "total_net_pnl_usdt": float(short_total_pnl),
        },
        "paired_comparison": {
            "source_total_net_pnl_usdt": float(source_pnl),
            "candidate_total_net_pnl_usdt": float(candidate_pnl),
            "net_pnl_delta_usdt": float(candidate_pnl - source_pnl),
            "source_profit_factor": None if source_pf is None else float(source_pf),
            "candidate_profit_factor": None if candidate_pf is None else float(candidate_pf),
            "source_worst_fold_drawdown_pct": float(source_dd),
            "candidate_worst_fold_drawdown_pct": float(candidate_dd),
            "worst_fold_drawdown_delta_pct": float(candidate_dd - source_dd),
        },
        "discovery_evidence_may_validate_candidate": False,
        "automatic_strategy_activation_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_observation_automatic_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


def acquire_and_run_directional_gate(
    *,
    now: datetime | None = None,
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS,
    opening_equity_usdt: Decimal = Decimal("1000"),
    policy_path: Path = _POLICY_PATH,
    client: BybitPublicTradeArchiveClient | None = None,
) -> dict[str, Any]:
    policy = load_directional_gate_policy(policy_path)
    cutoff = datetime.now(UTC) if now is None else now
    readiness = prospective_readiness(now=cutoff, policy=policy)
    if readiness["ready"] is not True:
        return {
            **readiness,
            "source": "NO_MARKET_ACQUISITION_BEFORE_FIXED_WINDOW_IS_COMPLETE",
        }

    dates = prospective_validation_dates(policy)
    archive = BybitPublicTradeArchiveClient() if client is None else client
    acquisition = archive.fetch_klines(
        symbols=symbols,
        dates=dates,
        interval_minutes=5,
    )
    acquisition.validate(requested_symbols=symbols, minimum_bars=25)
    report = run_directional_gate_walk_forward(
        acquisition.klines,
        policy=policy,
        opening_equity_usdt=opening_equity_usdt,
    )
    report.update(
        source="BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        symbols=list(symbols),
        archive_completed_utc_days_only=True,
        fixed_prospective_window=True,
        raw_trade_archive_committed_to_repository=False,
        automatic_strategy_activation_allowed=False,
        strategy_promotion_allowed=False,
        demo_observation_automatic_activation_allowed=False,
        bybit_live_order_routing_allowed=False,
    )
    return report


def _run_combined_risk(
    acquisition: BybitKlineAcquisition,
    *,
    opening_equity_usdt: Decimal,
    base_config: Any,
) -> dict[str, Any]:
    return replay_open_ended_crypto_runner(
        acquisition,
        opening_equity_usdt=opening_equity_usdt,
        base_config=base_config,
        runner_admission_policy=CryptoRunnerAdmissionPolicy(),
        session_risk_policy=CryptoSessionRiskPolicy(),
        correlation_policy=CryptoCorrelationPolicy(),
        execution_risk_policy=CryptoExecutionRiskPolicy(),
    )


def _profit_factor_not_worse(
    candidate: Decimal | None,
    source: Decimal | None,
) -> bool:
    if source is None:
        return candidate is None
    if candidate is None:
        return True
    return candidate >= source


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"directional gate {field} must be an object")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"directional gate {field} must be a positive integer")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"directional gate {field} must be a positive integer") from exc
    if parsed < 1 or str(parsed) != str(value):
        raise ValueError(f"directional gate {field} must be a positive integer")
    return parsed


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"directional gate {field} must be a finite decimal")
    try:
        parsed = Decimal(str(value))
    except (ValueError, ArithmeticError) as exc:
        raise ValueError(f"directional gate {field} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"directional gate {field} must be a finite decimal")
    return parsed


def _optional_decimal(value: object, field: str) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value, field)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate the predeclared prospective Bybit crypto directional gate"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--policy", type=Path, default=_POLICY_PATH)
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument("--now")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    symbols = tuple(
        symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()
    )
    now = None if args.now is None else datetime.fromisoformat(args.now)
    report = acquire_and_run_directional_gate(
        now=now,
        symbols=symbols,
        opening_equity_usdt=Decimal(args.opening_equity),
        policy_path=args.policy,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("BYBIT_DIRECTIONAL_GATE_V1=" + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
