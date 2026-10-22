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


HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>ASTRA / Research Terminal</title>
<style>
:root{
  --bg:#0b1217;--surface:#131e25;--subtle:#17242c;
  --line:#293a44;--text:#e5ecef;--muted:#9cacb6;
  --accent:#8cc5bf;--gain:#83ceb1;--loss:#edaaa0;
  --mono:ui-monospace,SFMono-Regular,Consolas,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif}
.shell{max-width:1440px;margin:auto;padding:32px clamp(18px,4vw,56px) 64px}
.topline{display:flex;align-items:center;justify-content:space-between;gap:16px;
  padding-bottom:16px;border-bottom:1px solid var(--line)}
.brand{font-weight:800;letter-spacing:.16em;font-size:15px}
.scope{color:var(--muted);font:11px var(--mono);letter-spacing:.04em}
.scope strong{color:var(--loss);font-weight:600}
.toplinks{display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.toplinks a{font:11px var(--mono);color:var(--accent);text-decoration:underline;text-underline-offset:4px}
.toplinks a:focus-visible{outline:2px solid var(--accent);outline-offset:4px}
header{padding:33px 0 29px}
h1{font-size:clamp(27px,3.4vw,42px);line-height:1.13;letter-spacing:-.035em;
  margin:0 0 10px;font-weight:620}
header p{color:var(--muted);margin:0;max-width:83ch;font-size:14px}
.provenance{font:11px/1.7 var(--mono);color:var(--muted);border-top:1px solid var(--line);
  padding-top:16px;word-break:break-word}
.provenance b{color:var(--accent);font-weight:500}
.controls{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:14px;
  padding:22px 0 24px;border-top:1px solid var(--line)}
.control label{font:11px var(--mono);letter-spacing:.055em;color:var(--muted);
  display:block;margin-bottom:8px}
select{background:var(--surface);color:var(--text);border:1px solid #3b535f;
  width:100%;padding:11px 32px 11px 12px;border-radius:2px;font:13px inherit;
  font-family:inherit;min-height:44px}
select:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.context{display:flex;align-items:baseline;flex-wrap:wrap;gap:10px;margin-bottom:14px;
  color:var(--muted);font:11px var(--mono)}
.context span+span:before{content:"/";color:var(--line);margin-right:10px}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));border:1px solid var(--line);
  background:var(--line);gap:1px;margin-bottom:26px}
.stat{background:var(--surface);padding:22px 20px;min-width:0}
.stat .caption{font:11px var(--mono);color:var(--muted);margin-bottom:12px}
.stat .value{font-size:clamp(19px,2.2vw,30px);font-variant-numeric:tabular-nums;
  letter-spacing:-.025em;overflow-wrap:anywhere}
.positive{color:var(--gain)!important}.negative{color:var(--loss)!important}
.board{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(300px,1fr);
  gap:18px;margin-bottom:18px}
.panel{background:var(--surface);border:1px solid var(--line);min-width:0}
.panel h2{font-size:15px;letter-spacing:-.01em;font-weight:600;margin:0;
  padding:17px 20px;border-bottom:1px solid var(--line)}
.panel .note{padding:12px 20px;color:var(--muted);font-size:12px;
  border-bottom:1px solid var(--line)}
.window-list{padding:17px 20px 22px}
.bar-row{display:grid;grid-template-columns:110px minmax(40px,1fr) 92px;
  align-items:center;gap:14px;padding:7px 0;font:12px var(--mono)}
