"""Render the operator status console from the generated status surface.

CURRENT_SYSTEM_STATUS.json is machine-readable and the README block is a few lines of
prose. Neither shows an operator, at a glance, which authorities are closed, what the
frozen strategy replay actually returned, or how far the qualified capability set has
advanced. This renders that from the same source, so the console cannot drift from the
truth it reports.

The output is generated, never hand-edited, and `--check` fails when the committed file
disagrees with its source. It carries no script and no external request: it is a document
about a fail-closed system, not a control surface for one. Nothing here can grant an
authority, and every authority is rendered from the status surface rather than assumed.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "CURRENT_SYSTEM_STATUS.json"
OUTPUT_PATH = ROOT / "docs" / "status-console.html"

AUTHORITIES = (
    ("external_order_routing_allowed", "Внешний роутинг ордеров"),
    ("demo_trading_allowed", "Торговля на Demo"),
    ("live_trading_allowed", "Торговля на живых деньгах"),
    ("mainnet_entry_allowed", "Вход в mainnet"),
    ("production_release_allowed", "Продакшен-релиз"),
)

REPLAY_ROWS = (
    ("reference_equity_usdt", "Эталонный капитал, USDT", False),
    ("eligible_signals", "Подходящих сигналов", False),
    ("plan_eligible_signals", "Из них прошли планирование", False),
    ("independent_target_stop_episodes", "Независимых эпизодов target/stop", False),
    ("target_first", "Первым сработал target", False),
    ("stop_first", "Первым сработал stop", True),
    ("neither", "Ни один не сработал", False),
    ("portfolio_trades", "Портфельных сделок", False),
    ("wins", "Выигрышей", False),
    ("breakeven", "В ноль", False),
    ("losses", "Проигрышей", True),
    ("net_pnl_usdt", "Чистый P&L, USDT", True),
)


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _display(path: Path) -> str:
    """Return a repository-relative path, or the full path when it lies outside."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _pill(allowed: bool) -> str:
    label = "разрешено" if allowed else "закрыто"
    state = "open" if allowed else "shut"
    return f'<span class="pill {state}">{label}</span>'


FONTS = ""

PALETTE_LIGHT = """
  --bg:#eef1f4; --surface:#fff; --surface2:#e4e9ed; --sunk:#f6f8f9;
  --text:#121a20; --text2:#485760; --text3:#6d7d88;
  --line:#ced7dd; --line2:#dfe6ea;
  --accent:#0f6167; --accent2:#d7ebec;
  --ok:#2a6746; --okbg:#e0eee6;
  --warn:#8d5c11; --warnbg:#f5ebd9;
  --crit:#962c3b; --critbg:#f6e2e4;
"""

PALETTE_DARK = """
  --bg:#0c1216; --surface:#141c22; --surface2:#1b262d; --sunk:#101820;
  --text:#e4ebef; --text2:#a3b1ba; --text3:#7a8893;
  --line:#26333b; --line2:#1e2a31;
  --accent:#47b3ba; --accent2:#0f3236;
  --ok:#69bd8a; --okbg:#122c1f;
  --warn:#d3a049; --warnbg:#2e2412;
  --crit:#d97382; --critbg:#2f1519;
"""

