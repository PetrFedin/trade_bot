"""Read-only ASTRA research terminal from independently reconciled 36-model history.

Produces one standalone HTML document. It has no order route, external assets,
fake prices, authentication claims, or interactive trading authority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tools.historical_execution_journal import build_journal

PERIODS = ("six_full_history", "six_matched_window", "broad21_matched_window")
COSTS = (("8", "5"), ("10", "10"))
EXIT_MODES = ("DAILY_STOP_FIRST", "HOURLY_COLLISION_REFINED")
FUNDING_MODES = ("NO_FUNDING", "UPPER_COST", "LOWER_COST")
DEFAULT_KEY = "six_matched_window|8|5|HOURLY_COLLISION_REFINED:UPPER_COST"


class ResearchConsoleError(ValueError):
    """Reject unverified, promotional, stale or unsafe research evidence."""


def _number(value: object) -> str:
    if isinstance(value, bool):
        raise ResearchConsoleError("boolean masquerades as numeric evidence")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ResearchConsoleError("invalid research metric") from exc
    if not number.is_finite():
        raise ResearchConsoleError("nonfinite research metric")
    return str(number)


def _verified_payload(journal: dict, original: dict, auditor: dict) -> dict:
    # Reconstruct full historical financial/trade journal from the same raw
    # independently audited report: an arbitrary 36-row JSON cannot impersonate it.
    rebuilt = build_journal(original, auditor)
    if rebuilt != journal:
        raise ResearchConsoleError("journal diverges from independent money/source audit")
    if (
        journal.get("schema_version") != "astra-trade-journal-reused-history-v1"
        or journal.get("evaluation_class") != "REUSED_INSPECTED_HISTORY_DIAGNOSTIC_ONLY"
        or journal.get("modelled_trading_only") is not True
        or journal.get("scenario_count") != 6
        or journal.get("model_count") != 36
    ):
        raise ResearchConsoleError("unexpected historical research schema")
    for flag in (
        "actual_exchange_fills_verified", "account_funding_ledger_verified",
        "settlement_mark_price_verified", "untouched_holdout",
        "profitability_proven", "strategy_promotion_allowed", "live_trading_allowed",
    ):
        if journal.get(flag) is not False:
            raise ResearchConsoleError(f"research authority must remain false: {flag}")
    if hashlib.sha256(canonical_json_bytes({
        k: v for k, v in journal.items() if k != "journal_sha256"
    })).hexdigest() != journal.get("journal_sha256"):
        raise ResearchConsoleError("research journal digest mismatch")

    cases = {}
    for comparison in journal["comparisons"]:
        period = comparison["period_id"]
        fee = comparison["fee_bps_per_fill"]
        slip = comparison["slippage_bps_per_side"]
        if period not in PERIODS or (fee, slip) not in COSTS:
            raise ResearchConsoleError("unsupported universe or execution cost")
        for mode, model in comparison["models"].items():
            if mode not in {
                f"{ex}:{fund}" for ex in EXIT_MODES for fund in FUNDING_MODES
            }:
                raise ResearchConsoleError("unregistered exit/funding model")
            key = "|".join((period, fee, slip, mode))
            if key in cases or model["model_key"] != mode:
                raise ResearchConsoleError("duplicate or mismatched research model")
            if Decimal(_number(model["ending_equity_usd"])) <= 0:
                raise ResearchConsoleError("nonpositive modelled account equity")
            # Results are historical equity and closed-trade attribution, not
            # actual venue settlements or a margin ledger.
            return_pct = Decimal(_number(model["portfolio_return_pct"]))
            ending = Decimal(_number(model["ending_equity_usd"]))
            if abs(return_pct - (ending / Decimal("10000") - 1) * 100) > Decimal("0.000001"):
                raise ResearchConsoleError("historical return/equity drift")
            by_symbol = model["by_symbol"]
            if sum(z["closed_trades"] for z in by_symbol.values()) != model["closed_trades"]:
                raise ResearchConsoleError("symbol attribution trade population mismatch")
            for symbol, row in by_symbol.items():
                if (not isinstance(symbol, str) or not symbol
                    or type(row["wins"]) is not int
                    or type(row["closed_trades"]) is not int
                    or not 0 <= row["wins"] <= row["closed_trades"]):
                    raise ResearchConsoleError("invalid historical symbol attribution")
                _number(row["closed_trade_net_usd"])
            window_sum = 0
            for window in model["windows"]:
                if type(window["complete_window"]) is not bool:
                    raise ResearchConsoleError("unqualified historical window")
                for numeric in ("equity_return_pct", "starting_equity_usd",
                                "ending_equity_usd", "max_daily_close_drawdown_pct"):
                    _number(window[numeric])
                window_sum += window["days"]
            if window_sum != model["trading_days"]:
                raise ResearchConsoleError("lost historical research days")
            cases[key] = {
                "period": period,
                "fees": fee,
                "slippage": slip,
                "mode": mode,
                "first_day": model["first_execution_day_utc"][:10],
                "last_day": model["last_source_day_utc"][:10],
                "trades": model["closed_trades"],
                "return_pct": str(return_pct),
                "ending_equity_usd": str(ending),
                "max_drawdown_pct": _number(model["max_drawdown_pct"]),
                "closed_trade_net_usd": _number(model["closed_trade_net_usd"]),
                "trade_stats": model["closed_trade_statistics"],
                "closed_trade_fees_usd": _number(model["closed_trade_fees_usd"]),
                "closed_trade_slippage_usd": _number(model["closed_trade_slippage_usd"]),
                "funding_cash_net_usd": _number(model["funding_cash_net_usd"]),
                "reason_counts": model["exit_reasons"],
                "windows": model["windows"],
                "by_symbol": by_symbol,
                "worst_trades": model["worst_closed_trades"],
                "best_trades": model["best_closed_trades"],
            }
    for case in cases.values():
        stats = case["trade_stats"]
        if (
            stats["closed_trades"] != case["trades"]
            or stats["wins"] + stats["losses"] + stats["breakeven"]
            != case["trades"]
            or stats["funding_included"] is not False
            or stats["actual_venue_fill_order_verified"] is not False
            or stats["same_day_exit_tiebreak"] != "UTC_EXIT_DATE_THEN_SYMBOL_LEXICAL"
        ):
            raise ResearchConsoleError("closed-trade statistics evidence mismatch")
        for key in (
            "win_rate_pct", "profit_factor_ex_funding", "average_win_usd",
            "average_loss_usd", "average_closed_trade_net_usd",
        ):
            if stats[key] is not None:
                _number(stats[key])
    if len(cases) != 36 or DEFAULT_KEY not in cases:
        raise ResearchConsoleError("incomplete 36-model historic research matrix")
    pairs = journal["matched_universe_differences"]
    if len(pairs) != 12 or any(p.get("same_dates_verified") is not True for p in pairs):
        raise ResearchConsoleError("unverified matched-universe comparison")
    for pair in pairs:
        for field in ("six_return_pct", "broad21_return_pct",
                      "six_minus_broad21_percentage_points"):
            _number(pair[field])
        if abs(
            Decimal(pair["six_return_pct"]) - Decimal(pair["broad21_return_pct"])
            - Decimal(pair["six_minus_broad21_percentage_points"])
        ) > Decimal("0.000001"):
            raise ResearchConsoleError("matched universe delta differs from source")

    return {
        "cases": cases,
        "pairs": pairs,
        "journal_sha256": journal["journal_sha256"],
        "source_sha256": journal["source_report_sha256"],
        "auditor_sha256": journal["independent_auditor_sha256"],
        "scope": "REUSED_INSPECTED_HISTORY_NOT_UNTOUCHED",
        "trading_allowed": False,
    }


def _safe_json(data: dict) -> str:
    # HTML parser must never interpret user/source-controlled data as tags,
    # especially the closing script tag even in application/json scripts.
    value = json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True,
                       separators=(",", ":"))
    return (value.replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


HTML_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[1]
    / "app" / "platform_assets" / "historical_research_terminal.html"
)
HTML = HTML_TEMPLATE_PATH.read_text(encoding="utf-8")


def render(journal: dict, original: dict, auditor: dict) -> str:
    payload = _verified_payload(journal, original, auditor)
    first = payload["cases"][DEFAULT_KEY]
    replacements = {
        "__RESEARCH_JSON__": _safe_json(payload),
        "__JOURNAL_SHA__": payload["journal_sha256"],
        "__SOURCE_SHA__": payload["source_sha256"],
        "__AUDITOR_SHA__": payload["auditor_sha256"],
        "__INITIAL_DATES__": first["first_day"] + " — " + first["last_day"],
        "__INITIAL_RETURN__": first["return_pct"] + "%",
        "__INITIAL_EQUITY__": first["ending_equity_usd"],
        "__INITIAL_DRAWDOWN__": first["max_drawdown_pct"] + "%",
        "__INITIAL_TRADES__": str(first["trades"]),
    }
    page = HTML
    for marker, replacement in replacements.items():
        if page.count(marker) != 1:
            raise ResearchConsoleError(f"invalid research terminal marker: {marker}")
        page = page.replace(marker, replacement)
    return page


def publish_new_html(path: Path, page: str) -> None:
    """Durable no-replace publication; never check-then-overwrite or follow symlinks."""
    parent = path.parent
    if any(folder.is_symlink() for folder in (parent, *parent.parents)):
        raise ResearchConsoleError("symlink in research output path")
    parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        raise ResearchConsoleError("research console already exists; no replacement")
    descriptor, temp = tempfile.mkstemp(prefix=".astra-html-", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(page.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, path, follow_symlinks=False)
        except OSError as exc:
            raise ResearchConsoleError("atomic console publication refused") from exc
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--source-report", required=True, type=Path)
    parser.add_argument("--independent-verifier", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    for source in (args.journal, args.source_report, args.independent_verifier):
        if not source.is_file() or source.is_symlink():
            raise ResearchConsoleError("missing or symlinked research input")
    def load(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))
    page = render(load(args.journal), load(args.source_report),
                  load(args.independent_verifier))
    publish_new_html(args.output, page)
    print("ASTRA_RESEARCH_TERMINAL_CREATED", str(args.output))
    print("RESEARCH_ONLY_NO_TRADING_AUTHORITY", hashlib.sha256(page.encode()).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
