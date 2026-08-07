from __future__ import annotations

from dataclasses import dataclass
import re

STABLE_PACKAGE_NAME = "astra-trade-bot"
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
