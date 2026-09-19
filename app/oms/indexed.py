from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Protocol

from app.domain.trading import Side
from app.oms.postgres import PostgresOmsStore, psycopg
from app.oms.protocols import OmsStore
from app.oms.risk_reservations import (
    PendingBuyExposure,
    RiskReservationBudget,
    RiskReservationRejected,
    evaluate_buy_reservation,
)
from app.oms.store import DurableOmsStore, OrderRecord, OrderState

_RISK_RESERVING_STATES = (
    OrderState.RISK_APPROVED,
    OrderState.OUTBOXED,
    OrderState.SUBMIT_STARTED,
    OrderState.ACKNOWLEDGED,
    OrderState.PARTIALLY_FILLED,
    OrderState.CANCEL_REQUESTED,
    OrderState.UNCERTAIN,
    OrderState.RECONCILING,
    OrderState.RECONCILED,
    OrderState.MANUAL,
)
_POSTGRES_RISK_RESERVATION_LOCK_KEY = 0x41535452


def _reservation_request(
    record: OrderRecord,
    budget: RiskReservationBudget,
) -> dict[str, object]:
    return {
        "intent_id": record.intent_id,
        "symbol": record.symbol,
        "side": record.side.value,
        "quantity": str(record.quantity),
        "limit_price": str(record.limit_price),
        "available_cash": None if budget.available_cash is None else str(budget.available_cash),
        "current_symbol_notional": str(budget.current_symbol_notional),
        "current_gross_notional": str(budget.current_gross_notional),
        "maximum_symbol_notional": str(budget.maximum_symbol_notional),
        "maximum_gross_notional": str(budget.maximum_gross_notional),
    }


def _validate_reservation_replay(
    *,
    stored_intent_id: object,
    stored_event_type: object,
    stored_payload: dict[str, object],
    intent_id: str,
    request: dict[str, object],
) -> None:
    if (
        str(stored_intent_id) != intent_id
        or str(stored_event_type) != OrderState.RISK_APPROVED.value
        or stored_payload.get("reservation_request") != request
    ):
        raise ValueError("OMS_EVENT_ID_CONFLICT")


class IndexedOmsStore(OmsStore, Protocol):
    """OMS port with durable broker identity lookup and atomic risk reservation."""

    def get_by_client_order_id(self, client_order_id: str) -> OrderRecord | None: ...

    def get_by_broker_order_id(self, broker_order_id: str) -> OrderRecord | None: ...

    def operational_blocking_count(self) -> int:
        """Count OMS states that require operator/reconciliation attention."""
        ...

    def register_replace_successor(
        self,
        *,
        intent_id: str,
        mutation_id: str,
        predecessor_broker_order_id: str,
        successor_broker_order_id: str,
        occurred_at: datetime,
    ) -> None: ...

    def approve_risk_with_reservation(
        self,
        intent_id: str,
        *,
        event_id: str,
        occurred_at: datetime,
        budget: RiskReservationBudget,
    ) -> OrderRecord: ...