.bar-row .lab{color:var(--muted)}
.bar-row strong{text-align:right;font-weight:500;font-variant-numeric:tabular-nums}
.track{position:relative;height:20px;background:var(--subtle);border-radius:1px}
.track:after{content:"";position:absolute;left:50%;width:1px;top:0;bottom:0;
  background:#627681}
.bar{position:absolute;top:5px;bottom:5px;min-width:1px}
.bar.positive{background:var(--gain)}.bar.negative{background:var(--loss)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;text-transform:uppercase;font:10px var(--mono);letter-spacing:.04em;
  color:var(--muted);font-weight:500;background:var(--subtle)}
td,th{padding:12px 14px;border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:0}
td{color:var(--text)}
th.right,td.right{text-align:right;font-variant-numeric:tabular-nums}
.scroll{overflow:auto;max-width:100%}
.ledger{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:18px}
.ledger .scroll{max-height:470px;overflow:auto}
.ledger table{min-width:490px}
.notes{margin:20px 0 0;border-top:1px solid var(--line);padding:17px 0;
  color:var(--muted);font-size:12px;line-height:1.65}
.notes strong{color:var(--text);font-weight:500}
footer{font:10px var(--mono);color:var(--muted);border-top:1px solid var(--line);
  padding-top:16px;display:flex;flex-wrap:wrap;gap:12px;justify-content:space-between}
.empty{padding:20px;color:var(--muted)}
@media(max-width:900px){.controls{grid-template-columns:repeat(2,minmax(0,1fr))}
  .board{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}}
@media(max-width:620px){.shell{padding:20px 14px 35px}
  .topline{align-items:flex-start;flex-direction:column}
  .controls{grid-template-columns:1fr 1fr;gap:10px}
  .stat{padding:16px 12px}.stat .value{font-size:20px}
  .ledger{grid-template-columns:1fr}
  .bar-row{grid-template-columns:86px minmax(35px,1fr) 75px;gap:8px;font-size:11px}
  td,th{padding:10px}.panel h2{padding:14px 12px}
  .window-list{padding:14px 12px}.panel .note{padding:10px 12px}}
@media(prefers-reduced-motion:reduce){*{scroll-behavior:auto!important}}
</style>
</head>
<body>
<div class="shell">
<div class="topline">
  <div class="brand">ASTRA <span class="scope">/ RESEARCH</span></div>
  <div class="toplinks"><a href="operator-status.html">Контур допуска / main</a>
    <div class="scope"><strong>NOT FOR ORDER EXECUTION</strong> · Архивная модель · Только чтение</div>
  </div>
</div>
<header>
  <h1>Аналитика торговых решений</h1>
  <p>Исторический путь капитала, исполнение внутри свечи, funding, причины выхода
     и сравнение инструментов. Все значения — расчётные результаты замороженной
     исследовательской модели. Не котировки в реальном времени.</p>
</header>
<div class="controls" aria-label="Фильтры исторических моделей">
  <div class="control"><label for="period">Вселенная активов</label>
    <select id="period">
      <option value="six_full_history">6 монет · полная история</option>
      <option value="six_matched_window" selected>6 монет · общий период</option>
      <option value="broad21_matched_window">21 монета · общий период</option>
    </select></div>
  <div class="control"><label for="cost">Комиссия / проскальзывание, б.п.</label>
    <select id="cost"><option value="8|5" selected>8 / 5</option>
      <option value="10|10">10 / 10</option></select></div>
  <div class="control"><label for="exit">Модель внутри свечи</label>
    <select id="exit"><option value="HOURLY_COLLISION_REFINED" selected>Часовая реконструкция</option>
      <option value="DAILY_STOP_FIRST">Дневной Stop First</option></select></div>
  <div class="control"><label for="funding">Funding</label>
    <select id="funding"><option value="UPPER_COST" selected>Консервативный сценарий</option>
      <option value="LOWER_COST">Нижняя граница модели</option>
      <option value="NO_FUNDING">Без funding · контроль</option></select></div>
</div>
<div class="context" aria-live="polite"><span id="dates">__INITIAL_DATES__</span>
  <span>Стартовый капитал $10 000</span><span id="selection">Strategy A</span></div>
<div class="stats">
  <div class="stat"><div class="caption">Доходность за весь период</div>
    <div class="value" id="return">__INITIAL_RETURN__</div></div>
  <div class="stat"><div class="caption">Конечный капитал, USDT</div>
    <div class="value" id="equity">__INITIAL_EQUITY__</div></div>
  <div class="stat"><div class="caption">Просадка по закрытию дня</div>
    <div class="value" id="drawdown">__INITIAL_DRAWDOWN__</div></div>
  <div class="stat"><div class="caption">Закрытых модельных сделок</div>
    <div class="value" id="trades">__INITIAL_TRADES__</div></div>
</div>
<div class="board">
  <section class="panel">
    <h2>Доходность по 90-дневным окнам</h2>
    <div class="note">Непересекающиеся участки ранее изученной истории. Последний неполный период обозначен отдельно.</div>
    <div id="windows" class="window-list" aria-live="polite"></div>
  </section>
  <section class="panel">
    <h2>Сверка модельных денежных потоков</h2>
    <div class="scroll"><table><tbody>
      <tr><th scope="row">Чистый итог закрытых сделок</th><td class="right" id="net_closed">—</td></tr>
      <tr><th scope="row">Комиссии закрытых сделок</th><td class="right" id="fees">—</td></tr>
      <tr><th scope="row">Проскальзывание закрытых сделок</th><td class="right" id="slippage">—</td></tr>
      <tr><th scope="row">Модельный funding</th><td class="right" id="funding_cash">—</td></tr>
      <tr><th scope="row">6 монет, общая выборка</th><td class="right" id="six_return">—</td></tr>
      <tr><th scope="row">21 монета, общая выборка</th><td class="right" id="broad_return">—</td></tr>
      <tr><th scope="row">Разница, процентные пункты</th><td class="right" id="difference">—</td></tr>
    </tbody></table></div>
    <div class="note">Закрытые сделки нельзя суммировать с доходностью портфеля: открытые позиции переоцениваются отдельно. Сравнение 6/21 — только на совпадающих датах.</div>
    <h2>Результативность закрытых сделок</h2>
    <div class="scroll"><table><tbody>
      <tr><th scope="row">Доля прибыльных закрытий</th><td class="right" id="winrate">—</td></tr>
      <tr><th scope="row">Profit Factor, без funding</th><td class="right" id="pf">—</td></tr>
      <tr><th scope="row">Средний прибыльный результат, USDT</th><td class="right" id="avg_win">—</td></tr>
      <tr><th scope="row">Средний убыточный результат, USDT</th><td class="right" id="avg_loss">—</td></tr>
      <tr><th scope="row">Средний итог закрытия, USDT</th><td class="right" id="avg_trade">—</td></tr>
      <tr><th scope="row">Серия убыточных закрытий*</th><td class="right" id="loss_streak">—</td></tr>
    </tbody></table></div>
    <div class="note">*При совпадении даты порядок определяется названием инструмента; это не реальная внутридневная последовательность исполнений. Отсутствующее значение показано как «н/д», а не ноль.</div>
  </section>
</div>
<div class="board">
  <section class="panel"><h2>Атрибуция по монетам</h2>
    <div class="scroll"><table><thead><tr><th>Инструмент</th>
      <th class="right">Сделок</th><th class="right">Прибыльных</th>
      <th class="right">Чистый P&amp;L, USDT</th></tr></thead>
      <tbody id="symbols"></tbody></table></div></section>
  <section class="panel"><h2>Причины закрытия позиций</h2>
    <div class="scroll"><table><thead><tr><th>Причина выхода</th>
      <th class="right">Сделок</th></tr></thead><tbody id="reasons"></tbody></table></div>
  </section>
</div>
<div class="ledger">
  <section class="panel"><h2>Пять самых убыточных закрытий</h2>
    <div class="scroll"><table><thead><tr><th>Монета</th><th>Вход UTC</th>
      <th>Выход UTC</th><th>Причина</th><th class="right">P&amp;L</th></tr></thead>
      <tbody id="worst"></tbody></table></div></section>
  <section class="panel"><h2>Пять самых прибыльных закрытий</h2>
    <div class="scroll"><table><thead><tr><th>Монета</th><th>Вход UTC</th>
      <th>Выход UTC</th><th>Причина</th><th class="right">P&amp;L</th></tr></thead>
      <tbody id="best"></tbody></table></div></section>
</div>
<div class="notes"><strong>Границы доказательства.</strong>
  Это не подтверждение прибыльности. Нет фактических биржевых исполнений, стакана,
  точного расчёта funding по Mark Price, ликвидаций и нетронутого future holdout.
  Часовые свечи не подтверждают порядок касаний внутри одного часа. Отчёт
  сгенерирован из отдельно проверенного расчётного журнала; хеши не заменяют
  независимого удостоверения источника биржей. Разрешений на торговлю нет.
</div>
<div class="provenance">
  Сценарии: 36 · Класс: REUSED_INSPECTED_HISTORY · Исследовательский журнал:
  <b>__JOURNAL_SHA__</b><br>
  Исходный расчёт: <b>__SOURCE_SHA__</b><br>
  Независимая арифметическая сверка: <b>__AUDITOR_SHA__</b>
</div>
<footer><span>ASTRA · Historical research terminal</span>
  <span>READ ONLY / PROFITABILITY_NOT_PROVEN</span></footer>
</div>
<script type="application/json" id="research-data">__RESEARCH_JSON__</script>
<script>
(function(){
  "use strict";
  const data=JSON.parse(document.getElementById("research-data").textContent);
  const pick=id=>document.getElementById(id).value;
  const byId=id=>document.getElementById(id);
  const fmt=(v,d=2)=>new Intl.NumberFormat("ru-RU",{
    minimumFractionDigits:d,maximumFractionDigits:d
  }).format(Number(v));
  const pct=v=>(Number(v)>0?"+":"")+fmt(v)+"%";
  function signed(el,value,unit){
    const v=Number(value);
    el.textContent=(v>0?"+":"")+fmt(v)+(unit||"");
    el.classList.remove("positive","negative");
    el.classList.add(v<0?"negative":"positive");
  }
  function td(row,value,right){
    const x=document.createElement("td");x.textContent=String(value);
    if(right)x.className="right";row.appendChild(x);return x;
  }
  function renderWindows(windows){
    const parent=byId("windows");parent.replaceChildren();
    const scale=Math.max(0.1,...windows.map(x=>Math.abs(Number(x.equity_return_pct))));
    for(const w of windows){
      const row=document.createElement("div");row.className="bar-row";
      const cap=document.createElement("span");cap.className="lab";
      cap.textContent=w.start_utc.slice(0,7)+(w.complete_window?"":" *");
      cap.title=w.start_utc+" – "+w.end_utc+" / "+w.days+" days";
      const track=document.createElement("div");track.className="track";
      const bar=document.createElement("span");const value=Number(w.equity_return_pct);
      bar.className="bar "+(value<0?"negative":"positive");
      const width=Math.min(49,Math.abs(value)/scale*49);
      bar.style.width=width+"%";bar.style.left=(value<0?50-width:50)+"%";
      track.appendChild(bar);
      const x=document.createElement("strong");x.textContent=pct(value);
      x.className=value<0?"negative":"positive";
      row.append(cap,track,x);parent.appendChild(row);
    }
  }
  function renderSymbols(values){
    const parent=byId("symbols");parent.replaceChildren();
    const pairs=Object.entries(values).sort((a,b)=>Number(b[1].closed_trade_net_usd)-Number(a[1].closed_trade_net_usd));
    for(const [symbol,item] of pairs){
      const r=document.createElement("tr");
      td(r,symbol);td(r,item.closed_trades,true);td(r,item.wins,true);
      signed(td(r,"",true),item.closed_trade_net_usd);
      parent.appendChild(r);
    }
  }
  function renderReasons(data){
    const parent=byId("reasons");parent.replaceChildren();
    for(const [k,v] of Object.entries(data).sort((a,b)=>Number(b[1])-Number(a[1]))){
      const r=document.createElement("tr");td(r,k);td(r,v,true);parent.appendChild(r);
    }
  }
  function renderTradeTable(name,values){
    const parent=byId(name);parent.replaceChildren();
    for(const item of values){
      const r=document.createElement("tr");
      td(r,item.symbol);td(r,item.entry_utc.slice(0,10));td(r,item.exit_utc.slice(0,10));
      const note=td(r,item.exit_reason);
      if(item.ambiguous_intrabar_exit)note.title="Неоднозначное касание внутри свечи";
      signed(td(r,"",true),item.net_pnl_usd);
      parent.appendChild(r);
    }
  }
  function refresh(){
    const [fee,slip]=pick("cost").split("|");
    const model=pick("exit")+":"+pick("funding");
    const key=[pick("period"),fee,slip,model].join("|");
    const x=data.cases[key];
    if(!x){throw new Error("Scenario missing from verified 36-model matrix: "+key);}
    byId("dates").textContent=x.first_day+" — "+x.last_day;
    byId("selection").textContent=x.mode.replace(/_/g," ");
    signed(byId("return"),x.return_pct,"%");
    byId("equity").textContent=fmt(x.ending_equity_usd);
    byId("drawdown").textContent=fmt(x.max_drawdown_pct)+"%";
    byId("trades").textContent=String(x.trades);
    signed(byId("net_closed"),x.closed_trade_net_usd);
    signed(byId("funding_cash"),x.funding_cash_net_usd);
    byId("fees").textContent=fmt(x.closed_trade_fees_usd);
    byId("slippage").textContent=fmt(x.closed_trade_slippage_usd);
    const st=x.trade_stats;
    byId("winrate").textContent=st.win_rate_pct===null?"н/д":fmt(st.win_rate_pct)+"%";
    byId("pf").textContent=st.profit_factor_ex_funding===null
      ?"н/д":fmt(st.profit_factor_ex_funding);
    for(const [id,key] of [
      ["avg_win","average_win_usd"],
      ["avg_loss","average_loss_usd"],
      ["avg_trade","average_closed_trade_net_usd"]
    ]){
      const value=st[key];
      if(value===null){byId(id).textContent="н/д";continue;}
      signed(byId(id),value);
    }
    byId("loss_streak").textContent=String(st.max_consecutive_losing_closures);
    const p=data.pairs.find(item=>item.fee_bps_per_fill===fee
      &&item.slippage_bps_per_side===slip&&item.model_key===model);
    for(const id of ["six_return","broad_return","difference"])byId(id).textContent="—";
    if(p){
      signed(byId("six_return"),p.six_return_pct,"%");
      signed(byId("broad_return"),p.broad21_return_pct,"%");
      signed(byId("difference"),p.six_minus_broad21_percentage_points," п.п.");
    }
    renderWindows(x.windows);renderSymbols(x.by_symbol);
    renderReasons(x.reason_counts);
    renderTradeTable("worst",x.worst_trades);
    renderTradeTable("best",x.best_trades);
  }
  for(const id of ["period","cost","exit","funding"]){
    byId(id).addEventListener("change",refresh);
  }
  refresh();
})();
</script>
</body></html>
"""


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
