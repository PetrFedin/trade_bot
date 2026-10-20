"""Export every modelled trade and decision from verified ASTRA research history.

Read-only, research-only. The exporter NEVER asserts real venue fills, execution
times, funding allocation per trade, broker authority or future profitability.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import tempfile
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tools.historical_execution_journal import (
    COSTS,
    MODELS,
    PERIODS,
    build_journal,
    date,
    number,
)
from tools.publish_research_receipt import (
    ReceiptPublicationError,
    publish_new_json_receipt,
)
from tools.research_control_tower import (
    TowerError,
    require_detached_journal_pin,
    validate,
)

SYMBOL = re.compile(r"[A-Z0-9]{3,24}")
ZERO = Decimal("0")
MAX_SOURCE_BYTES = 64 * 1024 * 1024
TRADE_FIELDS = (
    "period_id", "fee_bps_per_fill", "slippage_bps_per_side", "model_key",
    "symbol", "entry_bar_day_utc", "exit_bar_day_utc", "holding_daily_bars",
    "exit_reason", "modelled_quantity", "modelled_entry_execution_price",
    "modelled_exit_execution_price", "gross_pnl_before_costs_usdt",
    "entry_fee_usdt", "exit_fee_usdt", "slippage_cost_usdt",
    "net_pnl_excluding_funding_usdt", "ambiguous_intrabar_exit",
    "gap_through_stop", "execution_evidence_class",
)
POSITION_FIELDS = (
    "period_id", "fee_bps_per_fill", "slippage_bps_per_side", "model_key",
    "symbol", "entry_bar_day_utc", "last_source_bar_day_utc",
    "modelled_open_quantity", "modelled_entry_execution_price",
    "entry_fee_usdt", "last_daily_close_mark_price",
    "modelled_end_marked_value_usdt", "status", "execution_evidence_class",
)
DECISION_FIELDS = (
    "period_id", "fee_bps_per_fill", "slippage_bps_per_side", "model_key",
    "utc_model_bar_day", "selected_symbols", "entered_symbols",
    "selected_not_entered_symbols_reason_unverified",
    "modelled_prior_equity_usdt", "modelled_closing_equity_usdt",
    "modelled_funding_booked_on_day_usdt", "source_class",
)
FILENAME_FIELDS = (
    ("closed-trades.csv", TRADE_FIELDS),
    ("final-open-positions.csv", POSITION_FIELDS),
    ("daily-decisions.csv", DECISION_FIELDS),
)


class TapeError(ValueError):
    """A complete and provenance-linked research tape cannot be built."""


def safe_symbol(symbol: object, universe: list[str]) -> str:
    if (
        not isinstance(symbol, str) or not SYMBOL.fullmatch(symbol)
        or symbol not in universe
    ):
        raise TapeError("foreign or spreadsheet-unsafe modelled symbol")
    return symbol


def decimal(value: object) -> str:
    return str(number(value))


def day(value: object) -> str:
    return date(value).date().isoformat()


def _csv_bytes(fields: tuple[str, ...], rows: list[dict]) -> bytes:
    target = io.StringIO(newline="")
    writer = csv.DictWriter(
        target, fieldnames=fields, extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return target.getvalue().encode("utf-8")


def _link_new(target: Path, payload: bytes) -> None:
    """Create an immutable complete CSV without replacement, fail closed."""
    parent = target.parent
    if any(part.is_symlink() for part in (parent, *parent.parents)):
        raise TapeError("symlinked output parent")
    parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise TapeError("existing evidence target cannot be overwritten")
    fd, staged = tempfile.mkstemp(
        prefix=".astra-full-tape-", suffix=".stage", dir=parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(staged, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise TapeError("competing publisher created output") from exc
        except OSError as exc:
            raise TapeError("no safe atomic CSV publication") from exc
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def build_export(
    report: dict, verifier: dict, journal: dict, expected_journal_sha: str
) -> tuple[dict, dict[str, bytes]]:
    """Rebuild audited journal, then conserve EVERY trade, holding and day."""
    try:
        require_detached_journal_pin(journal, expected_journal_sha)
        validate(journal)
        reconstructed = build_journal(report, verifier)
    except (TowerError, ValueError, KeyError, TypeError) as exc:
        raise TapeError(f"unqualified source, verifier or research pin: {exc}") from exc
    if canonical_json_bytes(reconstructed) != canonical_json_bytes(journal):
        raise TapeError("replayed source disagrees with approved immutable journal")
    if journal["journal_sha256"] != expected_journal_sha:
        raise TapeError("separate journal admission failed")
    scenarios = {(row["period_id"], row["fee_bps_per_fill"],
                  row["slippage_bps_per_side"]): row
                 for row in report["comparisons"]}
    if set(scenarios) != {
        (p, f, s) for p in PERIODS for f, s in COSTS
    } or len(report["comparisons"]) != 6:
        raise TapeError("missing or duplicate source portfolio scenarios")
    models_from_journal = {
        (g["period_id"], g["fee_bps_per_fill"], g["slippage_bps_per_side"]): g
        for g in journal["comparisons"]
    }
    trade_rows: list[dict] = []
    open_rows: list[dict] = []
    decision_rows: list[dict] = []
    totals = {
        "closed_trades": 0,
        "terminal_open_positions": 0,
        "daily_model_decisions": 0,
        "entered_positions": 0,
        "selected_without_entry_observations": 0,
        "by_exit_reason": Counter(),
    }
    for period in PERIODS:
        for fee, slip in COSTS:
            group_key = (period, fee, slip)
            comparison = scenarios[group_key]
            universe = comparison["symbols"]
            if (
                not isinstance(universe, list) or len(universe) != len(set(universe))
                or any(not SYMBOL.fullmatch(str(s)) for s in universe)
            ):
                raise TapeError("unqualified portfolio universe")
            for model_key in MODELS:
                model = comparison["models"][model_key]
                verified = models_from_journal[group_key]["models"][model_key]
                prefix = {
                    "period_id": period,
                    "fee_bps_per_fill": fee,
                    "slippage_bps_per_side": slip,
                    "model_key": model_key,
                }
                expected_entries = set()
                funding_by_day = {
                    day(f["day_utc"]): decimal(f["net_funding_cash_usd"])
                    for f in model["funding_days"]
                }
                for decision in model["daily_equity"]:
                    stamp = day(decision["day_utc"])
                    selected, entered = (
                        decision["selected_symbols"], decision["entered_symbols"]
                    )
                    if (
                        len(selected) != len(set(selected))
                        or len(entered) != len(set(entered))
                        or len(entered) > 2 or not set(entered).issubset(selected)
                    ):
                        raise TapeError("selected/entered decision lineage not unique")
                    selected = [safe_symbol(x, universe) for x in selected]
                    entered = [safe_symbol(x, universe) for x in entered]
                    unentered = [x for x in selected if x not in entered]
                    expected_entries.update((symbol, stamp) for symbol in entered)
                    decision_rows.append({
                        **prefix,
                        "utc_model_bar_day": stamp,
                        "selected_symbols": "|".join(selected),
                        "entered_symbols": "|".join(entered),
                        "selected_not_entered_symbols_reason_unverified":
                            "|".join(unentered),
                        "modelled_prior_equity_usdt": decimal(
                            decision["prior_equity"]
                        ),
                        "modelled_closing_equity_usdt": decimal(
                            decision["closing_equity"]
                        ),
                        "modelled_funding_booked_on_day_usdt":
                            funding_by_day.get(stamp, "0"),
                        "source_class": "MODELLED_DAILY_DECISION",
                    })
                    totals["entered_positions"] += len(entered)
                    totals["selected_without_entry_observations"] += len(unentered)
                identities = set()
                realized = ZERO
                attributed = defaultdict(lambda: [0, ZERO])
                exits = Counter()
                for trade in model["trades"]:
                    symbol = safe_symbol(trade["symbol"], universe)
                    entered, exited = day(trade["entry_time"]), day(
                        trade["exit_time"]
                    )
                    identity = symbol, entered
                    if identity in identities or identity not in expected_entries:
                        raise TapeError("unmatched or duplicate closed-trade entry")
                    identities.add(identity)
                    net = number(trade["net_pnl"])
                    realized += net
                    attributed[symbol][0] += 1
                    attributed[symbol][1] += net
                    exits[trade["exit_reason"]] += 1
                    trade_rows.append({
                        **prefix, "symbol": symbol,
                        "entry_bar_day_utc": entered,
                        "exit_bar_day_utc": exited,
                        "holding_daily_bars": trade["holding_bars"],
                        "exit_reason": trade["exit_reason"],
                        "modelled_quantity": decimal(trade["quantity"]),
                        "modelled_entry_execution_price": decimal(
                            trade["entry_execution_price"]
                        ),
                        "modelled_exit_execution_price": decimal(
                            trade["exit_execution_price"]
                        ),
                        "gross_pnl_before_costs_usdt": decimal(
                            trade["gross_pnl_before_costs"]
                        ),
                        "entry_fee_usdt": decimal(trade["entry_fee"]),
                        "exit_fee_usdt": decimal(trade["exit_fee"]),
                        "slippage_cost_usdt": decimal(trade["slippage_cost"]),
                        "net_pnl_excluding_funding_usdt": str(net),
                        "ambiguous_intrabar_exit": str(
                            trade["ambiguous_intrabar_exit"]
                        ).lower(),
                        "gap_through_stop": str(
                            trade["gap_through_stop"]
                        ).lower(),
                        "execution_evidence_class": "MODELLED_DAILY_BAR",
                    })
                open_count = 0
                for item in model["final_open_positions"]:
                    symbol = safe_symbol(item["symbol"], universe)
                    entered = day(item["entry_time"])
                    identity = symbol, entered
                    if identity not in expected_entries or identity in identities:
                        raise TapeError("duplicate or unrecorded terminal position")
                    identities.add(identity)
                    open_count += 1
                    open_rows.append({
                        **prefix, "symbol": symbol,
                        "entry_bar_day_utc": entered,
                        "last_source_bar_day_utc": day(
                            comparison["last_source_day_utc"]
                        ),
                        "modelled_open_quantity": decimal(item["quantity"]),
                        "modelled_entry_execution_price": decimal(
                            item["entry_execution_price"]
                        ),
                        "entry_fee_usdt": decimal(item["entry_fee"]),
                        "last_daily_close_mark_price": decimal(
                            item["final_mark"]
                        ),
                        "modelled_end_marked_value_usdt": str(
                            number(item["quantity"]) * number(item["final_mark"])
                        ),
                        "status": "OPEN_AT_END_NOT_EXECUTED_EXIT",
                        "execution_evidence_class": "MODELLED_DAILY_BAR",
                    })
                if identities != expected_entries:
                    raise TapeError("model entry records do not conserve")
                if (
                    len(model["trades"]) != verified["closed_trades"]
                    or len(model["daily_equity"]) != verified["trading_days"]
                ):
                    raise TapeError("journal-to-export trade or day population drift")
                if abs(realized - number(verified["closed_trade_net_usd"])) > Decimal(
                    "0.000001"
                ):
                    raise TapeError("journal-to-export net PnL differs")
                if dict(sorted(exits.items())) != verified["exit_reasons"]:
                    raise TapeError("journal exit-reason census differs")
                for sym, info in verified["by_symbol"].items():
                    if (
                        attributed[sym][0] != info["closed_trades"]
                        or abs(
                            attributed[sym][1] - number(
                                info["closed_trade_net_usd"]
                            )
                        ) > Decimal("0.000001")
                    ):
                        raise TapeError("journal instrument attribution differs")
                totals["closed_trades"] += len(model["trades"])
                totals["terminal_open_positions"] += open_count
                totals["daily_model_decisions"] += len(model["daily_equity"])
                totals["by_exit_reason"].update(exits)
    if totals["entered_positions"] != (
        totals["closed_trades"] + totals["terminal_open_positions"]
    ):
        raise TapeError("global decision-to-position population mismatch")
    if totals["daily_model_decisions"] != verifier["daily_equity_rows_checked"]:
        raise TapeError("independent auditor daily rows mismatch")
    if totals["closed_trades"] != verifier["closed_trades_checked"]:
        raise TapeError("independent auditor closed trades mismatch")
    data = {
        "closed-trades.csv": _csv_bytes(TRADE_FIELDS, trade_rows),
        "final-open-positions.csv": _csv_bytes(POSITION_FIELDS, open_rows),
        "daily-decisions.csv": _csv_bytes(DECISION_FIELDS, decision_rows),
    }
    manifest = {
        "schema_version": "astra-complete-modelled-trade-ledger-v1",
        "evaluation_class": "REUSED_INSPECTED_HISTORY_DIAGNOSTIC_ONLY",
        "source_report_sha256": journal["source_report_sha256"],
        "independent_auditor_sha256": journal["independent_auditor_sha256"],
        "journal_sha256": journal["journal_sha256"],
        "model_count": journal["model_count"],
        "scenario_count": journal["scenario_count"],
        "csv_files": {
            filename: {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size_bytes": len(payload),
                "rows_excluding_header": {
                    "closed-trades.csv": len(trade_rows),
                    "final-open-positions.csv": len(open_rows),
                    "daily-decisions.csv": len(decision_rows),
                }[filename],
            }
            for filename, payload in data.items()
        },
        "totals": {
            **{k: v for k, v in totals.items() if k != "by_exit_reason"},
            "by_exit_reason": dict(sorted(totals["by_exit_reason"].items())),
        },
        "trade_pnl_excludes_funding": True,
        "daily_bar_timestamps_are_not_execution_timestamps": True,
        "selected_without_entry_does_not_claim_block_reason": True,
        "actual_exchange_fills_verified": False,
        "funding_exact_exchange_timing_verified": False,
        "untouched_holdout": False,
        "profitability_proven": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
        "interpretation": (
            "Modelled per-exit-day closed PnL excludes funding, and cannot be "
            "summed with mark-to-market capital returns. Same-day bar labels "
            "do not establish actual order or fill times. Decisions do not "
            "contain verified no-entry rejection reasons. Open positions at "
            "end have marked values only; no hypothetical closing fills."
        ),
    }
    manifest["manifest_sha256"] = hashlib.sha256(
        canonical_json_bytes(manifest)
    ).hexdigest()
    return manifest, data


def _read_json(path: Path, *, max_bytes: int) -> dict:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > max_bytes:
        raise TapeError("missing, symlinked or oversized research input")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise TapeError("research input must be JSON object")
    return result


def publish_bundle(path: Path, manifest: dict, data: dict[str, bytes]) -> None:
    """Publish all CSV first and an immutable complete-manifest marker LAST."""
    target = Path(path)
    if any(item.is_symlink() for item in (target, *target.parents)):
        raise TapeError("symlinked research destination")
    if (
        set(data) != {filename for filename, _ in FILENAME_FIELDS}
        or any((target / name).exists() or (target / name).is_symlink()
               for name in (*data, "complete-manifest.json"))
    ):
        raise TapeError("research export already exists; no overwrite")
    for filename, fields in FILENAME_FIELDS:
        if data[filename].split(b"\n", 1)[0] != (
            ",".join(fields).encode("utf-8")
        ):
            raise TapeError("unexpected CSV schema; publication rejected")
        if hashlib.sha256(data[filename]).hexdigest() != (
            manifest["csv_files"][filename]["sha256"]
        ):
            raise TapeError("CSV digest differs from manifest")
    for filename, _ in FILENAME_FIELDS:
        _link_new(target / filename, data[filename])
    try:
        publish_new_json_receipt(target / "complete-manifest.json", manifest)
    except ReceiptPublicationError as exc:
        raise TapeError(f"complete research publication refused: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--independent-verifier", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--expected-journal-sha256", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    source = _read_json(args.report, max_bytes=MAX_SOURCE_BYTES)
    receipt = _read_json(args.independent_verifier, max_bytes=1024 * 1024)
    journal = _read_json(args.journal, max_bytes=24 * 1024 * 1024)
    manifest, data = build_export(
        source, receipt, journal, args.expected_journal_sha256
    )
    publish_bundle(args.output_dir, manifest, data)
    print("ASTRA_FULL_MODEL_TAPE_MANIFEST_SHA256", manifest["manifest_sha256"])
    print("ASTRA_FULL_MODEL_TAPE_CLOSED_TRADES", manifest["totals"]["closed_trades"])
    print("ASTRA_FULL_MODEL_TAPE_DECISION_DAYS", manifest["totals"]["daily_model_decisions"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
