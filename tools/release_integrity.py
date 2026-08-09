from __future__ import annotations

import argparse
import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from tools.product_identity import STABLE_PACKAGE_NAME

_REQUIREMENT = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)==(?P<version>[^\\\s]+)")


@dataclass(frozen=True)
class LockedDependency:
    name: str
    version: str
    hashes: tuple[str, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_lock(path: Path) -> tuple[LockedDependency, ...]:
    lines = path.read_text(encoding="utf-8").splitlines()
    dependencies: list[LockedDependency] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        match = _REQUIREMENT.match(stripped)
        if match is None:
            index += 1
            continue
        name = match.group("name")
        version = match.group("version")
        hashes: list[str] = []
        cursor = index
        while cursor < len(lines):
            current = lines[cursor].strip()
            if cursor > index and _REQUIREMENT.match(current):
                break
            marker = "--hash=sha256:"
            if marker in current:
                hashes.append(current.split(marker, 1)[1].rstrip(" \\"))
            cursor += 1
        if not hashes:
            raise ValueError(f"locked dependency has no SHA-256 hashes: {name}")
        dependencies.append(LockedDependency(name, version, tuple(sorted(set(hashes)))))
        index = cursor
    if not dependencies:
        raise ValueError("dependency lock contains no pinned packages")
    names = [dependency.name.lower().replace("_", "-") for dependency in dependencies]
    if len(names) != len(set(names)):
        raise ValueError("dependency lock contains duplicate package identities")
    return tuple(sorted(dependencies, key=lambda item: item.name.lower()))


def _spdx_id(name: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9.-]", "-", name)
    return f"SPDXRef-Package-{normalized}"


def _parse_created_at(value: str) -> datetime:
    created_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    return created_at.astimezone(UTC)


def _release_artifacts(
    dist_directory: Path,
    *,
    package_name: str,
    package_version: str,
) -> tuple[Path, ...]:
    if package_name != STABLE_PACKAGE_NAME:
        raise ValueError(f"release package must be {STABLE_PACKAGE_NAME}")
    normalized = package_name.replace("-", "_")
    wheel_prefix = f"{normalized}-{package_version}-"
    sdist_name = f"{normalized}-{package_version}.tar.gz"
    artifacts = tuple(sorted(path for path in dist_directory.iterdir() if path.is_file()))
    if not artifacts:
        raise ValueError("release directory contains no artifacts")
    if any(path.stat().st_size <= 0 for path in artifacts):
        raise ValueError("release artifacts must be non-empty")
    wheels = [path for path in artifacts if path.name.startswith(wheel_prefix) and path.suffix == ".whl"]
    sdists = [path for path in artifacts if path.name == sdist_name]
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("release must contain exactly one matching wheel and source distribution")
    allowed = {wheels[0].name, sdists[0].name}
    unexpected = [path.name for path in artifacts if path.name not in allowed]
    if unexpected:
        raise ValueError(f"unexpected release artifacts: {','.join(unexpected)}")
    return artifacts


def _spdx_package(
    *,
    name: str,
    version: str,
    package_type: str = "library",
) -> dict[str, object]:
    return {
        "SPDXID": _spdx_id(name),
        "name": name,
        "versionInfo": version,
        "primaryPackagePurpose": package_type.upper(),
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": "NOASSERTION",
        "copyrightText": "NOASSERTION",
        "externalRefs": [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": f"pkg:pypi/{name.lower().replace('_', '-')}@{version}",
            }
        ],
    }


def generate_release_evidence(
    *,
    repository_root: Path,
    lock_path: Path,
    dist_directory: Path,
    output_directory: Path,
    commit_sha: str,
    created_at: datetime,
) -> tuple[Path, Path]:
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        raise ValueError("commit_sha must be a full lowercase Git SHA")
    dependencies = parse_lock(lock_path)
    pyproject_path = repository_root / "pyproject.toml"
    project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]
    package_name = str(project["name"])
    package_version = str(project["version"])
    artifacts = _release_artifacts(
        dist_directory,
        package_name=package_name,
        package_version=package_version,
    )

    output_directory.mkdir(parents=True, exist_ok=True)
    created_iso = created_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    artifact_entries = [
        {
            "name": path.name,
            "size_bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in artifacts
    ]
    manifest = {
        "schema": "astra-release-integrity-v1",
        "package": {"name": package_name, "version": package_version},
        "commit_sha": commit_sha,
        "created_at": created_iso,
        "pyproject_sha256": sha256_file(pyproject_path),
        "requirements_lock_sha256": sha256_file(lock_path),
        "locked_dependency_count": len(dependencies),
        "artifacts": artifact_entries,
        "external_order_routing_allowed": False,
        "live_trading_allowed": False,
    }
    manifest_path = output_directory / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    product_spdx_id = _spdx_id(package_name)
    dependency_packages = [
        _spdx_package(name=dependency.name, version=dependency.version)
        for dependency in dependencies
    ]
    spdx_packages = [
        _spdx_package(name=package_name, version=package_version, package_type="application"),
        *dependency_packages,
    ]
    relationships = [
        {
            "spdxElementId": "SPDXRef-DOCUMENT",
            "relationshipType": "DESCRIBES",
            "relatedSpdxElement": product_spdx_id,
        },
        *(
            {
                "spdxElementId": product_spdx_id,
                "relationshipType": "DEPENDS_ON",
                "relatedSpdxElement": _spdx_id(dependency.name),
            }
            for dependency in dependencies
        ),
    ]
    sbom = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"{package_name}-{package_version}-dependencies",
        "documentNamespace": f"https://github.com/PetrFedin/trade_bot/spdx/{commit_sha}",
        "creationInfo": {
            "created": created_iso,
            "creators": ["Tool: astra-release-integrity-v1"],
        },
        "packages": spdx_packages,
        "relationships": relationships,
    }
    sbom_path = output_directory / "sbom.spdx.json"
    sbom_path.write_text(json.dumps(sbom, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest_path, sbom_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra-release-integrity")
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--lock", type=Path, default=Path("requirements.lock"))
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--output", type=Path, default=Path("release-evidence"))
    parser.add_argument("--commit-sha", required=True)
    parser.add_argument("--created-at", required=True)
    args = parser.parse_args(argv)
    manifest, sbom = generate_release_evidence(
        repository_root=args.root.resolve(),
        lock_path=args.lock.resolve(),
        dist_directory=args.dist.resolve(),
        output_directory=args.output.resolve(),
        commit_sha=args.commit_sha,
        created_at=_parse_created_at(args.created_at),
    )
    print(json.dumps({"manifest": str(manifest), "sbom": str(sbom)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
