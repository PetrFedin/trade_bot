from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "CURRENT_SYSTEM_STATUS.json"
README_PATH = ROOT / "README.md"
ENGINEERING_DIR = ROOT / "qualification" / "engineering"
STRATEGY_EVIDENCE_PATH = (
    ROOT / "qualification" / "strategy" / "bybit_price_only_frozen_negative.json"
)
RELEASE_IDENTITY_PATH = ROOT / "RELEASE_IDENTITY_V109.json"
LIVE_STATUS_PATH = ROOT / "LIVE_EXECUTION_STATUS_V109.json"

RUNTIME_SHA_MARKER = "<runtime:git-rev-parse-head>"
README_BEGIN = "<!-- ASTRA_STATUS:BEGIN -->"
README_END = "<!-- ASTRA_STATUS:END -->"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_FORBIDDEN_STATIC_STATUS_HEADINGS = (
    "## Current identities",
    "## Current qualification reference",
    "## Current operational consolidation",
    "## Current primary blockers",
)


class StatusContractError(RuntimeError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatusContractError(f"cannot load {path.relative_to(ROOT)}: {exc}") from exc
    if not isinstance(value, dict):
        raise StatusContractError(f"{path.relative_to(ROOT)} must contain a JSON object")
    return value


def _required_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise StatusContractError(f"{name} must be boolean")
    return value


def _latest_engineering_qualification() -> tuple[Path, dict[str, Any]]:
    candidates: list[tuple[str, Path, dict[str, Any]]] = []
    for path in sorted(ENGINEERING_DIR.glob("*.json")):
        value = _load_json(path)
        if value.get("schema_version") != "engineering-main-qualification-v1":
            continue
        qualified_at = value.get("qualified_at")
        subject_sha = value.get("subject_sha")
        if not isinstance(qualified_at, str) or not qualified_at:
            raise StatusContractError(f"{path.name}: qualified_at is required")
        if not isinstance(subject_sha, str) or _SHA_RE.fullmatch(subject_sha) is None:
            raise StatusContractError(f"{path.name}: subject_sha must be a 40-char SHA")
        if value.get("scope") != "ENGINEERING_CI_ONLY":
            raise StatusContractError(f"{path.name}: unsupported engineering scope")
        promotion = value.get("promotion")
        if not isinstance(promotion, dict):
            raise StatusContractError(f"{path.name}: promotion object is required")
        for key in (
            "strategy_qualified",
            "demo_trading_allowed",
            "external_order_routing_allowed",
            "live_trading_allowed",
            "mainnet_entry_allowed",
        ):
            if _required_bool(promotion.get(key), f"{path.name}.promotion.{key}"):
                raise StatusContractError(
                    f"{path.name}: engineering qualification cannot grant {key}"
                )
        candidates.append((qualified_at, path, value))
    if not candidates:
        raise StatusContractError("no engineering qualification manifests found")
    _, path, value = max(candidates, key=lambda item: (item[0], item[1].name))
    return path, value


def _workflow_summary(qualification: dict[str, Any]) -> dict[str, Any]:
    github = qualification.get("github")
    if not isinstance(github, dict):
        raise StatusContractError("engineering qualification github object is required")
    runs = github.get("workflow_runs")
    if not isinstance(runs, list) or not runs:
        raise StatusContractError("engineering qualification workflow_runs are required")
    names: list[str] = []
    for item in runs:
        if not isinstance(item, dict):
            raise StatusContractError("workflow run must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise StatusContractError("workflow run name is required")
        if item.get("conclusion") != "success":
            raise StatusContractError(f"qualified workflow {name} is not successful")
        names.append(name)
    if len(names) != len(set(names)):
        raise StatusContractError("engineering qualification has duplicate workflow names")
    if github.get("workflow_run_count") != len(runs):
        raise StatusContractError("workflow_run_count disagrees with workflow_runs")
    for key in (
        "all_observed_workflows_completed",
        "all_observed_workflows_success",
        "all_observed_check_runs_completed",
        "all_observed_check_runs_success",
    ):
        if not _required_bool(github.get(key), f"github.{key}"):
            raise StatusContractError(f"qualified engineering manifest requires {key}=true")
    return {
        "workflow_run_count": len(runs),
        "check_run_count": github.get("check_run_count"),
        "all_workflows_success": True,
        "all_check_runs_success": True,
        "workflow_names": sorted(names),
    }


def build_tracked_status() -> dict[str, Any]:
    engineering_path, engineering = _latest_engineering_qualification()
    release = _load_json(RELEASE_IDENTITY_PATH)
    live = _load_json(LIVE_STATUS_PATH)
    strategy = _load_json(STRATEGY_EVIDENCE_PATH)
    workflow = _workflow_summary(engineering)

    if live.get("status") != "SOURCE_AND_CI_QUALIFICATION_ONLY":
        raise StatusContractError("V109 live status must remain source/CI qualification only")
    if _required_bool(live.get("external_order_routing_allowed"), "external routing"):
        raise StatusContractError("source status cannot grant external routing")
    if _required_bool(live.get("live_trading_allowed"), "live trading"):
        raise StatusContractError("source status cannot grant live trading")
    if strategy.get("strategy_status") != "PROFITABILITY_NOT_PROVEN":
        raise StatusContractError("strategy evidence must remain PROFITABILITY_NOT_PROVEN")
    if _required_bool(strategy.get("promotion_allowed"), "strategy promotion"):
        raise StatusContractError("negative strategy evidence cannot grant promotion")

    capabilities = engineering.get("qualified_capabilities")
    if not isinstance(capabilities, list) or not all(
        isinstance(item, str) and item for item in capabilities
    ):
        raise StatusContractError("qualified_capabilities must be a non-empty string array")
    next_gate = engineering.get("next_vertical_gate")
    if not isinstance(next_gate, dict):
        raise StatusContractError("next_vertical_gate is required")

    return {
        "schema_version": "current-system-status-v3",
        "authority": {
            "generator": "tools/system_status.py",
            "tracked_status_is_generated": True,
            "readme_status_block_is_generated": True,
            "rule": (
                "Engineering CI evidence never authorizes strategy promotion, Demo, "
                "external routing, mainnet or live trading. Those require separate "
                "explicit promotion evidence."
            ),
        },
        "repository": {
            "observed_checkout_sha": RUNTIME_SHA_MARKER,
            "observed_checkout_sha_source": "GITHUB_SHA or git rev-parse HEAD",
            "latest_engineering_qualification_manifest": str(
                engineering_path.relative_to(ROOT)
            ),
            "latest_engineering_qualified_main_sha": engineering["subject_sha"],
            "engineering_qualification_scope": engineering["scope"],
            "checkout_relation": "<runtime:computed>",
        },
        "engineering_qualification": {
            "subject": engineering.get("subject"),
            "qualified_at": engineering.get("qualified_at"),
            "source_pull_request": engineering.get("source_pull_request"),
            **workflow,
            "qualified_capabilities": capabilities,
            "next_vertical_gate": next_gate,
        },
        "release": {
            "latest_immutable_release_identity": RELEASE_IDENTITY_PATH.name,
            "schema": release.get("schema"),
            "version": release.get("version"),
            "live_execution_status_source": LIVE_STATUS_PATH.name,
            "status": live["status"],
        },
        "strategy": {
            "evidence_source": str(STRATEGY_EVIDENCE_PATH.relative_to(ROOT)),
            "status": strategy["strategy_status"],
            "promotion_allowed": False,
            "latest_frozen_bybit_price_only_replay": {
                "reference_equity_usdt": strategy.get("reference_equity_usdt"),
                "eligible_signals": strategy.get("eligible_signals"),
                "plan_eligible_signals": strategy.get("plan_eligible_signals"),
                "independent_target_stop_episodes": strategy.get(
                    "independent_target_stop_episodes"
                ),
                "target_first": strategy.get("target_first"),
                "stop_first": strategy.get("stop_first"),
                "neither": strategy.get("neither"),
                "portfolio_trades": strategy.get("portfolio_trades"),
                "wins": strategy.get("wins"),
                "breakeven": strategy.get("breakeven"),
                "losses": strategy.get("losses"),
                "net_pnl_usdt": strategy.get("net_pnl_usdt"),
                "net_return_fraction_approx": strategy.get(
                    "net_return_fraction_approx"
                ),
            },
        },
        "trading_authority": {
            "external_order_routing_allowed": False,
            "demo_trading_allowed": False,
            "live_trading_allowed": False,
            "mainnet_entry_allowed": False,
            "production_release_allowed": False,
            "reason": "No separate explicit trading-promotion manifest is present.",
        },
    }


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


def _readme_block(status: dict[str, Any]) -> str:
    repository = status["repository"]
    engineering = status["engineering_qualification"]
    strategy = status["strategy"]
    authority = status["trading_authority"]
    gate = engineering["next_vertical_gate"]
    return "\n".join(
        (
            README_BEGIN,
            "## Current system status — generated",
            "",
            "This block is generated by `tools/system_status.py`. Do not edit it manually.",
            "",
            "- Exact checkout SHA: runtime-derived via `python tools/system_status.py --show`.",
            "- Latest engineering-qualified `main`: "
            f"`{repository['latest_engineering_qualified_main_sha']}` ",
            f"  (`{repository['engineering_qualification_scope']}`).",
            f"- Post-merge engineering workflows: {engineering['workflow_run_count']} / "
            f"{engineering['workflow_run_count']} observed workflow runs successful; "
            f"{engineering['check_run_count']} / {engineering['check_run_count']} "
            "observed check-runs successful.",
            f"- Strategy: `{strategy['status']}`; promotion is not allowed.",
            "- Trading authority: external routing, Demo, mainnet and live remain "
            "fail-closed (`false`).",
            f"- Next vertical gate: issue #{gate['issue']} — `{gate['name']}` ",
            f"  ({', '.join(gate['required_families'])}).",
            "- Immutable release authority source: `LIVE_EXECUTION_STATUS_V109.json` ",
            "  remains `SOURCE_AND_CI_QUALIFICATION_ONLY`.",
            "",
            "A green engineering workflow cannot promote a strategy or grant trading authority.",
            README_END,
        )
    )


def _replace_readme_block(readme: str, block: str) -> str:
    if readme.count(README_BEGIN) != 1 or readme.count(README_END) != 1:
        raise StatusContractError("README must contain exactly one generated status block")
    begin = readme.index(README_BEGIN)
    end = readme.index(README_END, begin) + len(README_END)
    return readme[:begin] + block + readme[end:]


def _validate_static_readme(readme: str) -> None:
    begin = readme.index(README_BEGIN)
    end = readme.index(README_END, begin) + len(README_END)
    static = readme[:begin] + readme[end:]
    for heading in _FORBIDDEN_STATIC_STATUS_HEADINGS:
        if heading in static:
            raise StatusContractError(
                f"README duplicates generated current-state content: {heading}"
            )


def _checkout_sha() -> str:
    from_env = os.environ.get("GITHUB_SHA", "").strip().lower()
    if _SHA_RE.fullmatch(from_env):
        return from_env
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise StatusContractError("cannot resolve checkout SHA from GITHUB_SHA or git") from exc
    value = result.stdout.strip().lower()
    if _SHA_RE.fullmatch(value) is None:
        raise StatusContractError("git rev-parse HEAD did not return a 40-char SHA")
    return value


def _checkout_relation(observed_sha: str, qualified_sha: str) -> str:
    if observed_sha == qualified_sha:
        return "CURRENT_ENGINEERING_QUALIFICATION"
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", qualified_sha, observed_sha],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise StatusContractError("cannot compare checkout with qualified SHA") from exc
    if result.returncode == 0:
        return "CHANGED_SINCE_LAST_ENGINEERING_QUALIFICATION"
    if result.returncode == 1:
        return "DIVERGED_FROM_LAST_ENGINEERING_QUALIFICATION"
    raise StatusContractError("git merge-base could not compare qualification ancestry")


def render_runtime_status(
    tracked: dict[str, Any],
    *,
    observed_sha: str,
    relation: str,
) -> dict[str, Any]:
    if _SHA_RE.fullmatch(observed_sha) is None:
        raise StatusContractError("observed_sha must be a 40-char lowercase SHA")
    runtime = deepcopy(tracked)
    runtime["repository"]["observed_checkout_sha"] = observed_sha
    runtime["repository"]["checkout_relation"] = relation
    return runtime


def check() -> None:
    status = build_tracked_status()
    expected_status = _json_text(status)
    actual_status = STATUS_PATH.read_text(encoding="utf-8")
    if actual_status != expected_status:
        raise StatusContractError("STATUS_ARTIFACT_STALE: CURRENT_SYSTEM_STATUS.json")

    readme = README_PATH.read_text(encoding="utf-8")
    _validate_static_readme(readme)
    expected_readme = _replace_readme_block(readme, _readme_block(status))
    if readme != expected_readme:
        raise StatusContractError("STATUS_ARTIFACT_STALE: README generated block")


def write() -> None:
    status = build_tracked_status()
    STATUS_PATH.write_text(_json_text(status), encoding="utf-8")
    readme = README_PATH.read_text(encoding="utf-8")
    _validate_static_readme(readme)
    README_PATH.write_text(
        _replace_readme_block(readme, _readme_block(status)),
        encoding="utf-8",
    )


def show() -> None:
    tracked = build_tracked_status()
    observed = _checkout_sha()
    qualified = tracked["repository"]["latest_engineering_qualified_main_sha"]
    relation = _checkout_relation(observed, qualified)
    print(_json_text(render_runtime_status(tracked, observed_sha=observed, relation=relation)), end="")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compile and validate ASTRA system status")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="fail if tracked artifacts drift")
    mode.add_argument("--write", action="store_true", help="rewrite tracked generated artifacts")
    mode.add_argument("--show", action="store_true", help="print runtime status with exact SHA")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.check:
            check()
        elif args.write:
            write()
        else:
            show()
    except (StatusContractError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