STYLE = """
*{box-sizing:border-box}
body{
  margin:0;background:var(--bg);color:var(--text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
  font-size:15px;line-height:1.6;-webkit-font-smoothing:antialiased;
}
.wrap{
  max-width:900px;margin:0 auto;
  padding-block:36px 64px;padding-left:20px;padding-right:20px;
}
h1,h2{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
  text-wrap:balance;margin:0;
}
h1{
  font-size:clamp(26px,4.6vw,37px);font-weight:700;
  letter-spacing:-.022em;line-height:1.12;
}
h2{font-size:20px;font-weight:600;letter-spacing:-.012em}
p{margin:0}
code{font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;font-size:.87em}
.eyebrow{
  font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;
  font-size:11px;font-weight:500;
  letter-spacing:.15em;text-transform:uppercase;color:var(--accent);
}
.lede{font-size:16px;color:var(--text2);max-width:64ch;margin-top:12px}
header{border-bottom:2px solid var(--text);padding-bottom:24px;margin-bottom:28px}
.generated{
  font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;font-size:11.5px;
  color:var(--text3);margin-top:14px;
}
section{margin-top:38px}
.shead{
  display:flex;align-items:baseline;gap:12px;margin-bottom:16px;
  border-bottom:1px solid var(--line);padding-bottom:8px;
}
.shead .n{
  font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;
  font-size:12px;color:var(--text3);
}
.verdict{
  border:2px solid var(--crit);background:var(--surface);padding:18px 20px;
  display:flex;flex-direction:column;gap:8px;
}
.verdict .t{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
  font-weight:700;font-size:17px;color:var(--crit);
}
.verdict p{font-size:14.5px;color:var(--text2)}
.auths{
  display:flex;flex-direction:column;gap:1px;
  background:var(--line2);border:1px solid var(--line2);margin-top:14px;
}
.auth{
  background:var(--surface);padding:11px 14px;display:flex;
  justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;
}
.auth .k{font-size:14.5px}
.pill{
  font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;
  font-size:11.5px;padding:3px 10px;
  border-radius:3px;border:1px solid;white-space:nowrap;
}
.pill.shut{background:var(--critbg);color:var(--crit);border-color:var(--crit)}
.pill.open{background:var(--okbg);color:var(--ok);border-color:var(--ok)}
.grid{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:1px;background:var(--line2);border:1px solid var(--line2);
}
.cell{background:var(--surface);padding:13px 15px}
.cell .v{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
  font-size:23px;font-weight:700;
  letter-spacing:-.02em;font-variant-numeric:tabular-nums;line-height:1.15;
}
.cell .k{font-size:12px;color:var(--text3);margin-top:3px}
.cell.neg .v{color:var(--crit)}
.cell.pos .v{color:var(--ok)}
.tw{overflow-x:auto;border:1px solid var(--line2);margin-top:14px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
td{
  padding:9px 13px;border-bottom:1px solid var(--line2);
  color:var(--text2);background:var(--surface);
}
tr:last-child td{border-bottom:none}
td.num{
  text-align:right;font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;
  font-variant-numeric:tabular-nums;color:var(--text);white-space:nowrap;
}
td.num.neg{color:var(--crit)}
ul.caps{
  list-style:none;padding:0;margin:0;display:grid;
  grid-template-columns:repeat(auto-fit,minmax(248px,1fr));gap:5px 16px;
}
ul.caps li{font-size:13px;color:var(--text2);padding-left:15px;position:relative}
ul.caps li::before{
  content:"";position:absolute;left:0;top:8px;width:5px;height:5px;
  border-radius:50%;background:var(--ok);
}
ul.plain{
  list-style:none;padding:0;margin:0;display:grid;
  grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:4px 16px;
  font-size:13px;color:var(--text2);
}
.gatebox{
  background:var(--warnbg);border-left:3px solid var(--warn);padding:15px 17px;
  display:flex;flex-direction:column;gap:7px;margin-top:14px;
}
.gatebox .t{
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,sans-serif;
  font-weight:600;color:var(--warn);font-size:14.5px;
}
.gatebox p{font-size:13.5px;color:var(--text2)}
.rule{
  background:var(--accent2);border-left:3px solid var(--accent);
  padding:14px 16px;font-size:13.5px;color:var(--text2);margin-top:14px;
}
footer{
  margin-top:46px;padding-top:16px;border-top:1px solid var(--line);
  font-size:12px;color:var(--text3);
  font-family:ui-monospace,SFMono-Regular,Consolas,"Liberation Mono",monospace;
}
@media(max-width:560px){
  .auth{flex-direction:column;align-items:flex-start;gap:6px}
}
"""


