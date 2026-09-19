"""Fail when the declared next vertical gate no longer matches the tracker.

`tools/system_status.py` validates that committed status artifacts agree with their
sources, but every one of those sources lives in the repository. Nothing compares the
declared gate against the issue tracker, so a gate stays advertised as "next" after its
issue is closed: the generated README block pointed at issue #138 for as long as the
qualification evidence did, including after #138 was closed.

This check is deliberately separate from system_status.py, which stays offline and pure.
It reads the declared gate, asks the tracker for that issue's state, and fails when the
issue is closed or missing. Declaring the replacement gate stays a maintainer decision:
this only refuses to let a stale one pass silently.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "CURRENT_SYSTEM_STATUS.json"
API = "https://api.github.com/repos/{repository}/issues/{issue}"


def declared_gate(status_path: Path) -> dict:
    """Return the next_vertical_gate block declared by the generated status surface."""
    status = json.loads(status_path.read_text())
    qualification = status.get("engineering_qualification")
    if not isinstance(qualification, dict):
        raise SystemExit("STATUS_CONTRACT: engineering_qualification is missing")
    gate = qualification.get("next_vertical_gate")
    if not isinstance(gate, dict) or "issue" not in gate:
        raise SystemExit("STATUS_CONTRACT: next_vertical_gate.issue is missing")
    return gate


def issue_state(repository: str, issue: int, token: str | None) -> tuple[str, str]:
    """Return (state, title) for an issue, or raise SystemExit describing the failure."""
    request = urllib.request.Request(API.format(repository=repository, issue=issue))
    request.add_header("Accept", "application/vnd.github+json")
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise SystemExit(
                f"GATE_ISSUE_MISSING: {repository}#{issue} is not visible to this token"
            ) from error
        raise SystemExit(
            f"TRACKER_UNAVAILABLE: {repository}#{issue} returned HTTP {error.code}"
        ) from error
    except urllib.error.URLError as error:
        raise SystemExit(f"TRACKER_UNAVAILABLE: {error.reason}") from error
    return str(payload.get("state", "")), str(payload.get("title", ""))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository",
        default=os.environ.get("GITHUB_REPOSITORY", "PetrFedin/trade_bot"),
        help="owner/name of the tracker repository",
    )
    parser.add_argument(
        "--status-path",
        type=Path,
        default=STATUS_PATH,
        help="path to the generated status surface",
    )
    args = parser.parse_args(argv)

    gate = declared_gate(args.status_path)
    issue = int(gate["issue"])
    name = gate.get("name", "")

    state, title = issue_state(
        args.repository,
        issue,
        os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"),
    )

    if state != "open":
        print(
            f"STALE_VERTICAL_GATE: #{issue} ({name}) is declared as the next gate but is "
            f"{state} in {args.repository}: {title!r}. Declare the replacement gate in the "
            f"qualification evidence for the current subject SHA, then regenerate the "
            f"status surface with tools/system_status.py --write.",
            file=sys.stderr,
        )
        return 1

    print(f"next vertical gate #{issue} ({name}) is open: {title!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
