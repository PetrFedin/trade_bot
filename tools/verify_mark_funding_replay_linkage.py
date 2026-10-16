"""Cross-PR, research-only linkage gate for ASTRA Mark Price and funded replay.

Bind a separately verified PR #324 historical funding/cash replay to a fully
audited, six-symbol PR #326 Mark Price source set. This NEVER recalculates or
substitutes funding PnL, does not establish venue authenticity, and cannot
promote a strategy or authorize execution. The bridge fails closed on mismatch.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tools.mark_price_hourly_sensitivity import _utc
from tools.summarize_mark_price_hourly_sensitivity import SIX, summarize
from tools.verify_funded_portfolio_money import (
    audit_portfolio_model,
    canonical_audit_digest,
)

MODEL_KEYS = {
    f"{execution}:{funding}"
    for execution in ("DAILY_STOP_FIRST", "HOURLY_COLLISION_REFINED")
    for funding in ("NO_FUNDING", "UPPER_COST", "LOWER_COST")
}
PERIODS = ("six_full_history", "six_matched_window", "broad21_matched_window")
COSTS = {("8", "5"), ("10", "10")}
DIGEST = re.compile(r"[0-9a-f]{64}")


class MarkFundingLinkageError(ValueError):
    """No admission when evidence or timelines cannot be reconciled."""


def _read(path: Path, *, max_bytes: int = 192 * 1024 * 1024) -> tuple[dict, bytes]:
    if not path.is_file() or path.is_symlink():
        raise MarkFundingLinkageError(f"missing or symlinked evidence: {path.name}")
    if path.stat().st_size > max_bytes:
        raise MarkFundingLinkageError(f"evidence exceeds size limit: {path.name}")
    data = path.read_bytes()

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise MarkFundingLinkageError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_nonfinite(value):
        raise MarkFundingLinkageError(f"non-finite JSON value: {value}")

    try:
        value = json.loads(
            data, object_pairs_hook=unique_object, parse_constant=reject_nonfinite
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise MarkFundingLinkageError(f"malformed evidence: {path.name}") from exc
    if not isinstance(value, dict):
        raise MarkFundingLinkageError(f"evidence root must be object: {path.name}")
    return value, data


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _false_flags(data: dict, label: str, fields: tuple[str, ...]) -> None:
    for field in fields:
        if data.get(field) is not False:
            raise MarkFundingLinkageError(f"{label} unsafe/missing flag: {field}")


def _validate_source_set(
    root: Path, funding_root: Path, expected_set_sha256: str
) -> tuple[dict, dict, dict]:
    if not isinstance(expected_set_sha256, str) or not DIGEST.fullmatch(
        expected_set_sha256
    ):
        raise MarkFundingLinkageError("invalid detached source-set SHA-256")
    receipt, _ = _read(root / "source-set-receipt.json", max_bytes=1_000_000)
    documented_summary, raw_summary = _read(
        root / "summary.json", max_bytes=1_000_000
    )
    if (
        receipt.get("schema_version") != "astra-six-major-offline-source-set-receipt-v1"
        or receipt.get("source_set_sha256") != expected_set_sha256
        or receipt.get("symbols") != list(SIX)
        or receipt.get("summary_sha256") != _sha(raw_summary)
        or receipt.get("source_hash_consistency_verified") is not True
    ):
        raise MarkFundingLinkageError("six-major source-set receipt/pin mismatch")
    _false_flags(receipt, "source set", (
        "provider_origin_verified", "account_funding_ledger_verified",
        "exact_settlement_mark_verified", "strategy_replay_completed",
        "strategy_promotion_allowed", "live_trading_allowed",
        "profitability_proven",
    ))
    summary = summarize(root, funding_root=funding_root)
    if documented_summary != summary:
        raise MarkFundingLinkageError("six-major summary disagrees with raw sources")
    if (
        summary.get("symbols") != list(SIX)
        or receipt.get("start") != summary.get("start")
        or receipt.get("end") != summary.get("end")
        or receipt.get("hourly_rows_total") != summary.get("hourly_rows_total")
        or receipt.get("funding_settlements_total")
        != summary.get("funding_settlements_total")
    ):
        raise MarkFundingLinkageError("source-set chronology/count mismatch")
    _false_flags(summary, "source summary", (
        "exact_settlement_mark_verified", "compatible_with_final_1m_admission",
        "account_funding_ledger_verified", "strategy_replay_completed",
        "strategy_promotion_allowed", "profitability_proven",
    ))
    per_symbol = receipt.get("symbol_receipts")
    if not isinstance(per_symbol, dict) or set(per_symbol) != set(SIX):
        raise MarkFundingLinkageError("incomplete per-symbol replay receipts")
    manifests = {}
    for symbol in SIX:
        mark, raw_mark = _read(
            root / symbol / f"{symbol}_mark_60m.manifest.json",
            max_bytes=500_000,
        )
        recorded = per_symbol[symbol]
        if (
            not isinstance(recorded, dict)
            or recorded.get("symbol") != symbol
            or recorded.get("manifest_sha256") != _sha(raw_mark)
            or mark.get("schema_version") != "bybit-mark-price-hourly-sensitivity-v1"
            or mark.get("interval_minutes") != 60
        ):
            raise MarkFundingLinkageError(f"{symbol}: receipt/manifests mismatch")
        manifests[symbol] = mark
    return receipt, summary, manifests


def _validate_funded_replay(
    report: dict, verifier: dict, mark_summary: dict, manifests: dict
) -> str:
    unsigned = dict(report)
    reported_digest = unsigned.pop("evidence_sha256", None)
    if not isinstance(reported_digest, str) or not DIGEST.fullmatch(reported_digest):
        raise MarkFundingLinkageError("funding replay evidence digest missing")
    if _sha(canonical_json_bytes(unsigned)) != reported_digest:
        raise MarkFundingLinkageError("funding replay canonical SHA-256 mismatch")
    if (
        report.get("schema_version") != "astra-daily-funding-cash-feedback-v1"
        or report.get("evaluation_class")
        != "REUSED_HISTORY_DAILY_CASH_FEEDBACK_NOT_VENUE_SETTLEMENT"
        or report.get("research_only") is not True
        or report.get("funding_cash_feedback_modelled") is not True
        or report.get("mark_price_used") is not False
        or report.get("notional_model") != "CONSTANT_ENTRY_EXECUTION_NOTIONAL"
        or report.get("untouched_holdout") is not False
    ):
        raise MarkFundingLinkageError("funded replay research semantics changed")
    _false_flags(report, "funded replay", (
        "strategy_promotion_allowed", "live_trading_allowed",
        "funding_exact_exchange_timing", "margin_or_liquidation_modelled",
    ))
    if (
        verifier.get("arithmetic_verification") != "PASS"
        or verifier.get("configurations") != 6
        or verifier.get("model_replays") != 36
        or verifier.get("matched_dates_verified") is not True
        or verifier.get("evidence_sha256") != reported_digest
        or verifier.get("execution_or_venue_account_truth_verified") is not False
        or verifier.get("independent_human_or_agent_review") is not False
        or verifier.get("live_trading_allowed") is not False
    ):
        raise MarkFundingLinkageError("independent replay verification receipt mismatch")

    source_hashes = report.get("source_daily_and_funding_sha256")
    if not isinstance(source_hashes, dict):
        raise MarkFundingLinkageError("funding source fingerprint map missing")
    for symbol, meta in manifests.items():
        for filename, key in (
            (f"{symbol}_funding.csv", "funding_source_sha256"),
            (f"{symbol}_funding.manifest.json", "funding_manifest_sha256"),
        ):
            if (
                source_hashes.get(f"funding/{filename}") != meta.get(key)
                or not isinstance(meta.get(key), str)
                or DIGEST.fullmatch(meta[key]) is None
            ):
                raise MarkFundingLinkageError(
                    f"{symbol}: funding archive SHA-256 differs between PRs"
                )

    start = _utc(mark_summary["start"], "Mark Price source start")
    end = _utc(mark_summary["end"], "Mark Price source end")
    if (
        end < start
        or (end - start + timedelta(hours=1)) % timedelta(days=1)
        != timedelta(0)
        or (start.hour, end.hour) != (0, 23)
    ):
        raise MarkFundingLinkageError("Mark Price chronology not complete UTC days")
    rows = report.get("comparisons")
    if not isinstance(rows, list) or len(rows) != 6:
        raise MarkFundingLinkageError("funded replay must preserve all six configurations")
    matched = []
    money_audits = {}
    observed = {period: set() for period in PERIODS}
    for row in rows:
        if not isinstance(row, dict):
            raise MarkFundingLinkageError("invalid funded replay configuration")
        period = row.get("period_id")
        pair = (row.get("fee_bps_per_fill"), row.get("slippage_bps_per_side"))
        if period not in observed or pair not in COSTS or pair in observed[period]:
            raise MarkFundingLinkageError("missing/duplicate funded configuration")
        observed[period].add(pair)
        if not isinstance(row.get("models"), dict) or set(row["models"]) != MODEL_KEYS:
            raise MarkFundingLinkageError("missing signed funding or exit scenarios")
        for model_key, model in row["models"].items():
            money_audits[f"{period}:{pair[0]}:{pair[1]}:{model_key}"] = (
                audit_portfolio_model(
                    model, scenario=model_key.split(":")[1],
                    allowed_symbols=tuple(row["symbols"]),
                    fee_bps_per_fill=pair[0],
                    slippage_bps_per_side=pair[1],
                )
            )
        if period == "six_matched_window":
            if row.get("symbols") != list(SIX):
                raise MarkFundingLinkageError("six-major universe changed")
            matched.append(row)
        elif period == "broad21_matched_window":
            if (
                not isinstance(row.get("symbols"), list)
                or len(row["symbols"]) != 21
                or not set(SIX).issubset(row["symbols"])
            ):
                raise MarkFundingLinkageError("broad universe corrupted")
            matched.append(row)
    if any(observed[period] != COSTS for period in PERIODS):
        raise MarkFundingLinkageError("incomplete historical stress grid")
    for row in matched:
        if (
            _utc(row["first_source_day_utc"], "replay start") != start
            or _utc(row["last_source_day_utc"], "replay end") + timedelta(
                hours=23
            ) != end
        ):
            raise MarkFundingLinkageError("funding replay / Mark Price windows differ")
    return reported_digest, canonical_audit_digest(money_audits)


def verify_linkage(
    *,
    source_root: Path,
    funding_root: Path,
    daily_report_path: Path,
    daily_verifier_path: Path,
    expected_source_set_sha256: str,
) -> dict:
    """Return only a research compatibility receipt; do not recompute PnL."""
    _, mark_summary, manifests = _validate_source_set(
        source_root, funding_root, expected_source_set_sha256
    )
    daily_report, raw_report = _read(daily_report_path)
    daily_verifier, raw_verifier = _read(
        daily_verifier_path, max_bytes=5_000_000
    )
    report_sha, money_sha = _validate_funded_replay(
        daily_report, daily_verifier, mark_summary, manifests
    )
    return {
        "schema_version": "astra-mark-funding-replay-linkage-v1",
        "evaluation_class": "RESEARCH_SOURCE_IDENTITY_ONLY_NO_FUNDING_PNL_RECALC",
        "source_set_sha256": expected_source_set_sha256,
        "mark_summary_evidence_sha256": mark_summary["evidence_sha256"],
        "daily_report_evidence_sha256": report_sha,
        "independent_money_audit_sha256": money_sha,
        "money_models_reconciled": 36,
        "daily_report_file_sha256": _sha(raw_report),
        "independent_verifier_file_sha256": _sha(raw_verifier),
        "symbols": list(SIX),
        "start": mark_summary["start"],
        "end": mark_summary["end"],
        "mark_price_interval_minutes": 60,
        "funding_source_shas_equal": True,
        "matched_universe_chronology_equal": True,
        "arithmetic_verification_receipt_bound": True,
        "funding_pnl_recomputed_with_mark_price": False,
        "mark_price_exact_at_settlement_verified": False,
        "account_funding_ledger_verified": False,
        "provider_origin_verified": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "profitability_proven": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--funding-root", type=Path, required=True)
    parser.add_argument("--daily-report", type=Path, required=True)
    parser.add_argument("--daily-verifier", type=Path, required=True)
    parser.add_argument("--source-set-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = verify_linkage(
        source_root=args.source_root, funding_root=args.funding_root,
        daily_report_path=args.daily_report,
        daily_verifier_path=args.daily_verifier,
        expected_source_set_sha256=args.source_set_sha256,
    )
    if args.output.exists() or args.output.is_symlink():
        raise MarkFundingLinkageError("output exists: receipts are immutable")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("MARK_FUNDING_REPLAY_SOURCE_LINKAGE_ONLY", report["daily_report_evidence_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
