from __future__ import annotations

from pathlib import Path

_ROOT = Path(".github/workflows")


def _workflow(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


def test_readiness_emits_database_and_account_zone_sidecar() -> None:
    text = _workflow("bybit-demo-activation-readiness.yml")

    assert "BYBIT_DEMO_ZONE_BINDING_SECRET" in text
    assert "--producer activation_readiness" in text
    assert "--include-demo-account" in text
    assert "artifacts/bybit-demo-operational-zone-binding.json" in text


def test_session_sidecars_bind_database_only_for_both_modes() -> None:
    text = _workflow("bybit-demo-session-start.yml")
    secret_binding = (
        "BYBIT_DEMO_ZONE_BINDING_SECRET: "
        "${{ secrets.BYBIT_DEMO_ZONE_BINDING_SECRET }}"
    )

    assert text.count("--producer session_start") == 2
    assert text.count(secret_binding) == 2
    assert "--include-demo-account" not in text
    assert text.count("artifacts/bybit-demo-operational-zone-binding.json") >= 4


def test_supervisor_and_entry_bind_database_and_demo_account() -> None:
    supervisor = _workflow("bybit-demo-persistent-supervisor.yml")
    entry = _workflow("bybit-operator-approved-demo-execution.yml")

    assert "--producer supervisor" in supervisor
    assert "--include-demo-account" in supervisor
    assert "BYBIT_DEMO_ZONE_BINDING_SECRET" in supervisor
    assert "--producer operational_entry" in entry
    assert "--include-demo-account" in entry
    assert "BYBIT_DEMO_ZONE_BINDING_SECRET" in entry


def test_control_binds_only_arm_and_halt_with_exact_producer_identity() -> None:
    text = _workflow("bybit-demo-control-plane.yml")

    assert "if: inputs.mode == 'arm' || inputs.mode == 'halt'" in text
    assert 'ZONE_PRODUCER="arm_control"' in text
    assert 'ZONE_PRODUCER="halt_control"' in text
    assert "--producer \"$ZONE_PRODUCER\"" in text
    assert "--include-demo-account" in text
    assert "BYBIT_DEMO_ZONE_BINDING_SECRET" in text


def test_recovery_binds_database_without_gaining_bybit_credentials() -> None:
    text = _workflow("bybit-demo-runtime-lease-recovery.yml")
    operational = text.split("\n  operational:\n", 1)[1]

    assert "--producer recovery_receipt" in operational
    assert "BYBIT_DEMO_ZONE_BINDING_SECRET" in operational
    assert "--include-demo-account" not in operational
    assert "BYBIT_DEMO_READONLY_API_KEY" not in operational
    assert "BYBIT_DEMO_TRADING_API_KEY" not in operational


def test_release_assembler_requires_zone_sidecar_for_every_supplied_stage() -> None:
    text = _workflow("bybit-demo-operational-release-evidence.yml")

    required_flags = (
        "--activation-readiness-zone",
        "--session-start-zone",
        "--supervisor-zone",
        "--arm-control-zone",
        "--operational-entry-zone",
        "--halt-control-zone",
        "--recovery-receipt-zone",
    )
    for flag in required_flags:
        assert flag in text
    assert text.count("bybit-demo-operational-zone-binding.json") >= 7
    assert "BYBIT_DEMO_ZONE_BINDING_SECRET" not in text
    assert "secrets." not in text