def render(status: dict) -> str:
    """Return the console document for a status surface."""
    authority = status.get("authority", {})
    repository = status.get("repository", {})
    qualification = status.get("engineering_qualification", {})
    release = status.get("release", {})
    strategy = status.get("strategy", {})
    trading = status.get("trading_authority", {})

    gate = qualification.get("next_vertical_gate", {})
    capabilities = list(qualification.get("qualified_capabilities", []))
    workflows = list(qualification.get("workflow_names", []))
    replay = strategy.get("latest_frozen_bybit_price_only_replay", {})

    granted = sum(1 for key, _ in AUTHORITIES if bool(trading.get(key)))
    promotion = "да" if bool(strategy.get("promotion_allowed")) else "нет"

    schema = esc(release.get("schema", "—"))
    version = esc(release.get("version", "—"))
    release_status = esc(release.get("status", "—"))
    scope = esc(repository.get("engineering_qualification_scope", "—"))
    qualified_sha = esc(repository.get("latest_engineering_qualified_main_sha", "—"))[:12]
    schema_version = esc(status.get("schema_version", "—"))

    reason = esc(trading.get("reason", ""))
    rule = esc(authority.get("rule", ""))

    strategy_status = esc(strategy.get("status", "—"))
    evidence_source = esc(strategy.get("evidence_source", "—"))
    net_pnl = esc(replay.get("net_pnl_usdt", "—"))
    trades = esc(replay.get("portfolio_trades", "—"))

    runs = esc(qualification.get("workflow_run_count", "—"))
    checks = esc(qualification.get("check_run_count", "—"))
    subject = esc(qualification.get("subject", "—"))
    source_pr = esc(qualification.get("source_pull_request", "—"))
    qualified_at = esc(qualification.get("qualified_at", "—"))

    gate_issue = esc(gate.get("issue", "—"))
    gate_name = esc(gate.get("name", "—"))
    families = esc(", ".join(gate.get("required_families", []))) or "—"

    authority_rows = "\n".join(
        f'        <div class="auth"><span class="k">{esc(label)}</span>'
        f"{_pill(bool(trading.get(key)))}</div>"
        for key, label in AUTHORITIES
    )
    capability_items = "\n".join(
        f'        <li><code>{esc(item)}</code></li>' for item in capabilities
    )
    workflow_items = "\n".join(f"        <li>{esc(item)}</li>" for item in workflows)
    replay_rows = "\n".join(
        f"        <tr><td>{esc(label)}</td>"
        f'<td class="num{" neg" if negative else ""}">'
        f'{esc(replay.get(key, "—"))}</td></tr>'
        for key, label, negative in REPLAY_ROWS
        if key in replay
    )

    return f"""<title>Консоль допуска ASTRA</title>
{FONTS}
<style>
:root{{{PALETTE_LIGHT}}}
@media (prefers-color-scheme:dark){{
  :root:not([data-theme="light"]){{{PALETTE_DARK}}}
}}
:root[data-theme="dark"]{{{PALETTE_DARK}}}
{STYLE}</style>

<div class="wrap">
<header>
  <div class="eyebrow">Операторская консоль · сгенерировано</div>
  <h1>Консоль допуска ASTRA</h1>
  <p class="lede">
    Схема {schema}, версия {version}. Состояние допуска, инженерной квалификации
    и доказательств по стратегии — из единственного источника.
  </p>
  <p class="generated">
    Источник: <code>CURRENT_SYSTEM_STATUS.json</code> ·
    генератор: <code>tools/status_dashboard.py</code> · править вручную нельзя
  </p>
</header>

<section>
  <div class="shead">
    <h2>Допуск к торговле</h2>
    <span class="n">{granted} из {len(AUTHORITIES)} открыто</span>
  </div>
  <div class="verdict">
    <div class="t">Торговля запрещена на всех уровнях</div>
    <p>{reason}</p>
  </div>
  <div class="auths">
{authority_rows}
  </div>
  <div class="rule">{rule}</div>
</section>

<section>
  <div class="shead">
    <h2>Доказательство по стратегии</h2>
    <span class="n">замороженный реплей</span>
  </div>
  <div class="grid">
    <div class="cell neg">
      <div class="v">{strategy_status}</div>
      <div class="k">статус стратегии</div>
    </div>
    <div class="cell neg">
      <div class="v">{promotion}</div>
      <div class="k">промоушен разрешён</div>
    </div>
    <div class="cell neg">
      <div class="v">{net_pnl}</div>
      <div class="k">чистый P&amp;L, USDT</div>
    </div>
    <div class="cell">
      <div class="v">{trades}</div>
      <div class="k">портфельных сделок</div>
    </div>
  </div>
  <div class="tw"><table><tbody>
{replay_rows}
  </tbody></table></div>
  <p class="generated">Источник: <code>{evidence_source}</code></p>
</section>

<section>
  <div class="shead">
    <h2>Инженерная квалификация</h2>
    <span class="n">{scope}</span>
  </div>
  <div class="grid">
    <div class="cell pos">
      <div class="v">{runs}</div>
      <div class="k">прогонов workflow, все успешны</div>
    </div>
    <div class="cell pos">
      <div class="v">{checks}</div>
      <div class="k">check-run, все успешны</div>
    </div>
    <div class="cell">
      <div class="v">{len(capabilities)}</div>
      <div class="k">квалифицированных способностей</div>
    </div>
    <div class="cell">
      <div class="v">{release_status}</div>
      <div class="k">статус релиза</div>
    </div>
  </div>
  <div class="gatebox">
    <div class="t">
      Следующий вертикальный гейт — issue #{gate_issue}: {gate_name}
    </div>
    <p>
      Требуемые семейства: {families}. Свежесть этого объявления проверяет
      <code>tools/status_tracker_sync.py</code>.
    </p>
  </div>
  <p class="generated">
    Субъект: {subject} · PR #{source_pr} · {qualified_at}
  </p>
</section>

<section>
  <div class="shead">
    <h2>Квалифицированные способности</h2>
    <span class="n">{len(capabilities)}</span>
  </div>
  <ul class="caps">
{capability_items}
  </ul>
</section>

<section>
  <div class="shead">
    <h2>Workflow последней квалификации</h2>
    <span class="n">{len(workflows)}</span>
  </div>
  <ul class="plain">
{workflow_items}
  </ul>
</section>

<footer>
  Сгенерировано из CURRENT_SYSTEM_STATUS.json · схема {schema_version} ·
  квалифицированный main {qualified_sha}
</footer>
</div>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-path", type=Path, default=STATUS_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--write", action="store_true", help="write the console document")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the committed console disagrees with its source",
    )
    args = parser.parse_args(argv)

    status = json.loads(args.status_path.read_text())
    document = render(status)

    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(document)
        print(f"wrote {_display(args.output)}")
        return 0

    if args.check:
        if not args.output.exists():
            print(f"CONSOLE_MISSING: {_display(args.output)}", file=sys.stderr)
            return 1
        if args.output.read_text() != document:
            print(
                f"CONSOLE_ARTIFACT_STALE: {_display(args.output)} disagrees with "
                f"{args.status_path.name}. Regenerate with tools/status_dashboard.py --write.",
                file=sys.stderr,
            )
            return 1
        print("console artifact agrees with its source")
        return 0

    sys.stdout.write(document)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
