from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from tools.qualify_entry_quality_shadow import qualify as qualify_entry_shadow
from tools.qualify_entry_quality_walk_forward import qualify as qualify_entry_walk
from tools.qualify_profit_runner_shadow import qualify as qualify_runner_shadow
from tools.qualify_profit_runner_walk_forward import qualify as qualify_runner_walk
from tools.qualify_selection_exit_confirmation_shadow import (
    qualify as qualify_exit_shadow,
)
from tools.qualify_selection_exit_confirmation_walk_forward import (
    qualify as qualify_exit_walk,
)

_SCHEMA = "external-strategy-candidate-suite-v1"


def _write(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def qualify_suite(
    *,
    csv_path: Path,
    trading_quality_config_path: Path,
    entry_quality_policy_path: Path,
    selection_exit_policy_path: Path,
    profit_runner_policy_path: Path,
    output_dir: Path,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    tasks: tuple[
        tuple[str, str, Callable[[Path, Path, Path], dict[str, object]], Path], ...
    ] = (
        (
            "entry_quality_same_sample",
            "entry-quality-report.json",
            qualify_entry_shadow,
            entry_quality_policy_path,
        ),
        (
            "entry_quality_walk_forward",
            "entry-quality-walk-forward-report.json",
            qualify_entry_walk,
            entry_quality_policy_path,
        ),
        (
            "selection_exit_same_sample",
            "selection-exit-report.json",
            qualify_exit_shadow,
            selection_exit_policy_path,
        ),
        (
            "selection_exit_walk_forward",
            "selection-exit-walk-forward-report.json",
            qualify_exit_walk,
            selection_exit_policy_path,
        ),
        (
            "profit_runner_same_sample",
            "profit-runner-report.json",
            qualify_runner_shadow,
            profit_runner_policy_path,
        ),
        (
            "profit_runner_walk_forward",
            "profit-runner-walk-forward-report.json",
            qualify_runner_walk,
            profit_runner_policy_path,
        ),
    )

    reports: dict[str, dict[str, object]] = {}
    files: dict[str, str] = {}
    for name, file_name, qualifier, policy_path in tasks:
        report = qualifier(
            csv_path,
            trading_quality_config_path,
            policy_path,
        )
        if report.get("source_csv_sha256") != source_sha:
            raise RuntimeError(f"candidate report source hash mismatch:{name}")
        if report.get("shadow_only") is not True:
            raise RuntimeError(f"candidate report is not shadow_only:{name}")
        if report.get("strategy_promotion_allowed") is not False:
            raise RuntimeError(f"candidate report unexpectedly allows promotion:{name}")
        if report.get("external_order_routing_allowed") is not False:
            raise RuntimeError(f"candidate report unexpectedly allows routing:{name}")
        if report.get("live_trading_allowed") is not False:
            raise RuntimeError(f"candidate report unexpectedly allows live trading:{name}")
        _write(output_dir / file_name, report)
        reports[name] = report
        files[name] = file_name

    manifest = {
        "schema_version": _SCHEMA,
        "qualification": "PASS_EXTERNAL_STRATEGY_CANDIDATE_SUITE",
        "source_csv_sha256": source_sha,
        "shadow_only": True,
        "strategy_promotion_allowed": False,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
        "candidate_count": len(reports),
        "reports": files,
        "remaining_real_paper_blockers": sorted(
            {
                blocker
                for report in reports.values()
                for blocker in report.get("remaining_promotion_blockers", [])
                if isinstance(blocker, str) and "REAL_PAPER" in blocker
            }
        ),
    }
    _write(output_dir / "strategy-candidate-suite-manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run all non-promotable strategy candidates on one acquired CSV"
    )
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--trading-quality-config", type=Path, required=True)
    parser.add_argument("--entry-quality-policy", type=Path, required=True)
    parser.add_argument("--selection-exit-policy", type=Path, required=True)
    parser.add_argument("--profit-runner-policy", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    manifest = qualify_suite(
        csv_path=args.csv,
        trading_quality_config_path=args.trading_quality_config,
        entry_quality_policy_path=args.entry_quality_policy,
        selection_exit_policy_path=args.selection_exit_policy,
        profit_runner_policy_path=args.profit_runner_policy,
        output_dir=args.output_dir,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
