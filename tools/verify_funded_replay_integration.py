"""Independent real-history money audit against pinned PR #324 replay evidence.

This offline step consumes PR #324's complete historical report AND its
independent source/arithmetic verifier result, then independently recomputes
monetary invariants in every one of the 36 model variants. It does not
qualify actual exchange fills, settlement Mark Price, or live trading.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from tools.publish_research_receipt import ReceiptPublicationError, publish_new_json_receipt
from tools.verify_funded_portfolio_money import (
    PortfolioMoneyInvariantError,
    audit_portfolio_model,
    canonical_audit_digest,
)

SIX = ("BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
PERIODS = ("six_full_history", "six_matched_window", "broad21_matched_window")
COST_GRID = {("8", "5"), ("10", "10")}
MODEL_GRID = {
    f"{execution}:{funding}"
    for execution in ("DAILY_STOP_FIRST", "HOURLY_COLLISION_REFINED")
    for funding in ("NO_FUNDING", "UPPER_COST", "LOWER_COST")
}
MAX_REPORT_BYTES = 192 * 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024


class HistoricalMoneyIntegrationError(ValueError):
    """Fail-closed money and evidence admission without trading privileges."""


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical(value: dict) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _read(path: Path, limit: int) -> tuple[dict, bytes]:
    if not path.is_file() or path.is_symlink():
        raise HistoricalMoneyIntegrationError(f"missing or symlinked evidence: {path.name}")
    if path.stat().st_size > limit:
        raise HistoricalMoneyIntegrationError(f"evidence exceeds size cap: {path.name}")
    raw = path.read_bytes()

    def unique(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                raise HistoricalMoneyIntegrationError(f"duplicate JSON field: {key}")
            output[key] = value
        return output

    def reject_float(value):
        raise HistoricalMoneyIntegrationError("floating-point JSON evidence forbidden")

    def reject_constant(value):
        raise HistoricalMoneyIntegrationError("non-finite JSON evidence forbidden")

    try:
        parsed = json.loads(
            raw, object_pairs_hook=unique, parse_float=reject_float,
            parse_constant=reject_constant,
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise HistoricalMoneyIntegrationError(f"invalid JSON evidence: {path.name}") from exc
    if not isinstance(parsed, dict):
        raise HistoricalMoneyIntegrationError(f"JSON root not an object: {path.name}")
    return parsed, raw


def audit_real_historical_money(
    report: dict, verifier: dict, *, report_bytes: bytes,
    verifier_bytes: bytes, historical_head_sha: str,
    model_auditor: Callable[[str, dict, dict], dict] | None = None,
) -> dict:
    """Enforce full matched 36-model grid and independent monetary equations."""
    if (
        not isinstance(report, dict) or not isinstance(verifier, dict)
        or not isinstance(historical_head_sha, str)
        or len(historical_head_sha) != 40
        or any(char not in "0123456789abcdef" for char in historical_head_sha)
    ):
        raise HistoricalMoneyIntegrationError("unqualified historical evidence reference")
    unsigned = dict(report)
    source_evidence_sha = unsigned.pop("evidence_sha256", None)
    if not isinstance(source_evidence_sha, str) or source_evidence_sha != _hash(
        _canonical(unsigned)
    ):
        raise HistoricalMoneyIntegrationError("historical report evidence SHA-256 mismatch")
    if (
        report.get("schema_version") != "astra-daily-funding-cash-feedback-v1"
        or report.get("evaluation_class")
        != "REUSED_HISTORY_DAILY_CASH_FEEDBACK_NOT_VENUE_SETTLEMENT"
        or report.get("research_only") is not True
        or report.get("funding_cash_feedback_modelled") is not True
        or report.get("mark_price_used") is not False
        or report.get("notional_model") != "CONSTANT_ENTRY_EXECUTION_NOTIONAL"
        or report.get("untouched_holdout") is not False
        or report.get("cash_booking_clock") != "AFTER_DAILY_TRADES_BEFORE_NEXT_DAY_SIZING"
    ):
        raise HistoricalMoneyIntegrationError("historical research scope changed")
    for denied in (
        "strategy_promotion_allowed", "live_trading_allowed",
        "funding_exact_exchange_timing", "funding_same_hour_exit_refinement_used",
        "intraday_reinvestment", "margin_or_liquidation_modelled",
    ):
        if report.get(denied) is not False:
            raise HistoricalMoneyIntegrationError(f"unsafe report authority: {denied}")
    if (
        verifier.get("arithmetic_verification") != "PASS"
        or verifier.get("evidence_sha256") != source_evidence_sha
        or verifier.get("configurations") != 6
        or verifier.get("model_replays") != 36
        or verifier.get("matched_dates_verified") is not True
        or verifier.get("independent_human_or_agent_review") is not False
        or verifier.get("execution_or_venue_account_truth_verified") is not False
        or verifier.get("live_trading_allowed") is not False
    ):
        raise HistoricalMoneyIntegrationError("PR #324 independent verifier rejected")

    configs = report.get("comparisons")
    if not isinstance(configs, list) or len(configs) != 6:
        raise HistoricalMoneyIntegrationError("expected exactly six historical configurations")
    observed = {period: set() for period in PERIODS}
    summaries = {}
    chronology = {}
    for config in configs:
        if not isinstance(config, dict):
            raise HistoricalMoneyIntegrationError("malformed model configuration")
        period = config.get("period_id")
        costs = (config.get("fee_bps_per_fill"), config.get("slippage_bps_per_side"))
        if period not in observed or costs not in COST_GRID or costs in observed[period]:
            raise HistoricalMoneyIntegrationError("unexpected or repeated model configuration")
        observed[period].add(costs)
        symbols = config.get("symbols")
        if (
            not isinstance(symbols, list) or len(symbols) != len(set(symbols))
            or (period.startswith("six_") and symbols != list(SIX))
            or (period == "broad21_matched_window" and (
                len(symbols) != 21 or not set(SIX).issubset(symbols)
            ))
        ):
            raise HistoricalMoneyIntegrationError("frozen universe composition mismatch")
        models = config.get("models")
        if not isinstance(models, dict) or set(models) != MODEL_GRID:
            raise HistoricalMoneyIntegrationError("missing historical funding/exit scenario")
        if period.endswith("matched_window"):
            period_dates = (
                config.get("first_source_day_utc"),
                config.get("last_source_day_utc"),
            )
            if period in chronology and chronology[period] != period_dates:
                raise HistoricalMoneyIntegrationError("matched-window cost chronology drift")
            chronology[period] = period_dates
        for name in sorted(models):
            model = models[name]
            identity = f"{period}:{costs[0]}:{costs[1]}:{name}"
            parameters = {
                "scenario": name.split(":")[1],
                "allowed_symbols": tuple(symbols),
                "expected_first_execution_day_utc": config["first_execution_day_utc"],
                "expected_last_source_day_utc": config["last_source_day_utc"],
                "fee_bps_per_fill": costs[0],
                "slippage_bps_per_side": costs[1],
            }
            try:
                summaries[identity] = (
                    audit_portfolio_model(model, **parameters)
                    if model_auditor is None
                    else model_auditor(identity, model, parameters)
                )
            except PortfolioMoneyInvariantError as exc:
                raise HistoricalMoneyIntegrationError(
                    f"historical model monetary drift {identity}: {exc}"
                ) from exc
    if (
        any(observed[period] != COST_GRID for period in PERIODS)
        or chronology.get("six_matched_window")
        != chronology.get("broad21_matched_window")
        or len(summaries) != 36
    ):
        raise HistoricalMoneyIntegrationError("36-model stress grid or dates incomplete")
    return {
        "schema_version": "astra-historical-funded-money-integration-v1",
        "evaluation_class": "REUSED_HISTORY_INTERNAL_MONEY_RECONCILIATION_ONLY",
        "historical_source_head_sha": historical_head_sha,
        "historical_report_evidence_sha256": source_evidence_sha,
        "historical_report_file_sha256": _hash(report_bytes),
        "independent_verifier_file_sha256": _hash(verifier_bytes),
        "money_audit_sha256": canonical_audit_digest(summaries),
        "configuration_count": len(configs),
        "model_count": len(summaries),
        "matched_chronology_verified": True,
        "independent_arithmetic_reconciled": True,
        "model_errors_rejected": True,
        "source_provider_origin_verified": False,
        "settlement_mark_price_verified": False,
        "account_funding_ledger_verified": False,
        "real_execution_verified": False,
        "untouched_holdout": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "profitability_proven": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--independent-verifier", type=Path, required=True)
    parser.add_argument("--historical-head-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report, raw_report = _read(args.report, MAX_REPORT_BYTES)
    verifier, raw_verifier = _read(args.independent_verifier, MAX_RECEIPT_BYTES)
    receipt = audit_real_historical_money(
        report, verifier, report_bytes=raw_report, verifier_bytes=raw_verifier,
        historical_head_sha=args.historical_head_sha,
    )
    try:
        publish_new_json_receipt(args.output, receipt)
    except ReceiptPublicationError as exc:
        raise HistoricalMoneyIntegrationError(
            f"immutable money receipt refused: {exc}"
        ) from exc
    print("REPLAY_MONEY_36_MODEL_AUDIT", json.dumps({
        "models": receipt["model_count"],
        "sha256": receipt["money_audit_sha256"],
        "profitability_proven": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
