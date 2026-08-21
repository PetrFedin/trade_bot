from __future__ import annotations

from collections.abc import Sequence

from app.oms.store import OrderRecord, OrderState

try:
    import psycopg
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None

_BYBIT_ENTRY_PREFIX = "ASTRA-DEMO-E-"
_RECOVERY_CANDIDATE_STATES = (
    OrderState.SUBMIT_STARTED,
    OrderState.UNCERTAIN,
    OrderState.RECONCILING,
    OrderState.ACKNOWLEDGED,
    OrderState.PARTIALLY_FILLED,
    OrderState.FILLED,
)


class PostgresBybitEntryRecoveryCandidateReader:
    """Read-only discovery of entries that may have crashed before durable trade handoff.

    This deliberately does not change the OMS unresolved-SLO definition. It widens only restart
    recovery discovery to include an ENTRY whose broker acknowledgement/fill was already persisted
    but whose canonical trade checkpoint/terminal handoff may still be missing.
    """

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, entry_oms) -> None:
        if getattr(entry_oms, "live_mainnet_order_routing_allowed", True) is not False:
            raise ValueError("recovery candidate reader rejected mainnet-capable OMS")
        dsn = getattr(entry_oms, "dsn", None)
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("recovery candidate reader requires PostgreSQL OMS DSN")
        if psycopg is None:
            raise RuntimeError("install the postgresql extra to use recovery candidate reader")
        self.entry_oms = entry_oms
        self.dsn = dsn

    def load_candidates(self, *, limit: int = 8) -> tuple[OrderRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 64:
            raise ValueError("recovery candidate limit must be within [1, 64]")
        states: Sequence[str] = tuple(state.value for state in _RECOVERY_CANDIDATE_STATES)
        if psycopg is None:  # pragma: no cover - constructor already rejects this boundary.
            raise RuntimeError("PostgreSQL dependency is unavailable")
        with psycopg.connect(self.dsn, autocommit=True) as connection:
            rows = connection.execute(
                """SELECT o.intent_id
                FROM astra_oms_orders AS o
                WHERE o.client_order_id LIKE %s
                  AND o.state = ANY(%s)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM astra_bybit_terminal_evidence AS terminal
                      WHERE terminal.entry_order_link_id = o.client_order_id
                  )
                ORDER BY o.updated_at DESC, o.intent_id DESC
                LIMIT %s""",
                (f"{_BYBIT_ENTRY_PREFIX}%", list(states), limit),
            ).fetchall()
        records: list[OrderRecord] = []
        for row in rows:
            intent_id = str(row[0])
            record = self.entry_oms.get(intent_id)
            if record is None:
                raise RuntimeError("recovery candidate disappeared from canonical OMS")
            if not record.client_order_id.startswith(_BYBIT_ENTRY_PREFIX):
                raise ValueError("recovery candidate is not a deterministic Bybit ENTRY")
            if record.state not in _RECOVERY_CANDIDATE_STATES:
                raise RuntimeError("recovery candidate state changed during read")
            records.append(record)
        return tuple(records)
