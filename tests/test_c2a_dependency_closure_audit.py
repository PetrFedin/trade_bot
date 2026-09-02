import json
import re
from pathlib import Path

AUDIT_PATH = Path("C2A_DEPENDENCY_CLOSURE_AUDIT.json")
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def load_audit() -> dict:
    return json.loads(AUDIT_PATH.read_text(encoding="utf-8"))


def test_c2a_dependency_closure_rejects_hidden_strategy_ancestry() -> None:
    audit = load_audit()

    assert audit["schema_version"] == "c2a-dependency-closure-audit-v1"
    assert SHA40.fullmatch(audit["source_heads"]["pr76"])
    assert SHA40.fullmatch(audit["source_heads"]["pr77"])

    components = {row["component"]: row for row in audit["dependency_classification"]}
    assert components["app/execution/bybit_demo_runtime_lease.py"]["first_slice_allowed"] is True
    assert (
        components["app/execution/bybit_demo_postgres_runtime_lease.py"][
            "first_slice_allowed"
        ]
        is True
    )

    excursion = components["app/execution/bybit_demo_postgres_excursion_store.py"]
    assert excursion["first_slice_allowed"] is False
    assert "app.strategy.crypto_perp.CryptoTradePlan" in excursion[
        "transitive_strategy_dependencies"
    ]
    assert excursion["decision"] == "DEFER_TO_C2C_CHECKPOINT_DOMAIN_EXTRACTION"

    provenance = components["app/execution/bybit_demo_postgres_entry_provenance_store.py"]
    assert provenance["first_slice_allowed"] is False
    assert "app.execution.bybit_demo_strategy_selector" in provenance[
        "transitive_dependencies"
    ]
    assert provenance["decision"] == "DEFER_TO_C3_C4_PROVENANCE_EXTRACTION"


def test_first_executable_slice_is_runtime_lease_only_and_non_trading() -> None:
    audit = load_audit()
    slice_ = audit["revised_first_executable_slice"]

    assert slice_["id"] == "C2A0"
    assert slice_["status"] == "READY_FOR_BOUNDED_EXTRACTION_AFTER_C0_PR_MERGE"
    assert slice_["source_artifacts"] == [
        "app/execution/bybit_demo_runtime_lease.py",
        "app/execution/bybit_demo_postgres_runtime_lease.py",
        "migrations/v119/001_bybit_demo_durable_runtime.sql",
    ]
    assert slice_["canonical_test_dsn"] == "ASTRA_TEST_POSTGRES_DSN"

    forbidden = set(slice_["forbidden"])
    assert {
        "BYBIT_NETWORK_CALL",
        "ORDER_WRITE",
        "ARM",
        "OPERATOR_APPROVAL",
        "STRATEGY_IMPORT",
        "CRYPTO_TRADE_PLAN_IMPORT",
        "EXCURSION_TRACKER_IMPORT",
        "PROVENANCE_IMPORT",
        "TERMINAL_PNL_IMPORT",
        "RESEARCH_DATA_IMPORT",
        "MAINNET_CAPABILITY",
    } == forbidden

    behavior = set(slice_["required_behavior"])
    assert "single writer across independent PostgreSQL store instances" in behavior
    assert "no TTL or automatic stale takeover" in behavior
    assert "wrong-owner release fails closed" in behavior
    assert "orphaned durable row blocks later acquire regardless of clock age" in behavior
    assert "live_mainnet_order_routing_allowed=false" in behavior
    assert "order_writes_supported=false" in behavior


def test_historical_migrations_are_not_rewritten_or_overclaimed() -> None:
    audit = load_audit()
    migrations = {row["path"]: row for row in audit["historical_migrations"]}

    v119 = migrations["migrations/v119/001_bybit_demo_durable_runtime.sql"]
    assert v119["sha"] == "9a7f8d6eee89d10673b288e0e6a9bfe276494d8a"
    assert v119["first_slice_allowed"] is True

    v120 = migrations["migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql"]
    assert v120["sha"] == "b337ef19dc7da4a3fcbc0a11a8d6d7d85dff3b00"
    assert v120["first_slice_allowed"] is False
    assert "#107" in v120["note"]

    assert audit["next_gate"] == (
        "QUALIFY_AND_MERGE_PR_106_THEN_CREATE_C2A0_FROM_CANONICAL_MAIN"
    )
