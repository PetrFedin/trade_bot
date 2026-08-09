from __future__ import annotations

import re
from dataclasses import dataclass

STABLE_PACKAGE_NAME = "astra-trade-bot"
KNOWN_SCHEMA_MINIMUM_VERSIONS: dict[int, tuple[int, int, int]] = {
    100: (7, 30, 0),
    101: (7, 31, 0),
    102: (7, 32, 0),
    103: (7, 33, 0),
    104: (7, 34, 0),
    105: (7, 35, 0),
    106: (7, 36, 0),
    107: (7, 37, 0),
    108: (7, 38, 0),
}
_VERSION_RE = re.compile(
    r'^version\s*=\s*"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"\s*$',
    re.MULTILINE,
)
_NAME_RE = re.compile(r'^name\s*=\s*"(?P<name>[^"]+)"\s*$', re.MULTILINE)


@dataclass(frozen=True)
class ProductIdentity:
    name: str
    version: tuple[int, int, int]


def parse_product_identity(pyproject: str) -> ProductIdentity | None:
    """Parse the distribution identity from ``pyproject.toml`` text.

    The parser is intentionally strict: an absent or malformed name/version is not
    guessed because architecture qualification must fail closed.
    """

    name_match = _NAME_RE.search(pyproject)
    version_match = _VERSION_RE.search(pyproject)
    if name_match is None or version_match is None:
        return None
    return ProductIdentity(
        name=name_match.group("name"),
        version=tuple(
            int(version_match.group(part)) for part in ("major", "minor", "patch")
        ),
    )


def compatible_schema_for_version(version: tuple[int, int, int]) -> int:
    """Return the highest known historical Schema contained by a product version."""

    compatible = [
        schema
        for schema, minimum in KNOWN_SCHEMA_MINIMUM_VERSIONS.items()
        if version >= minimum
    ]
    return max(compatible, default=0)


def stable_identity_findings(
    pyproject: str,
    *,
    minimum_version: tuple[int, int, int] | None = None,
    exact_version: tuple[int, int, int] | None = None,
) -> tuple[str, ...]:
    """Return fail-closed product identity findings for compatibility audits.

    Historical Schema audits validate that the current stable product still contains
    their compatibility layer. They therefore require the one stable distribution
    name and a product version new enough to contain the schema, rather than requiring
    the distribution itself to be renamed for every schema generation.
    """

    if minimum_version is not None and exact_version is not None:
        raise ValueError("minimum_version and exact_version are mutually exclusive")

    identity = parse_product_identity(pyproject)
    if identity is None:
        return ("package_identity", "package_version")

    findings: list[str] = []
    if identity.name != STABLE_PACKAGE_NAME:
        findings.append("package_identity")
    if exact_version is not None:
        if identity.version != exact_version:
            findings.append("package_version")
    elif minimum_version is not None and identity.version < minimum_version:
        findings.append("package_version")
    return tuple(findings)
