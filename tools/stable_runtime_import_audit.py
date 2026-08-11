from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path

_VERSIONED_RUNTIME_MODULE = re.compile(r"^app\.runtime\.[A-Za-z0-9_]+_v\d+$")
_VERSIONED_SOURCE_FILE = re.compile(r"_v\d+\.py$")


@dataclass(frozen=True, order=True)
class VersionedRuntimeImport:
    path: str
    module: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "module": self.module}


def _is_stable_source(path: Path, *, root: Path) -> bool:
    relative = path.relative_to(root).as_posix()
    if path.name == "__init__.py":
        return True
    if _VERSIONED_SOURCE_FILE.search(path.name):
        return False
    return relative.endswith(".py")


def scan_versioned_runtime_imports(root: Path) -> tuple[VersionedRuntimeImport, ...]:
    root = root.resolve()
    findings: set[VersionedRuntimeImport] = set()
    for path in sorted(root.rglob("*.py")):
        if not _is_stable_source(path, root=root):
            continue
        relative = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if _VERSIONED_RUNTIME_MODULE.fullmatch(module):
                    findings.add(VersionedRuntimeImport(relative, module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _VERSIONED_RUNTIME_MODULE.fullmatch(alias.name):
                        findings.add(VersionedRuntimeImport(relative, alias.name))
    return tuple(sorted(findings))


def load_baseline(path: Path) -> tuple[VersionedRuntimeImport, ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("stable runtime import baseline schema mismatch")
    imports = data.get("imports")
    if not isinstance(imports, list):
        raise ValueError("stable runtime import baseline imports must be a list")
    parsed: set[VersionedRuntimeImport] = set()
    for item in imports:
        if not isinstance(item, dict):
            raise ValueError("stable runtime import baseline item must be an object")
        path_value = item.get("path")
        module_value = item.get("module")
        if not isinstance(path_value, str) or not path_value.endswith(".py"):
            raise ValueError("stable runtime import baseline path is invalid")
        if not isinstance(module_value, str) or not _VERSIONED_RUNTIME_MODULE.fullmatch(
            module_value
        ):
            raise ValueError("stable runtime import baseline module is invalid")
        parsed.add(VersionedRuntimeImport(path_value, module_value))
    if len(parsed) != len(imports):
        raise ValueError("stable runtime import baseline contains duplicates")
    return tuple(sorted(parsed))


def compare_imports(
    observed: tuple[VersionedRuntimeImport, ...],
    baseline: tuple[VersionedRuntimeImport, ...],
) -> tuple[tuple[VersionedRuntimeImport, ...], tuple[VersionedRuntimeImport, ...]]:
    observed_set = set(observed)
    baseline_set = set(baseline)
    new = tuple(sorted(observed_set - baseline_set))
    stale = tuple(sorted(baseline_set - observed_set))
    return new, stale


def build_report(
    *,
    observed: tuple[VersionedRuntimeImport, ...],
    baseline: tuple[VersionedRuntimeImport, ...],
) -> dict[str, object]:
    new, stale = compare_imports(observed, baseline)
    return {
        "schema_version": 1,
        "status": "PASS" if not new and not stale else "FAIL",
        "observed_count": len(observed),
        "baseline_count": len(baseline),
        "observed": [item.to_dict() for item in observed],
        "baseline": [item.to_dict() for item in baseline],
        "new_versioned_runtime_imports": [item.to_dict() for item in new],
        "stale_baseline_imports": [item.to_dict() for item in stale],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Freeze direct stable-app imports of versioned runtime modules"
    )
    parser.add_argument("--root", type=Path, default=Path("app"))
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path("architecture/stable_runtime_versioned_imports.json"),
    )
    parser.add_argument("--report", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    observed = scan_versioned_runtime_imports(args.root)
    baseline = load_baseline(args.baseline)
    report = build_report(observed=observed, baseline=baseline)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(payload, end="")
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(payload, encoding="utf-8")
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
