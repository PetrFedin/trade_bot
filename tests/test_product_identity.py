from tools.product_identity import (
    STABLE_PACKAGE_NAME,
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


def test_malformed_identity_fails_closed() -> None:
    assert parse_product_identity("[project]\nname='wrong-quotes'\n") is None
    assert stable_identity_findings("[project]\n") == (
        "package_identity",
        "package_version",
    )


def test_version_modes_are_mutually_exclusive() -> None:
    try:
        stable_identity_findings(
            project(),
            minimum_version=(7, 30, 0),
            exact_version=(7, 38, 0),
        )
    except ValueError as exc:
        assert "mutually exclusive" in str(exc)
    else:
        raise AssertionError("expected ValueError")
