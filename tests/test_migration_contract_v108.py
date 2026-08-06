from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL = ROOT / "migrations/v108/001_asymmetric_signing_authority.sql"
PACKAGED = ROOT / "app/platform_assets/v108/migrations/001_asymmetric_signing_authority.sql"


def test_migration_is_packaged_byte_for_byte() -> None:
    assert CANONICAL.read_bytes() == PACKAGED.read_bytes()


def test_migration_has_replay_keyring_bundle_and_append_only_event_contracts() -> None:
    sql = CANONICAL.read_text(encoding="utf-8")
    for table in (
        "astra_signing_keyring_v108",
        "astra_signature_replay_v108",
        "astra_rollout_authorization_v108",
        "astra_signing_event_v108",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
        assert f"REVOKE ALL ON {table} FROM PUBLIC" in sql
    assert "nonce text NOT NULL UNIQUE" in sql
    assert "command_digest text NOT NULL UNIQUE" in sql
    assert "BEFORE UPDATE OR DELETE ON astra_signing_event_v108" in sql
