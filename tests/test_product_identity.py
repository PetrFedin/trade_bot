import pytest

from tools.product_identity import (
    STABLE_PACKAGE_NAME,
    compatible_schema_for_version,
    parse_product_identity,
    stable_identity_findings,
)


def project(name: str = STABLE_PACKAGE_NAME, version: str = "7.38.0") -> str:
    return f'[project]\nname = "{name}"\nversion = "{version}"\n'


def test_stable_identity_accepts_historical_minimum_version() -> None:
    assert stable_identity_findings(project(), minimum_version=(7, 30, 0)) == ()
    assert stable_identity_findings(project(), minimum_version=(7, 37, 0)) == ()


def test_stable_identity_requires_exact_name() -> None:
    assert stable_identity_findings(
        project("astra-schema108-asymmetric-signing-authority"),
        minimum_version=(7, 30, 0),
    ) == ("package_identity",)


def test_stable_identity_rejects_old_version() -> None:
    assert stable_identity_findings(project(version="7.36.9"), minimum_version=(7, 37, 0)) == (
        "package_version",
    )


def test_stable_identity_supports_exact_release_contract() -> None:
    assert stable_identity_findings(project(), exact_version=(7, 38, 0)) == ()
    assert stable_identity_findings(project(version="7.38.1"), exact_version=(7, 38, 0)) == (
        "package_version",
    )


def test_schema_compatibility_mapping_is_monotonic_for_known_releases() -> None:
    assert compatible_schema_for_version((7, 29, 99)) == 0
    assert compatible_schema_for_version((7, 30, 0)) == 100
    assert compatible_schema_for_version((7, 35, 0)) == 105
    assert compatible_schema_for_version((7, 38, 0)) == 108
    assert compatible_schema_for_version((7, 39, 0)) == 109
    assert compatible_schema_for_version((7, 99, 0)) == 109


def test_malformed_identity_fails_closed() -> None:
    assert parse_product_identity("[project]\nname='wrong-quotes'\n") is None
    assert stable_identity_findings("[project]\n") == (
        "package_identity",
        "package_version",
    )


def test_version_modes_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        stable_identity_findings(
            project(),
            minimum_version=(7, 30, 0),
            exact_version=(7, 38, 0),
        )
