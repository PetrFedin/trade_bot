from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

EXACT_SHA = re.compile(r"^[0-9a-f]{40}$")
USES_RE = re.compile(r"(?m)^\s*(?:-\s*)?uses:\s*['\"]?([^'\"\s#]+)")
EVENT_RE = re.compile(r"^  ([A-Za-z0-9_-]+):(?:\s*(.*))?$")

APPROVED_ACTION_REFS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",  # v7.0.1, Node 24
    "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",  # v7.0.0, Node 24
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",  # v7.0.1, Node 24
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",  # v8.0.1, Node 24
    "actions/attest": "1e69f48acb82d1966a394da916b4c1698aa569d6",  # v4.2.2, Node 24
}


@dataclass(frozen=True, order=True)
class Violation:
    path: str
    message: str


def _on_block(text: str) -> list[str]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("on:"):
            inline = line[3:].strip()
            if inline:
                return [f"  __inline__: {inline}"]
            block: list[str] = []
            for candidate in lines[index + 1 :]:
                if candidate and not candidate.startswith((" ", "\t")) and not candidate.lstrip().startswith("#"):
                    break
                block.append(candidate)
            return block
    return []


def _event_blocks(text: str) -> dict[str, list[str]]:
    block = _on_block(text)
    if not block:
        return {}
    if block[0].startswith("  __inline__:"):
        inline = block[0].split(":", 1)[1].strip()
        if inline.startswith("[") and inline.endswith("]"):
            names = [item.strip() for item in inline[1:-1].split(",") if item.strip()]
        else:
            names = [inline]
        return {name: [] for name in names}

    events: dict[str, list[str]] = {}
    current: str | None = None
    for line in block:
        match = EVENT_RE.match(line)
        if match:
            current = match.group(1)
            events[current] = []
            remainder = (match.group(2) or "").strip()
            if remainder:
                events[current].append("    " + remainder)
            continue
        if current is not None:
            events[current].append(line)
    return events


def _event_targets_main(lines: Iterable[str]) -> bool:
    material = "\n".join(lines)
    if "branches:" not in material:
        return True
    return bool(
        re.search(r"branches:\s*\[[^\]]*\bmain\b", material)
        or re.search(r"(?m)^\s+-\s+main\s*(?:#.*)?$", material)
    )


def is_operational_workflow(text: str) -> bool:
    events = _event_blocks(text)
    if not events:
        return False
    for event, lines in events.items():
        if event in {"workflow_dispatch", "schedule"}:
            return True
        if event in {"push", "pull_request", "pull_request_target"} and _event_targets_main(lines):
            return True
    return False


def _audit_uses(path: Path, text: str) -> list[Violation]:
    violations: list[Violation] = []
    for target in USES_RE.findall(text):
        if target.startswith("./"):
            continue
        if target.startswith("docker://"):
            if "@sha256:" not in target:
                violations.append(Violation(str(path), f"mutable Docker action reference: {target}"))
            continue
        if "@" not in target:
            violations.append(Violation(str(path), f"action reference is not pinned: {target}"))
            continue
        action, ref = target.rsplit("@", 1)
        if not EXACT_SHA.fullmatch(ref):
            violations.append(Violation(str(path), f"action reference must use exact 40-hex SHA: {target}"))
            continue
        approved = APPROVED_ACTION_REFS.get(action)
        if approved is not None and ref != approved:
            violations.append(
                Violation(
                    str(path),
                    f"{action} must use approved Node 24 SHA {approved}, found {ref}",
                )
            )
    return violations


def audit_workflow(path: Path, text: str) -> list[Violation]:
    if not is_operational_workflow(text):
        return []
    violations = _audit_uses(path, text)
    if re.search(r"(?i)\bpip(?:3)?\s+install\b[^\n]*--upgrade[^\n]*\bpip\b", text):
        violations.append(Violation(str(path), "operational workflow must not upgrade pip dynamically"))
    return sorted(violations)


def workflow_paths(root: Path) -> list[Path]:
    workflows = root / ".github" / "workflows"
    return sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")])


def audit_repository(root: Path) -> tuple[list[Path], list[Violation]]:
    operational: list[Path] = []
    violations: list[Violation] = []
    for path in workflow_paths(root):
        text = path.read_text(encoding="utf-8")
        if not is_operational_workflow(text):
            continue
        operational.append(path)
        violations.extend(audit_workflow(path.relative_to(root), text))
    return operational, sorted(violations)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit operational GitHub Actions supply-chain policy")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    operational, violations = audit_repository(root)
    print(f"operational workflows audited: {len(operational)}")
    for path in operational:
        print(f"  ACTIVE {path.relative_to(root)}")
    if violations:
        print(f"policy violations: {len(violations)}")
        for violation in violations:
            print(f"  FAIL {violation.path}: {violation.message}")
        return 1
    print("PASS operational workflow action policy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
