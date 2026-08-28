from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_IDENTITY_NAME = "CANONICAL_DEMO_OPERATIONAL_DATABASE"
_IDENTITY_RELATION = "astra_bybit_demo_operational_identity_v124"
_IDENTITY_TRIGGERS = (
    "astra_bybit_demo_operational_identity_immutable_v124",
    "astra_bybit_demo_operational_identity_no_truncate_v124",
)


@dataclass(frozen=True)
class BybitDemoOperationalDatabaseIdentity:
    database_instance_id: str
    immutable_record: bool
    diagnostics_only: bool
    order_writes_supported: bool
    live_mainnet_order_routing_allowed: bool

    def validate(self) -> None:
        try:
            parsed = UUID(self.database_instance_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("Bybit Demo logical database identity UUID is invalid") from exc
        if str(parsed) != self.database_instance_id:
            raise ValueError("Bybit Demo logical database identity UUID must be canonical")
        if self.immutable_record is not True:
            raise ValueError("Bybit Demo logical database identity lost immutable marker")
        if self.diagnostics_only is not True:
            raise ValueError("Bybit Demo logical database identity lost diagnostics marker")
        if self.order_writes_supported is not False:
            raise ValueError("Bybit Demo logical database identity cannot support order writes")
        if self.live_mainnet_order_routing_allowed is not False:
            raise ValueError(
                "Bybit Demo logical database identity cannot route mainnet orders"
            )


class PostgresBybitDemoOperationalDatabaseIdentityReader:
    """Read the immutable v124 logical database identity without mutation capability."""

    schema_mutation_supported = False
    order_writes_supported = False
    live_mainnet_order_routing_allowed = False

    def __init__(self, dsn: str) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("Bybit Demo logical database identity DSN is required")
        self._dsn = dsn

    def read_identity(self) -> BybitDemoOperationalDatabaseIdentity:
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        with psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False) as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    _require_schema(cursor)
                    cursor.execute(
                        """SELECT identity_name, database_instance_id::text,
                                  immutable_record, diagnostics_only,
                                  order_writes_supported,
                                  live_mainnet_order_routing_allowed
                           FROM astra_bybit_demo_operational_identity_v124"""
                    )
                    rows = cursor.fetchall()
        if len(rows) != 1:
            raise ValueError("Bybit Demo logical database identity must contain exactly one row")
        row = rows[0]
        if row["identity_name"] != _IDENTITY_NAME:
            raise ValueError("Bybit Demo logical database identity name is invalid")
        identity = BybitDemoOperationalDatabaseIdentity(
            database_instance_id=row["database_instance_id"],
            immutable_record=row["immutable_record"],
            diagnostics_only=row["diagnostics_only"],
            order_writes_supported=row["order_writes_supported"],
            live_mainnet_order_routing_allowed=row["live_mainnet_order_routing_allowed"],
        )
        identity.validate()
        return identity


def _require_schema(cursor) -> None:
    cursor.execute("SELECT to_regclass(%s) AS relation", (_IDENTITY_RELATION,))
    relation = cursor.fetchone()
    if relation is None or relation["relation"] is None:
        raise RuntimeError("Bybit Demo logical database identity v124 relation is missing")
    cursor.execute(
        """SELECT count(*) AS count
           FROM pg_trigger
           WHERE NOT tgisinternal AND tgname = ANY(%s)""",
        (list(_IDENTITY_TRIGGERS),),
    )
    trigger = cursor.fetchone()
    if trigger is None or int(trigger["count"]) != len(_IDENTITY_TRIGGERS):
        raise RuntimeError("Bybit Demo logical database identity v124 triggers are missing")


__all__ = [
    "BybitDemoOperationalDatabaseIdentity",
    "PostgresBybitDemoOperationalDatabaseIdentityReader",
]
