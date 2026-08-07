from __future__ import annotations

from typing import Protocol

from app.oms.postgres import PostgresOmsStore
from app.oms.protocols import OmsStore
from app.oms.store import DurableOmsStore, OrderRecord


class IndexedOmsStore(OmsStore, Protocol):
    """OMS port capable of resolving account-wide broker events after restart."""

    def get_by_client_order_id(self, client_order_id: str) -> OrderRecord | None: ...


class IndexedDurableOmsStore(DurableOmsStore):
    def get_by_client_order_id(self, client_order_id: str) -> OrderRecord | None:
        normalized = client_order_id.strip()
        if not normalized:
            raise ValueError("client_order_id is required")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM oms_orders WHERE client_order_id=?",
                (normalized,),
            ).fetchone()
            return None if row is None else self._row(row)
        finally:
            connection.close()


class IndexedPostgresOmsStore(PostgresOmsStore):
    def get_by_client_order_id(self, client_order_id: str) -> OrderRecord | None:
        normalized = client_order_id.strip()
        if not normalized:
            raise ValueError("client_order_id is required")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM astra_oms_orders WHERE client_order_id=%s",
                    (normalized,),
                )
                row = cursor.fetchone()
                return None if row is None else self._row(row)