class IndexedDurableOmsStore(DurableOmsStore):
    def __init__(self, path: str | Path) -> None:
        super().__init__(path)
        self._initialize_broker_order_lineage()

    def _initialize_broker_order_lineage(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS oms_broker_order_identities (
                    broker_order_id TEXT PRIMARY KEY,
                    intent_id TEXT NOT NULL REFERENCES oms_orders(intent_id) ON DELETE RESTRICT,
                    predecessor_broker_order_id TEXT
                        REFERENCES oms_broker_order_identities(broker_order_id) ON DELETE RESTRICT,
                    replace_mutation_id TEXT UNIQUE,
                    generation INTEGER NOT NULL CHECK (generation >= 0),
                    created_at TEXT NOT NULL,
                    CHECK (
                        (generation = 0 AND predecessor_broker_order_id IS NULL
                         AND replace_mutation_id IS NULL)
                        OR
                        (generation > 0 AND predecessor_broker_order_id IS NOT NULL
                         AND replace_mutation_id IS NOT NULL)
                    )
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_oms_broker_identity_primary
                    ON oms_broker_order_identities(intent_id)
                    WHERE generation = 0;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_oms_broker_identity_successor
                    ON oms_broker_order_identities(predecessor_broker_order_id)
                    WHERE predecessor_broker_order_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_oms_broker_identity_intent_generation
                    ON oms_broker_order_identities(intent_id, generation);
                CREATE TRIGGER IF NOT EXISTS oms_broker_order_identities_no_update
                BEFORE UPDATE ON oms_broker_order_identities
                BEGIN
                    SELECT RAISE(ABORT, 'oms_broker_order_identities is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS oms_broker_order_identities_no_delete
                BEFORE DELETE ON oms_broker_order_identities
                BEGIN
                    SELECT RAISE(ABORT, 'oms_broker_order_identities is append-only');
                END;
                """
            )
            rows = connection.execute(
                """SELECT intent_id, broker_order_id, updated_at FROM oms_orders
                WHERE broker_order_id <> '' ORDER BY intent_id"""
            ).fetchall()
            for row in rows:
                intent_id = str(row["intent_id"])
                broker_order_id = str(row["broker_order_id"])
                existing = connection.execute(
                    """SELECT intent_id FROM oms_broker_order_identities
                    WHERE broker_order_id=?""",
                    (broker_order_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["intent_id"]) != intent_id:
                        raise ValueError("OMS_BROKER_ORDER_ID_CONFLICT")
                    continue
                primary = connection.execute(
                    """SELECT broker_order_id FROM oms_broker_order_identities
                    WHERE intent_id=? AND generation=0""",
                    (intent_id,),
                ).fetchone()
                if primary is None:
                    connection.execute(
                        """INSERT INTO oms_broker_order_identities
                        (broker_order_id, intent_id, predecessor_broker_order_id,
                         replace_mutation_id, generation, created_at)
                        VALUES (?, ?, NULL, NULL, 0, ?)""",
                        (broker_order_id, intent_id, str(row["updated_at"])),
                    )
        finally:
            connection.close()

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

    def get_by_broker_order_id(self, broker_order_id: str) -> OrderRecord | None:
        normalized = broker_order_id.strip()
        if not normalized:
            raise ValueError("broker_order_id is required")
        connection = self._connect()
        try:
            identity = connection.execute(
                """SELECT intent_id FROM oms_broker_order_identities
                WHERE broker_order_id=?""",
                (normalized,),
            ).fetchone()
            if identity is not None:
                row = connection.execute(
                    "SELECT * FROM oms_orders WHERE intent_id=?",
                    (str(identity["intent_id"]),),
                ).fetchone()
                if row is None:
                    raise RuntimeError("OMS broker identity points to missing order")
                return self._row(row)
            rows = connection.execute(
                "SELECT * FROM oms_orders WHERE broker_order_id=? ORDER BY intent_id",
                (normalized,),
            ).fetchall()
            if len(rows) > 1:
                raise ValueError("OMS_BROKER_ORDER_ID_CONFLICT")
            return None if not rows else self._row(rows[0])
        finally:
            connection.close()

    def operational_blocking_count(self) -> int:
        blocking = tuple(
            state.value
            for state in (
                OrderState.UNCERTAIN,
                OrderState.RECONCILING,
                OrderState.MANUAL,
            )
        )
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT COUNT(*) AS count FROM oms_orders
                WHERE state IN (?, ?, ?)""",
                blocking,
            ).fetchone()
            return 0 if row is None else int(row["count"])
        finally:
            connection.close()

    def register_replace_successor(
        self,
        *,
        intent_id: str,
        mutation_id: str,
        predecessor_broker_order_id: str,
        successor_broker_order_id: str,
        occurred_at: datetime,
    ) -> None:
        predecessor = predecessor_broker_order_id.strip()
        successor = successor_broker_order_id.strip()
        if not intent_id.strip() or not mutation_id.strip():
            raise ValueError("intent_id and mutation_id are required")
        if not predecessor or not successor:
            raise ValueError("broker order lineage identity is required")
        moment = self._now(occurred_at)
        with self._transaction() as connection:
            proof = connection.execute(
                """SELECT m.intent_id, m.kind, m.state, m.broker_order_id, o.payload
                FROM oms_order_mutations AS m
                JOIN oms_order_mutation_outbox AS o ON o.mutation_id=m.mutation_id
                WHERE m.mutation_id=?""",
                (mutation_id,),
            ).fetchone()
            if proof is None:
                raise ValueError("REPLACE_LINEAGE_NOT_PROVEN")
            try:
                request_payload = dict(json.loads(str(proof["payload"])))
            except (TypeError, ValueError, json.JSONDecodeError):
                raise ValueError("REPLACE_LINEAGE_NOT_PROVEN") from None
            if (
                str(proof["intent_id"]) != intent_id
                or str(proof["kind"]) != "REPLACE"
                or str(proof["state"]) != "SUCCEEDED"
                or str(proof["broker_order_id"]) != successor
                or str(request_payload.get("broker_order_id", "")) != predecessor
            ):
                raise ValueError("REPLACE_LINEAGE_NOT_PROVEN")

            source = connection.execute(
                """SELECT intent_id, generation FROM oms_broker_order_identities
                WHERE broker_order_id=?""",
                (predecessor,),
            ).fetchone()
            if source is None:
                order = connection.execute(
                    "SELECT broker_order_id, updated_at FROM oms_orders WHERE intent_id=?",
                    (intent_id,),
                ).fetchone()
                if order is None or str(order["broker_order_id"]) != predecessor:
                    raise ValueError("REPLACE_PREDECESSOR_NOT_PROVEN")
                primary = connection.execute(
                    """SELECT broker_order_id FROM oms_broker_order_identities
                    WHERE intent_id=? AND generation=0""",
                    (intent_id,),
                ).fetchone()
                if primary is not None and str(primary["broker_order_id"]) != predecessor:
                    raise ValueError("REPLACE_PREDECESSOR_NOT_PROVEN")
                if primary is None:
                    connection.execute(
                        """INSERT INTO oms_broker_order_identities
                        (broker_order_id, intent_id, predecessor_broker_order_id,
                         replace_mutation_id, generation, created_at)
                        VALUES (?, ?, NULL, NULL, 0, ?)""",
                        (predecessor, intent_id, str(order["updated_at"])),
                    )
                source_generation = 0
            else:
                if str(source["intent_id"]) != intent_id:
                    raise ValueError("REPLACE_PREDECESSOR_NOT_PROVEN")
                source_generation = int(source["generation"])

            if successor == predecessor:
                return
            existing = connection.execute(
                """SELECT intent_id, predecessor_broker_order_id, replace_mutation_id,
                          generation
                FROM oms_broker_order_identities WHERE broker_order_id=?""",
                (successor,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["intent_id"]) == intent_id
                    and str(existing["predecessor_broker_order_id"]) == predecessor
                    and str(existing["replace_mutation_id"]) == mutation_id
                    and int(existing["generation"]) == source_generation + 1
                ):
                    return
                raise ValueError("BROKER_ORDER_LINEAGE_CONFLICT")
            try:
                connection.execute(
                    """INSERT INTO oms_broker_order_identities
                    (broker_order_id, intent_id, predecessor_broker_order_id,
                     replace_mutation_id, generation, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        successor,
                        intent_id,
                        predecessor,
                        mutation_id,
                        source_generation + 1,
                        moment.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("BROKER_ORDER_LINEAGE_CONFLICT") from exc

    def approve_risk_with_reservation(
        self,
        intent_id: str,
        *,
        event_id: str,
        occurred_at: datetime,
        budget: RiskReservationBudget,
    ) -> OrderRecord:
        """Atomically reserve pending BUY capacity before RISK_APPROVED."""

        budget.validate()
        moment = self._now(occurred_at)
        states = tuple(state.value for state in _RISK_RESERVING_STATES)
        with self._transaction() as connection:
            current = self._load_for_update(connection, intent_id)
            request = _reservation_request(current, budget)
            existing = connection.execute(
                "SELECT intent_id, event_type, payload FROM oms_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
            if existing is not None:
                try:
                    stored_payload = dict(json.loads(str(existing["payload"])))
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise ValueError("OMS_EVENT_ID_CONFLICT") from None
                _validate_reservation_replay(
                    stored_intent_id=existing["intent_id"],
                    stored_event_type=existing["event_type"],
                    stored_payload=stored_payload,
                    intent_id=intent_id,
                    request=request,
                )
                return current
            self._validate_transition(current.state, OrderState.RISK_APPROVED)
            if current.side is Side.BUY:
                rows = connection.execute(
                    """SELECT * FROM oms_orders
                    WHERE side='BUY'
                      AND state IN (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    states,
                ).fetchall()
                active_buys = tuple(
                    PendingBuyExposure(
                        symbol=record.symbol,
                        remaining_notional=(record.quantity - record.filled_quantity)
                        * record.limit_price,
                    )
                    for record in (self._row(row) for row in rows)
                )
                evaluation = evaluate_buy_reservation(
                    symbol=current.symbol,
                    candidate_notional=current.quantity * current.limit_price,
                    budget=budget,
                    active_buys=active_buys,
                )
                if not evaluation.approved:
                    raise RiskReservationRejected(evaluation.reasons)
                payload = evaluation.event_payload()
            else:
                payload = {"reservation": {"approved": True, "kind": "SELL_NO_CAPACITY_CREDIT"}}
            payload["reservation_request"] = request
            connection.execute(
                """UPDATE oms_orders
                SET state=?, version=version+1, updated_at=? WHERE intent_id=?""",
                (OrderState.RISK_APPROVED.value, moment.isoformat(), intent_id),
            )
            self._append_event(
                connection,
                event_id=event_id,
                intent_id=intent_id,
                event_type=OrderState.RISK_APPROVED.value,
                payload=payload,
                occurred_at=moment,
            )
            return self._load_for_update(connection, intent_id)


class IndexedPostgresOmsStore(PostgresOmsStore):
    def migrate(self, path: str | Path | None = None) -> None:
        if path is not None:
            super().migrate(path)
            return
        super().migrate()
        sql = Path("migrations/product/012_broker_order_lineage.sql").read_text(
            encoding="utf-8"
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

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

    def get_by_broker_order_id(self, broker_order_id: str) -> OrderRecord | None:
        normalized = broker_order_id.strip()
        if not normalized:
            raise ValueError("broker_order_id is required")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT intent_id FROM astra_broker_order_identities
                    WHERE broker_order_id=%s""",
                    (normalized,),
                )
                identity = cursor.fetchone()
                if identity is not None:
                    cursor.execute(
                        "SELECT * FROM astra_oms_orders WHERE intent_id=%s",
                        (str(identity["intent_id"]),),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise RuntimeError("OMS broker identity points to missing order")
                    return self._row(row)
                cursor.execute(
                    """SELECT * FROM astra_oms_orders
                    WHERE broker_order_id=%s ORDER BY intent_id LIMIT 2""",
                    (normalized,),
                )
                rows = cursor.fetchall()
                if len(rows) > 1:
                    raise ValueError("OMS_BROKER_ORDER_ID_CONFLICT")
                return None if not rows else self._row(rows[0])

    def operational_blocking_count(self) -> int:
        blocking = (
            OrderState.UNCERTAIN.value,
            OrderState.RECONCILING.value,
            OrderState.MANUAL.value,
        )
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """SELECT COUNT(*) AS count FROM astra_oms_orders
                    WHERE state IN (%s, %s, %s)""",
                    blocking,
                )
                row = cursor.fetchone()
                return 0 if row is None else int(row["count"])

    def register_replace_successor(
        self,
        *,
        intent_id: str,
        mutation_id: str,
        predecessor_broker_order_id: str,
        successor_broker_order_id: str,
        occurred_at: datetime,
    ) -> None:
        predecessor = predecessor_broker_order_id.strip()
        successor = successor_broker_order_id.strip()
        if not intent_id.strip() or not mutation_id.strip():
            raise ValueError("intent_id and mutation_id are required")
        if not predecessor or not successor:
            raise ValueError("broker order lineage identity is required")
        moment = self._now(occurred_at)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT m.intent_id, m.kind, m.state, m.broker_order_id, o.payload
                        FROM astra_order_mutations AS m
                        JOIN astra_order_mutation_outbox AS o
                          ON o.mutation_id=m.mutation_id
                        WHERE m.mutation_id=%s
                        FOR UPDATE OF m""",
                        (mutation_id,),
                    )
                    proof = cursor.fetchone()
                    if proof is None:
                        raise ValueError("REPLACE_LINEAGE_NOT_PROVEN")
                    request_payload = dict(proof["payload"])
                    if (
                        str(proof["intent_id"]) != intent_id
                        or str(proof["kind"]) != "REPLACE"
                        or str(proof["state"]) != "SUCCEEDED"
                        or str(proof["broker_order_id"]) != successor
                        or str(request_payload.get("broker_order_id", "")) != predecessor
                    ):
                        raise ValueError("REPLACE_LINEAGE_NOT_PROVEN")

                    cursor.execute(
                        """SELECT intent_id, generation
                        FROM astra_broker_order_identities
                        WHERE broker_order_id=%s FOR UPDATE""",
                        (predecessor,),
                    )
                    source = cursor.fetchone()
                    if source is None:
                        cursor.execute(
                            """SELECT broker_order_id, updated_at FROM astra_oms_orders
                            WHERE intent_id=%s FOR UPDATE""",
                            (intent_id,),
                        )
                        order = cursor.fetchone()
                        if order is None or str(order["broker_order_id"]) != predecessor:
                            raise ValueError("REPLACE_PREDECESSOR_NOT_PROVEN")
                        cursor.execute(
                            """SELECT broker_order_id FROM astra_broker_order_identities
                            WHERE intent_id=%s AND generation=0 FOR UPDATE""",
                            (intent_id,),
                        )
                        primary = cursor.fetchone()
                        if (
                            primary is not None
                            and str(primary["broker_order_id"]) != predecessor
                        ):
                            raise ValueError("REPLACE_PREDECESSOR_NOT_PROVEN")
                        if primary is None:
                            cursor.execute(
                                """INSERT INTO astra_broker_order_identities
                                (broker_order_id, intent_id, predecessor_broker_order_id,
                                 replace_mutation_id, generation, created_at)
                                VALUES (%s, %s, NULL, NULL, 0, %s)""",
                                (predecessor, intent_id, order["updated_at"]),
                            )
                        source_generation = 0
                    else:
                        if str(source["intent_id"]) != intent_id:
                            raise ValueError("REPLACE_PREDECESSOR_NOT_PROVEN")
                        source_generation = int(source["generation"])

                    if successor == predecessor:
                        return
                    cursor.execute(
                        """SELECT intent_id, predecessor_broker_order_id,
                                  replace_mutation_id, generation
                        FROM astra_broker_order_identities
                        WHERE broker_order_id=%s FOR UPDATE""",
                        (successor,),
                    )
                    existing = cursor.fetchone()
                    if existing is not None:
                        if (
                            str(existing["intent_id"]) == intent_id
                            and str(existing["predecessor_broker_order_id"]) == predecessor
                            and str(existing["replace_mutation_id"]) == mutation_id
                            and int(existing["generation"]) == source_generation + 1
                        ):
                            return
                        raise ValueError("BROKER_ORDER_LINEAGE_CONFLICT")
                    try:
                        cursor.execute(
                            """INSERT INTO astra_broker_order_identities
                            (broker_order_id, intent_id, predecessor_broker_order_id,
                             replace_mutation_id, generation, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s)""",
                            (
                                successor,
                                intent_id,
                                predecessor,
                                mutation_id,
                                source_generation + 1,
                                moment,
                            ),
                        )
                    except Exception as exc:
                        if psycopg is not None and isinstance(
                            exc, psycopg.errors.UniqueViolation
                        ):
                            raise ValueError("BROKER_ORDER_LINEAGE_CONFLICT") from exc
                        raise

    def approve_risk_with_reservation(
        self,
        intent_id: str,
        *,
        event_id: str,
        occurred_at: datetime,
        budget: RiskReservationBudget,
    ) -> OrderRecord:
        """Serialize account-wide approval and reserve existing pending BUY risk."""

        budget.validate()
        moment = self._now(occurred_at)
        states = [state.value for state in _RISK_RESERVING_STATES]
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_xact_lock(%s)",
                        (_POSTGRES_RISK_RESERVATION_LOCK_KEY,),
                    )
                    current = self._load_for_update(cursor, intent_id)
                    request = _reservation_request(current, budget)
                    cursor.execute(
                        """SELECT intent_id, event_type, payload
                        FROM astra_oms_events WHERE event_id=%s""",
                        (event_id,),
                    )
                    existing = cursor.fetchone()
                    if existing is not None:
                        _validate_reservation_replay(
                            stored_intent_id=existing["intent_id"],
                            stored_event_type=existing["event_type"],
                            stored_payload=dict(existing["payload"]),
                            intent_id=intent_id,
                            request=request,
                        )
                        return current
                    DurableOmsStore._validate_transition(
                        current.state, OrderState.RISK_APPROVED
                    )
                    if current.side is Side.BUY:
                        cursor.execute(
                            """SELECT * FROM astra_oms_orders
                            WHERE side='BUY' AND state = ANY(%s)""",
                            (states,),
                        )
                        active_buys = tuple(
                            PendingBuyExposure(
                                symbol=record.symbol,
                                remaining_notional=(
                                    record.quantity - record.filled_quantity
                                )
                                * record.limit_price,
                            )
                            for record in (self._row(row) for row in cursor.fetchall())
                        )
                        evaluation = evaluate_buy_reservation(
                            symbol=current.symbol,
                            candidate_notional=current.quantity * current.limit_price,
                            budget=budget,
                            active_buys=active_buys,
                        )
                        if not evaluation.approved:
                            raise RiskReservationRejected(evaluation.reasons)
                        payload = evaluation.event_payload()
                    else:
                        payload = {
                            "reservation": {
                                "approved": True,
                                "kind": "SELL_NO_CAPACITY_CREDIT",
                            }
                        }
                    payload["reservation_request"] = request
                    cursor.execute(
                        """UPDATE astra_oms_orders
                        SET state=%s, version=version+1, updated_at=%s
                        WHERE intent_id=%s""",
                        (OrderState.RISK_APPROVED.value, moment, intent_id),
                    )
                    self._append_event(
                        cursor,
                        event_id=event_id,
                        intent_id=intent_id,
                        event_type=OrderState.RISK_APPROVED.value,
                        payload=payload,
                        occurred_at=moment,
                    )
                    return self._load_for_update(cursor, intent_id)
