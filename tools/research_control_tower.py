"""Static read-only ASTRA research dashboard from audited 36-model trade journal.

No brokerage API, signal execution, external requests, JavaScript or live controls.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.qualification.portable_artifact_codec import canonical_json_bytes
from tools.historical_execution_journal import COSTS, MODELS, PERIODS

D = Decimal
ZERO = D("0")
CASH = D("10000")
PRIMARY = "HOURLY_COLLISION_REFINED:UPPER_COST"
SHA_PATTERN = re.compile(r"[0-9a-f]{64}")
LABELS = {
    "six_full_history": "Шесть монет / полная история",
    "six_matched_window": "Шесть монет / общий период",
    "broad21_matched_window": "21 монета / общий период",
}


class TowerError(ValueError):
    """Unqualified evidence cannot reach the research user interface."""


def money(value: object) -> Decimal:
    if isinstance(value, bool):
        raise TowerError("boolean cannot be a money value")
    try:
        out = D(str(value))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise TowerError("invalid decimal input") from exc
    if not out.is_finite():
        raise TowerError("non-finite research metric")
    return out


def count(value: object) -> int:
    if type(value) is not int or value < 0:
        raise TowerError("invalid count or boolean-valued count")
    return value


def close(left: object, right: object) -> None:
    if abs(money(left) - money(right)) > D("0.00000001"):
        raise TowerError("model monetary reconciliation mismatch")


def utc(value: str, clock: bool = False) -> date:
    if not isinstance(value, str):
        raise TowerError("date must be ISO 8601")
    try:
        if clock:
            t = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if t.utcoffset() != timedelta(0) or t.time().isoformat() != "00:00:00":
                raise TowerError("historical clock must be UTC midnight")
            return t.date()
        return date.fromisoformat(value)
    except ValueError as exc:
        raise TowerError("malformed research timestamp") from exc


def verify_model(model: dict, model_id: str) -> None:
    if not isinstance(model, dict) or model.get("model_key") != model_id:
        raise TowerError("wrong model identifier")
    start = utc(model["first_execution_day_utc"], True)
    end = utc(model["last_source_day_utc"], True)
    if count(model["trading_days"]) != (end - start).days + 1:
        raise TowerError("missing source trading days")
    tape = model["windows"]
    if not isinstance(tape, list) or not tape:
        raise TowerError("empty daily equity windows")
    cursor, equity = start, CASH
    trades, funding = 0, ZERO
    for index, interval in enumerate(tape):
        first, last = utc(interval["start_utc"]), utc(interval["end_utc"])
        days = count(interval["days"])
        if (
            first != cursor or days != (last - first).days + 1
            or days < 1 or days > 90
            or type(interval["complete_window"]) is not bool
            or interval["complete_window"] != (days == 90)
            or (index < len(tape) - 1 and days != 90)
        ):
            raise TowerError("overlapping/gapped/mislabeled 90-day intervals")
        initial, terminal = (
            money(interval["starting_equity_usd"]),
            money(interval["ending_equity_usd"]),
        )
        if initial <= ZERO or terminal <= ZERO:
            raise TowerError("nonpositive portfolio cash valuation")
        close(initial, equity)
        close((terminal / initial - 1) * 100, interval["equity_return_pct"])
        drawdown = money(interval["max_daily_close_drawdown_pct"])
        if not ZERO <= drawdown <= D("100"):
            raise TowerError("invalid historical drawdown")
        trades += count(interval["closed_trade_count"])
        if count(interval["wins"]) > interval["closed_trade_count"]:
            raise TowerError("more winning trades than closed trades")
        funding += money(interval["funding_cash_usd_booked"])
        cursor, equity = last + timedelta(days=1), terminal
    if cursor != end + timedelta(days=1):
        raise TowerError("missing terminal equity window")
    close(equity, model["ending_equity_usd"])
    close((equity / CASH - 1) * 100, model["portfolio_return_pct"])
    close(funding, model["funding_cash_net_usd"])
    if count(model["closed_trades"]) != trades:
        raise TowerError("incomplete closed-trade population")
    symbols = model["by_symbol"]
    if not isinstance(symbols, dict):
        raise TowerError("missing symbol attribution")
    if sum(count(item["closed_trades"]) for item in symbols.values()) != trades:
        raise TowerError("symbol trade counts do not conserve")
    close(
        sum((money(item["closed_trade_net_usd"]) for item in symbols.values()), ZERO),
        model["closed_trade_net_usd"],
    )
    for field in ("worst_closed_trades", "best_closed_trades"):
        rows = model[field]
        if not isinstance(rows, list) or len(rows) > 5:
            raise TowerError("invalid example trade coverage")
        for row in rows:
            for numeric in (
                "net_pnl_usd", "entry_fee_usd", "exit_fee_usd", "slippage_cost_usd"
            ):
                money(row[numeric])


def validate(source: dict) -> dict:
    if (
        not isinstance(source, dict)
        or source.get("schema_version") != "astra-trade-journal-reused-history-v1"
        or source.get("evaluation_class") != "REUSED_INSPECTED_HISTORY_DIAGNOSTIC_ONLY"
        or source.get("modelled_trading_only") is not True
    ):
        raise TowerError("unsupported journal or false historical authority")
    for flag in (
        "actual_exchange_fills_verified", "account_funding_ledger_verified",
        "settlement_mark_price_verified", "untouched_holdout",
        "profitability_proven", "strategy_promotion_allowed", "live_trading_allowed",
    ):
        if source.get(flag) is not False:
            raise TowerError(f"unsafe trading claim: {flag}")
    for name in ("journal_sha256", "source_report_sha256", "independent_auditor_sha256"):
        if not isinstance(source.get(name), str) or not SHA_PATTERN.fullmatch(source[name]):
            raise TowerError("missing complete provenance SHA-256")
    digest = hashlib.sha256(canonical_json_bytes({
        key: val for key, val in source.items() if key != "journal_sha256"
    })).hexdigest()
    if digest != source["journal_sha256"]:
        raise TowerError("modified historical journal")
    if (
        count(source["model_count"]) != 36
        or count(source["scenario_count"]) != 6
        or count(source["independently_reconciled_models"]) != 36
        or source.get("window_size_days") != 90
    ):
        raise TowerError("incomplete 36-model audit")
    expected = {(p, f, s) for p in PERIODS for f, s in COSTS}
    groups = {}
    if len(source["comparisons"]) != 6:
        raise TowerError("historical matrix incomplete")
    for item in source["comparisons"]:
        key = item["period_id"], item["fee_bps_per_fill"], item["slippage_bps_per_side"]
        if key not in expected or key in groups:
            raise TowerError("duplicate or unknown research configuration")
        if set(item["models"]) != set(MODELS):
            raise TowerError("missing model or funding mode")
        for name in MODELS:
            verify_model(item["models"][name], name)
        groups[key] = item
    diffs = source.get("matched_universe_differences")
    if not isinstance(diffs, list) or len(diffs) != 12:
        raise TowerError("missing matched universe pairs")
    seen = set()
    for row in diffs:
        ident = row["fee_bps_per_fill"], row["slippage_bps_per_side"], row["model_key"]
        if (
            ident in seen or ident[:2] not in COSTS or ident[2] not in MODELS
            or row.get("same_dates_verified") is not True
        ):
            raise TowerError("invalid matched study")
        seen.add(ident)
        a = groups[("six_matched_window", *ident[:2])]["models"][ident[2]]
        b = groups[("broad21_matched_window", *ident[:2])]["models"][ident[2]]
        for field in ("first_execution_day_utc", "last_source_day_utc", "trading_days"):
            if a[field] != b[field]:
                raise TowerError("different comparison chronologies")
        close(a["portfolio_return_pct"], row["six_return_pct"])
        close(b["portfolio_return_pct"], row["broad21_return_pct"])
        close(
            money(a["portfolio_return_pct"]) - money(b["portfolio_return_pct"]),
            row["six_minus_broad21_percentage_points"],
        )
    return groups


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def fmt(value: object) -> str:
    return f"{money(value):,.2f}".replace(",", " ")


def metric(value: object, unit: str = "%") -> str:
    num = money(value)
    mark = "+" if num > 0 else ""
    cls = "negative" if num < 0 else "positive"
    return f'<span class="{cls}">{mark}{fmt(num)}{esc(unit)}</span>'


def trade_table(title: str, trades: list[dict]) -> str:
    body = []
    for item in trades:
        body.append(
            "<tr><td>" + esc(item["symbol"]) + "</td><td>"
            + esc(item["entry_utc"])[:10] + "</td><td>"
            + esc(item["exit_reason"]) + "</td><td class='num'>"
            + metric(item["net_pnl_usd"], " USDT") + "</td><td class='num'>"
            + fmt(item["entry_fee_usd"]) + "</td><td class='num'>"
            + fmt(item["exit_fee_usd"]) + "</td><td class='num'>"
            + fmt(item["slippage_cost_usd"]) + "</td></tr>"
        )
    return (
        f"<h4>{esc(title)}</h4><div class='scroll'><table><thead><tr>"
        "<th>Монета</th><th>Вход UTC</th><th>Причина</th><th class='num'>Net</th>"
        "<th class='num'>Fee In</th><th class='num'>Fee Out</th>"
        "<th class='num'>Slippage</th></tr></thead><tbody>"
        + "".join(body) + "</tbody></table></div>"
    )


def details(model: dict) -> str:
    rows = []
    for w in model["windows"]:
        rows.append(
            "<tr><td>" + esc(w["start_utc"]) + " — " + esc(w["end_utc"])
            + "</td><td>" + ("Полный" if w["complete_window"] else "Неполный")
            + "</td><td class='num'>" + metric(w["equity_return_pct"])
            + "</td><td class='num'>" + fmt(w["starting_equity_usd"])
            + "</td><td class='num'>" + fmt(w["ending_equity_usd"])
            + "</td><td class='num'>" + esc(w["closed_trade_count"])
            + "</td></tr>"
        )
    coins = []
    for name, value in sorted(
        model["by_symbol"].items(),
        key=lambda entry: money(entry[1]["closed_trade_net_usd"])
    ):
        coins.append(
            "<tr><td>" + esc(name) + "</td><td class='num'>"
            + esc(value["closed_trades"]) + "</td><td class='num'>"
            + metric(value["closed_trade_net_usd"], " USDT") + "</td></tr>"
        )
    reasons = ", ".join(
        esc(name) + ": " + esc(num) for name, num in sorted(model["exit_reasons"].items())
    )
    return (
        "<div class='cards'>"
        "<div class='card'>Результат<div class='big'>"
        + metric(model["portfolio_return_pct"]) + "</div></div>"
        "<div class='card'>Закрыто сделок<div class='big'>"
        + esc(model["closed_trades"]) + "</div></div>"
        "<div class='card'>Дневная макс. просадка<div class='big'>"
        + fmt(model["max_drawdown_pct"]) + "%</div></div>"
        "<div class='card'>Funding cash<div class='big'>"
        + metric(model["funding_cash_net_usd"], " USDT") + "</div></div></div>"
        "<h4>Непересекающиеся интервалы по 90 дней</h4>"
        "<p class='sub'>Доходность капитала включает переоценку открытых позиций.</p>"
        "<div class='scroll'><table><thead><tr><th>Период UTC</th><th>Статус</th>"
        "<th class='num'>Доход</th><th class='num'>Капитал до</th>"
        "<th class='num'>Капитал после</th><th class='num'>Сделок</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
        "<h4>Причины закрытий</h4><p class='sub'>" + reasons + "</p>"
        "<h4>Реализованный PnL по инструментам</h4>"
        "<div class='scroll'><table><thead><tr><th>Монета</th>"
        "<th class='num'>Сделок</th><th class='num'>Closed net PnL</th>"
        "</tr></thead><tbody>" + "".join(coins) + "</tbody></table></div>"
        + trade_table("Наибольшие модельные убытки", model["worst_closed_trades"])
        + trade_table("Наибольшие модельные прибыли", model["best_closed_trades"])
    )


CSS = """
:root{color-scheme:dark;--bg:#0b131b;--panel:#142432;--line:#304655;
--ink:#eef4f6;--muted:#9cabb8;--accent:#8adfe3;--good:#9ce6b6;--bad:#efa1a1}
*{box-sizing:border-box}body{background:var(--bg);color:var(--ink);margin:0;
font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1220px;margin:auto;padding:32px 22px 80px}
h1{font-size:clamp(28px,4vw,48px);letter-spacing:-.03em;line-height:1.15}
h2{margin:34px 0 12px;font-size:23px}
p.lead,.sub,footer{color:var(--muted)}.sub{font-size:12px}
.kicker{color:var(--accent);text-transform:uppercase;letter-spacing:.15em;font-size:11px}
nav{display:flex;gap:10px;margin:23px 0;flex-wrap:wrap}
nav a{border:1px solid var(--line);border-radius:9px;padding:6px 10px;
text-decoration:none;color:var(--accent)}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.card,.panel,details{background:var(--panel);border:1px solid var(--line);
border-radius:14px;padding:16px}
.big{font-size:29px;font-weight:740;font-variant-numeric:tabular-nums}
.positive{color:var(--good)}.negative{color:var(--bad)}
.risk{background:#31251a;border:1px solid #926c42;color:#fae0b9;
border-radius:13px;padding:15px}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px;
font-variant-numeric:tabular-nums}
td,th{text-align:left;padding:9px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-size:11px}.num{text-align:right;white-space:nowrap}
details{margin:10px 0}details details{background:#1b2c3b}
summary{cursor:pointer;font-weight:650}code{overflow-wrap:anywhere;font-size:11px}
footer{margin-top:38px;border-top:1px solid var(--line);padding-top:20px}
a:focus-visible,summary:focus-visible{outline:3px solid var(--accent)}
@media(max-width:850px){.cards{grid-template-columns:repeat(2,1fr)}}
@media(max-width:480px){.cards{grid-template-columns:1fr}
main{padding:17px 12px}td,th{padding:6px}.card,details,.panel{padding:11px}}
"""


def render(source: dict) -> str:
    groups = validate(source)
    a = groups[("six_matched_window", "8", "5")]["models"][PRIMARY]
    b = groups[("broad21_matched_window", "8", "5")]["models"][PRIMARY]
    complete = [w for w in a["windows"] if w["complete_window"]]
    wins = sum(money(w["equity_return_pct"]) > ZERO for w in complete)
    parts = [
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta http-equiv='Content-Security-Policy' content=\""
        "default-src 'none';style-src 'unsafe-inline';base-uri 'none';form-action 'none'\">"
        "<title>ASTRA · Research Control Tower</title><style>", CSS,
        "</style></head><body><main><p class='kicker'>ASTRA · Research Evidence</p>",
        "<h1>Research Control Tower</h1>",
        "<p class='lead'>Историческая торговля по монетам, причинам выхода, "
        "стоимости сделок и изменениям капитала. "
        "Эта панель не может управлять ордерами.</p>",
        "<div class='risk'><strong>PROFITABILITY_NOT_PROVEN / LIVE HOLD</strong>"
        "<p>Биржевые исполнения, settlement Mark Price и будущий независимый "
        "holdout не подтверждены. Историческая выборка уже изучалась.</p></div>",
        "<nav><a href='#overview'>Обзор</a><a href='#comparison'>Сравнение</a>"
        "<a href='#models'>36 моделей</a><a href='#proof'>Аудит</a></nav>",
        "<section id='overview'><h2>Общая картина · 8 / 5 б.п.</h2>",
        "<p class='sub'>Hourly-refined и funding UPPER_COST на одном календаре.</p>",
        "<div class='cards'><div class='card'>6 монет<div class='big'>",
        metric(a["portfolio_return_pct"]), "</div></div>",
        "<div class='card'>21 монета<div class='big'>",
        metric(b["portfolio_return_pct"]), "</div></div>",
        "<div class='card'>Положительные 90 дней<div class='big'>",
        f"{wins}/{len(complete)}", "</div></div>",
        "<div class='card'>Аудировано моделей<div class='big'>36/36</div></div>",
        "</div></section><section id='comparison'><h2>Шесть против 21 монеты</h2>",
        "<p class='lead'>Сопоставление одинакового периода. "
        "Это не оценка будущей альфы.</p>",
        "<div class='panel scroll'><table><thead><tr><th>Fee / Slippage</th>"
        "<th class='num'>6 монет</th><th class='num'>21 монета</th>"
        "<th class='num'>Разница п.п.</th></tr></thead><tbody>",
    ]
    for fee, slip in COSTS:
        m6 = groups[("six_matched_window", fee, slip)]["models"][PRIMARY]
        m21 = groups[("broad21_matched_window", fee, slip)]["models"][PRIMARY]
        left, right = money(m6["portfolio_return_pct"]), money(
            m21["portfolio_return_pct"]
        )
        parts.append(
            "<tr><td>" + esc(fee) + " / " + esc(slip)
            + "</td><td class='num'>" + metric(left)
            + "</td><td class='num'>" + metric(right)
            + "</td><td class='num'>" + metric(left - right, " п.п.")
            + "</td></tr>"
        )
    parts.extend([
        "</tbody></table></div></section><section id='models'>",
        "<h2>36 исторических сценариев</h2>",
        "<p class='lead'>Раскрывайте периоды и варианты исполнения. "
        "Конечные неполные интервалы помечены отдельно.</p>",
    ])
    for group in source["comparisons"]:
        name = LABELS[group["period_id"]]
        f, s = group["fee_bps_per_fill"], group["slippage_bps_per_side"]
        parts.append(
            "<details><summary>" + esc(name) + " · комиссия/slippage "
            + esc(f) + "/" + esc(s) + " · 6 моделей</summary>"
        )
        for model_id in MODELS:
            item = group["models"][model_id]
            parts.append(
                "<details><summary>" + esc(model_id) + " · "
                + metric(item["portfolio_return_pct"]) + " · "
                + esc(item["closed_trades"]) + " сделок</summary>"
                + details(item) + "</details>"
            )
        parts.append("</details>")
    parts.extend([
        "</section><section id='proof'><h2>Достоверность и пределы</h2>"
        "<div class='panel'><p>Проверено: хеши входного исторического журнала, "
        "связь с независимым денежным аудитором, 36 сценариев и календарная "
        "согласованность результатов.</p><p>Не подтверждено: подлинные биржевые "
        "fills, точный funding по Mark Price, ликвидность стакана, margin и "
        "долгосрочная будущая доходность.</p><p class='sub'>"
        "Нет JavaScript, внешних HTTP-запросов и торговых команд.</p></div>",
        "<footer><p>Исторический исходник: <code>",
        esc(source["source_report_sha256"]),
        "</code></p><p>Аудитор: <code>",
        esc(source["independent_auditor_sha256"]),
        "</code></p><p>Журнал: <code>",
        esc(source["journal_sha256"]),
        "</code></p></footer></section></main></body></html>",
    ])
    return "".join(parts)


def publish(destination: Path, page: str) -> None:
    """Atomic no-overwrite same-directory hard-link publication with fsync."""
    target = Path(destination)
    directory = target.parent
    if any(x.is_symlink() for x in (directory, *directory.parents)):
        raise TowerError("symlinked destination parent forbidden")
    directory.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise TowerError("output already exists; never replace")
    fd, staged = tempfile.mkstemp(prefix=".astra-tower-", suffix=".html", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(page.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(staged, target, follow_symlinks=False)
        except FileExistsError as exc:
            raise TowerError("destination appeared during publish") from exc
        except OSError as exc:
            raise TowerError("no safe atomic HTML publication") from exc
    finally:
        if os.path.exists(staged):
            os.unlink(staged)
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if not args.journal.is_file() or args.journal.is_symlink():
        raise TowerError("historical source missing or symlinked")
    if args.journal.stat().st_size > 24 * 1024 * 1024:
        raise TowerError("unqualified oversized research source")
    source = json.loads(args.journal.read_text(encoding="utf-8"))
    page = render(source)
    publish(args.output, page)
    print("ASTRA_TOWER_HTML_SHA256", hashlib.sha256(page.encode()).hexdigest())
    print("ASTRA_TOWER_JOURNAL_SHA256", source["journal_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
