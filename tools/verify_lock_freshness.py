from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from tools.release_integrity import LockedDependency, parse_lock


@dataclass(frozen=True)
class LockDifference:
    package: str
    expected_version: str | None
    generated_version: str | None
    expected_hashes: tuple[str, ...]
    generated_hashes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "package": self.package,
            "expected_version": self.expected_version,
            "generated_version": self.generated_version,
            "expected_hashes": list(self.expected_hashes),
            "generated_hashes": list(self.generated_hashes),
        }


def _index(dependencies: tuple[LockedDependency, ...]) -> dict[str, LockedDependency]:
    return {
        dependency.name.lower().replace("_", "-"): dependency
        for dependency in dependencies
    }


def compare_locks(expected: Path, generated: Path) -> tuple[LockDifference, ...]:
    expected_index = _index(parse_lock(expected))
    generated_index = _index(parse_lock(generated))
    differences: list[LockDifference] = []
    for package in sorted(set(expected_index) | set(generated_index)):
        left = expected_index.get(package)
        right = generated_index.get(package)
        if left is not None and right is not None:
            if left.version == right.version and left.hashes == right.hashes:
                continue
        differences.append(
            LockDifference(
                package=package,
                expected_version=None if left is None else left.version,
                generated_version=None if right is None else right.version,
                expected_hashes=() if left is None else left.hashes,
                generated_hashes=() if right is None else right.hashes,
            )
        )
    return tuple(differences)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-verify-lock-freshness")
    parser.add_argument("expected", type=Path)
    parser.add_argument("generated", type=Path)
    args = parser.parse_args(argv)
    differences = compare_locks(args.expected, args.generated)
    result = {
        "status": "PASS" if not differences else "FAIL",
        "differences": [difference.as_dict() for difference in differences],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not differences else 2


if __name__ == "__main__":
    raise SystemExit(main())
