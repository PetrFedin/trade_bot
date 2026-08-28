from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlsplit

from app.execution.bybit_demo_same_account import (
    BybitDemoAccountIdentityInspector,
    BybitDemoApiAccountIdentity,
)

try:
    from psycopg.conninfo import conninfo_to_dict
except ImportError:  # pragma: no cover - optional dependency boundary
    conninfo_to_dict = None

_ALLOWED_PRODUCERS = frozenset(
    {
        "activation_readiness",
        "session_start",
        "supervisor",
        "arm_control",
        "operational_entry",
        "halt_control",
        "recovery_receipt",
    }
)
_BINDING_KEY_MARKER = b"BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_KEY_V1"


@dataclass(frozen=True)
class BybitDemoOperationalZoneBinding:
    producer: str
    git_sha: str
    observed_at: datetime
    binding_key_marker_sha256: str
    database_binding_sha256: str | None
    demo_account_binding_sha256: str | None
    order_writes_supported: bool = False
    live_mainnet_order_routing_allowed: bool = False

    @property
    def database_binding_present(self) -> bool:
        return self.database_binding_sha256 is not None

    @property
    def demo_account_binding_present(self) -> bool:
        return self.demo_account_binding_sha256 is not None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "BYBIT_DEMO_OPERATIONAL_ZONE_BINDING_V1",
            "status": "BOUND",
            "passed": True,
            "producer": self.producer,
            "git_sha": self.git_sha,
            "observed_at": self.observed_at.isoformat(),
            "binding_algorithm": "HMAC-SHA256",
            "binding_key_marker_sha256": self.binding_key_marker_sha256,
            "database_binding_present": self.database_binding_present,
            "database_binding_sha256": self.database_binding_sha256,
            "demo_account_binding_present": self.demo_account_binding_present,
            "demo_account_binding_sha256": self.demo_account_binding_sha256,
            "order_writes_supported": self.order_writes_supported,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


def build_bybit_demo_operational_zone_binding(
    *,
    producer: str,
    git_sha: str,
    binding_secret: str,
    database_dsn: str | None = None,
    account_inspector: BybitDemoAccountIdentityInspector | None = None,
    observed_at: datetime | None = None,
) -> BybitDemoOperationalZoneBinding:
    _validate_producer(producer)
    _validate_git_sha(git_sha)
    secret = _binding_secret_bytes(binding_secret)
    moment = _utc_now(observed_at)

    database_binding = None
    if database_dsn is not None:
        database_binding = _hmac_json(
            secret,
            _canonical_database_resource(database_dsn),
        )

    account_binding = None
    if account_inspector is not None:
        if not isinstance(account_inspector, BybitDemoAccountIdentityInspector):
            raise ValueError("Bybit Demo zone account binding requires exact GET-only inspector")
        identity = account_inspector.inspect()
        account_binding = bind_bybit_demo_account_identity(
            identity,
            binding_secret=binding_secret,
        )

    if database_binding is None and account_binding is None:
        raise ValueError("Bybit Demo operational zone binding requires at least one resource")

    return BybitDemoOperationalZoneBinding(
        producer=producer,
        git_sha=git_sha,
        observed_at=moment,
        binding_key_marker_sha256=hmac.new(
            secret,
            _BINDING_KEY_MARKER,
            hashlib.sha256,
        ).hexdigest(),
        database_binding_sha256=database_binding,
        demo_account_binding_sha256=account_binding,
    )


def bind_bybit_demo_account_identity(
    identity: BybitDemoApiAccountIdentity,
    *,
    binding_secret: str,
) -> str:
    if not isinstance(identity, BybitDemoApiAccountIdentity):
        raise ValueError("Bybit Demo account zone binding requires exact account identity")
    identity.validate()
    secret = _binding_secret_bytes(binding_secret)
    payload = {
        "namespace": "BYBIT_DEMO_ACCOUNT_RESOURCE_V1",
        "user_id": identity.user_id,
        "parent_uid": identity.parent_uid,
        "is_master": identity.is_master,
    }
    return _hmac_json(secret, payload)


def bind_bybit_demo_database_dsn(database_dsn: str, *, binding_secret: str) -> str:
    secret = _binding_secret_bytes(binding_secret)
    return _hmac_json(secret, _canonical_database_resource(database_dsn))


def _canonical_database_resource(database_dsn: str) -> dict[str, str]:
    if not isinstance(database_dsn, str) or not database_dsn.strip():
        raise ValueError("Bybit Demo operational database DSN is required")
    text = database_dsn.strip()
    values = _parse_conninfo(text)
    host = (values.get("host") or "").strip().lower()
    hostaddr = (values.get("hostaddr") or "").strip().lower()
    dbname = (values.get("dbname") or values.get("database") or "").strip()
    port = (values.get("port") or "5432").strip()
    sslmode = (values.get("sslmode") or "").strip().lower()
    target_session_attrs = (values.get("target_session_attrs") or "").strip().lower()
    if not host and not hostaddr:
        raise ValueError("Bybit Demo operational database DSN must identify a host")
    if not dbname:
        raise ValueError("Bybit Demo operational database DSN must identify a database")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("Bybit Demo operational database DSN port is invalid")
    return {
        "namespace": "BYBIT_DEMO_DATABASE_RESOURCE_V1",
        "host": host,
        "hostaddr": hostaddr,
        "port": str(int(port)),
        "dbname": dbname,
        "sslmode": sslmode,
        "target_session_attrs": target_session_attrs,
    }


def _parse_conninfo(text: str) -> dict[str, str]:
    if text.startswith(("postgresql://", "postgres://")):
        parsed = urlsplit(text)
        database = unquote(parsed.path.lstrip("/"))
        values = {
            "host": parsed.hostname or "",
            "port": str(parsed.port or 5432),
            "dbname": database,
        }
        query = {}
        pairs = parsed.query.split("&") if parsed.query else ()
        for pair in pairs:
            key, separator, value = pair.partition("=")
            if separator:
                query[unquote(key)] = unquote(value)
        for name in ("hostaddr", "sslmode", "target_session_attrs"):
            if name in query:
                values[name] = query[name]
        return values
    if conninfo_to_dict is None:
        raise ValueError("keyword PostgreSQL DSN parsing requires psycopg")
    parsed = conninfo_to_dict(text)
    return {str(key): str(value) for key, value in parsed.items() if value is not None}


def _hmac_json(secret: bytes, payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hmac.new(secret, encoded, hashlib.sha256).hexdigest()


def _binding_secret_bytes(value: str) -> bytes:
    if not isinstance(value, str) or value != value.strip() or len(value) < 32:
        raise ValueError(
            "Bybit Demo operational zone binding secret must be at least 32 characters"
        )
    return value.encode("utf-8")


def _validate_producer(value: str) -> None:
    if value not in _ALLOWED_PRODUCERS:
        raise ValueError("Bybit Demo operational zone producer is invalid")


def _validate_git_sha(value: str) -> None:
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("Bybit Demo operational zone Git SHA must be lowercase 40-char hex")


def _utc_now(value: datetime | None) -> datetime:
    moment = datetime.now(UTC) if value is None else value
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("Bybit Demo operational zone time must be timezone-aware")
    return moment.astimezone(UTC)


__all__ = [
    "BybitDemoOperationalZoneBinding",
    "bind_bybit_demo_account_identity",
    "bind_bybit_demo_database_dsn",
    "build_bybit_demo_operational_zone_binding",
]
