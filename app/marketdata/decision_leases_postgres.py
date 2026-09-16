from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from app.marketdata.decision_leases import (
    DecisionLeaseEventKind,
    DecisionLeasePolicy,
    DecisionLeaseReceipt,
    DecisionSafetyEvidence,
    StaleDecisionLease,
    _aware,
    _event_id,
    _required,
)
from app.marketdata.operational import OperationalDecisionTicket

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresDecisionLeaseStore:
    """PostgreSQL decision lease with SKIP LOCKED claim and fencing tokens."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("dsn is required")
        self.dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("psycopg is required for PostgreSQL decision leases")
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def migrate(self) -> None:
        migration = (
            Path(__file__).resolve().parents[2]
            / "migrations"
            / "product"
            / "011_operational_decision_leases.sql"
        ).read_text(encoding="utf-8")
        with self._connect() as connection:
            connection.execute(migration)

    def claim_next(
        self,
        *,
        strategy_id: str,
        owner_id: str,
        release_identity: str,
        occurred_at: datetime,
        policy: DecisionLeasePolicy | None = None,
    ) -> DecisionLeaseReceipt | None:
        normalized_strategy = _required(strategy_id, "strategy_id")
        normalized_owner = _required(owner_id, "owner_id")
        normalized_release = _required(release_identity, "release_identity")
        moment = _aware(occurred_at, "occurred_at")
        resolved = DecisionLeasePolicy() if policy is None else policy
        resolved.validate()
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """SELECT t.ticket_id, t.strategy_id, t.bar_id, t.created_at,
                                  b.provider, b.venue, b.symbol, b.interval_seconds,
                                  b.close_time
                        FROM astra_operational_decision_tickets t
                        JOIN astra_operational_market_bars b ON b.bar_id=t.bar_id
                        LEFT JOIN astra_operational_decision_completions c USING(ticket_id)
                        LEFT JOIN astra_operational_market_bar_conflicts x ON x.bar_id=t.bar_id
                        WHERE t.strategy_id=%s
                          AND t.created_at<=%s
                          AND c.ticket_id IS NULL
                          AND x.bar_id IS NULL
                          AND NOT EXISTS (
                              SELECT 1 FROM astra_operational_decision_leases l
                              WHERE l.ticket_id=t.ticket_id
                                AND l.lease_expires_at>%s
                          )
                        ORDER BY t.created_at, t.ticket_id
                        FOR UPDATE OF t SKIP LOCKED
                        LIMIT 1""",
                        (normalized_strategy, moment, moment),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        return None
                    cursor.execute(
                        """SELECT fencing_token, lease_expires_at
                        FROM astra_operational_decision_leases
                        WHERE ticket_id=%s FOR UPDATE""",
                        (str(row["ticket_id"]),),
                    )
                    lease = cursor.fetchone()
                    expires = moment + resolved.lease_ttl
                    if lease is None:
                        token = 1
                        kind = DecisionLeaseEventKind.CLAIM
                        cursor.execute(
                            """INSERT INTO astra_operational_decision_leases(
                                ticket_id, owner_id, release_identity, fencing_token,
                                acquired_at, lease_expires_at, updated_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                            (
                                str(row["ticket_id"]),
                                normalized_owner,
                                normalized_release,
                                token,
                                moment,
                                expires,
                                moment,
                            ),
                        )
                    else:
                        if self._moment(lease["lease_expires_at"]) > moment:
                            return None
                        token = int(str(lease["fencing_token"])) + 1
                        kind = DecisionLeaseEventKind.RECLAIM
                        cursor.execute(
                            """UPDATE astra_operational_decision_leases
                            SET owner_id=%s, release_identity=%s, fencing_token=%s,
                                acquired_at=%s, lease_expires_at=%s, updated_at=%s
                            WHERE ticket_id=%s""",
                            (
                                normalized_owner,
                                normalized_release,
                                token,
                                moment,
                                expires,
                                moment,
                                str(row["ticket_id"]),
                            ),
                        )
                    receipt = self._receipt_from_claim(
                        row,
                        owner_id=normalized_owner,
                        release_identity=normalized_release,
                        fencing_token=token,
                        acquired_at=moment,
                        expires_at=expires,
                    )
                    self._append_event(
                        cursor,
                        receipt=receipt,
                        kind=kind,
                        occurred_at=moment,
                        lease_expires_at=expires,
                        outcome_id=None,
                    )
                    return receipt

    def renew(
        self,
        receipt: DecisionLeaseReceipt,
        *,
        occurred_at: datetime,
        policy: DecisionLeasePolicy | None = None,
    ) -> DecisionLeaseReceipt:
        receipt.validate()
        moment = _aware(occurred_at, "occurred_at")
        resolved = DecisionLeasePolicy() if policy is None else policy
        resolved.validate()
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    self._assert_current(cursor, receipt, moment=moment, require_unexpired=True)
                    expires = moment + resolved.lease_ttl
                    cursor.execute(
                        """UPDATE astra_operational_decision_leases
                        SET lease_expires_at=%s, updated_at=%s WHERE ticket_id=%s""",
                        (expires, moment, receipt.ticket.ticket_id),
                    )
                    renewed = DecisionLeaseReceipt(**{**receipt.__dict__, "expires_at": expires})
                    self._append_event(
                        cursor,
                        receipt=renewed,
                        kind=DecisionLeaseEventKind.RENEW,
                        occurred_at=moment,
                        lease_expires_at=expires,
                        outcome_id=None,
                    )
                    return renewed

    def release(self, receipt: DecisionLeaseReceipt, *, occurred_at: datetime) -> bool:
        receipt.validate()
        moment = _aware(occurred_at, "occurred_at")
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    row = self._assert_current(
                        cursor,
                        receipt,
                        moment=moment,
                        require_unexpired=False,
                    )
                    if self._moment(row["lease_expires_at"]) <= moment:
                        return False
                    cursor.execute(
                        """UPDATE astra_operational_decision_leases
                        SET lease_expires_at=%s, updated_at=%s WHERE ticket_id=%s""",
                        (moment, moment, receipt.ticket.ticket_id),
                    )
                    self._append_event(
                        cursor,
                        receipt=receipt,
                        kind=DecisionLeaseEventKind.RELEASE,
                        occurred_at=moment,
                        lease_expires_at=moment,
                        outcome_id=None,
                    )
                    return True

    def record_safety(self, evidence: DecisionSafetyEvidence) -> bool:
        evidence.validate()
        moment = _aware(evidence.observed_at, "observed_at")
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    receipt = self._receipt_for_evidence(cursor, evidence)
                    self._assert_current(
                        cursor,
                        receipt,
                        moment=moment,
                        require_unexpired=True,
                    )
                    cursor.execute(
                        """INSERT INTO astra_operational_decision_safety_evidence(
                            evidence_id, ticket_id, owner_id, release_identity,
                            fencing_token, checkpoint_id, first_bar_id, last_bar_id,
                            bar_ids, continuity_reasons, readiness_reasons, control_mode,
                            control_version, observed_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s
                        ) ON CONFLICT (evidence_id) DO NOTHING""",
                        (
                            evidence.evidence_id,
                            evidence.ticket_id,
                            evidence.owner_id,
                            evidence.release_identity,
                            evidence.fencing_token,
                            evidence.checkpoint_id,
                            evidence.first_bar_id,
                            evidence.last_bar_id,
                            json.dumps(list(evidence.bar_ids)),
                            json.dumps(list(evidence.continuity_reasons)),
                            json.dumps(list(evidence.readiness_reasons)),
                            evidence.control_mode,
                            evidence.control_version,
                            moment,
                        ),
                    )
                    return cursor.rowcount == 1

    def complete(
        self,
        receipt: DecisionLeaseReceipt,
        *,
        outcome_id: str,
        occurred_at: datetime,
    ) -> bool:
        receipt.validate()
        normalized_outcome = _required(outcome_id, "outcome_id")
        moment = _aware(occurred_at, "occurred_at")
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    existing = self._completion(cursor, receipt.ticket.ticket_id)
                    if existing is not None:
                        if str(existing["outcome_id"]) != normalized_outcome:
                            raise ValueError("OPERATIONAL_DECISION_COMPLETION_CONFLICT")
                        return False
                    self._assert_current(
                        cursor,
                        receipt,
                        moment=moment,
                        require_unexpired=True,
                    )
                    self._require_ready_safety(
                        cursor,
                        receipt=receipt,
                        completed_at=moment,
                    )
                    cursor.execute(
                        """INSERT INTO astra_operational_decision_completions(
                            ticket_id, outcome_id, completed_at
                        ) VALUES (%s, %s, %s)
                        ON CONFLICT (ticket_id) DO NOTHING""",
                        (receipt.ticket.ticket_id, normalized_outcome, moment),
                    )
                    if cursor.rowcount == 0:
                        existing = self._completion(cursor, receipt.ticket.ticket_id)
                        if existing is None:
                            raise RuntimeError("decision completion idempotency lookup failed")
                        if str(existing["outcome_id"]) != normalized_outcome:
                            raise ValueError("OPERATIONAL_DECISION_COMPLETION_CONFLICT")
                        return False
                    cursor.execute(
                        """UPDATE astra_operational_decision_leases
                        SET lease_expires_at=%s, updated_at=%s WHERE ticket_id=%s""",
                        (moment, moment, receipt.ticket.ticket_id),
                    )
                    self._append_event(
                        cursor,
                        receipt=receipt,
                        kind=DecisionLeaseEventKind.COMPLETE,
                        occurred_at=moment,
                        lease_expires_at=moment,
                        outcome_id=normalized_outcome,
                    )
                    return True

    @staticmethod
    def _completion(cursor, ticket_id: str):
        cursor.execute(
            """SELECT outcome_id FROM astra_operational_decision_completions
            WHERE ticket_id=%s""",
            (ticket_id,),
        )
        return cursor.fetchone()

    @staticmethod
    def _require_ready_safety(
        cursor,
        *,
        receipt: DecisionLeaseReceipt,
        completed_at: datetime,
    ) -> None:
        cursor.execute(
            """SELECT checkpoint_id, first_bar_id, last_bar_id, bar_ids,
                      continuity_reasons, readiness_reasons, observed_at
            FROM astra_operational_decision_safety_evidence
            WHERE ticket_id=%s AND owner_id=%s AND release_identity=%s
              AND fencing_token=%s
            ORDER BY observed_at DESC, evidence_id DESC LIMIT 1""",
            (
                receipt.ticket.ticket_id,
                receipt.owner_id,
                receipt.release_identity,
                receipt.fencing_token,
            ),
        )
        row = cursor.fetchone()
        if row is None:
            raise ValueError("DECISION_READY_SAFETY_EVIDENCE_REQUIRED")
        observed_at = PostgresDecisionLeaseStore._moment(row["observed_at"])
        if observed_at < _aware(receipt.acquired_at, "acquired_at") or observed_at > completed_at:
            raise ValueError("DECISION_SAFETY_EVIDENCE_TIME_INVALID")
        continuity_reasons = _json_string_array(row["continuity_reasons"], "continuity_reasons")
        readiness_reasons = _json_string_array(row["readiness_reasons"], "readiness_reasons")
        if continuity_reasons or readiness_reasons:
            raise ValueError("DECISION_READY_SAFETY_EVIDENCE_REQUIRED")
        bar_ids = _json_string_array(row["bar_ids"], "bar_ids")
        if (
            row["checkpoint_id"] is None
            or not bar_ids
            or str(row["first_bar_id"]) != bar_ids[0]
            or str(row["last_bar_id"]) != bar_ids[-1]
            or bar_ids[-1] != receipt.ticket.bar_id
        ):
            raise ValueError("DECISION_READY_SAFETY_EVIDENCE_REQUIRED")

        placeholders = ",".join("%s" for _ in bar_ids)
        cursor.execute(
            f"""SELECT bar_id, provider, venue, symbol, interval_seconds,
                       open_time, close_time
                FROM astra_operational_market_bars
                WHERE bar_id IN ({placeholders})
                ORDER BY close_time, bar_id
                FOR UPDATE""",
            bar_ids,
        )
        bars = cursor.fetchall()
        if tuple(str(value["bar_id"]) for value in bars) != bar_ids:
            raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")

        previous_close: datetime | None = None
        for value in bars:
            if (
                str(value["provider"]) != receipt.provider
                or str(value["venue"]) != receipt.venue
                or str(value["symbol"]) != receipt.symbol
                or int(str(value["interval_seconds"])) != receipt.interval_seconds
            ):
                raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")
            open_time = PostgresDecisionLeaseStore._moment(value["open_time"])
            close_time = PostgresDecisionLeaseStore._moment(value["close_time"])
            if previous_close is not None and previous_close != open_time:
                raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")
            previous_close = close_time
        if previous_close != _aware(receipt.bar_close_time, "bar_close_time"):
            raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")

        cursor.execute(
            f"""SELECT 1
                FROM astra_operational_market_bar_conflicts
                WHERE bar_id IN ({placeholders})
                LIMIT 1""",
            bar_ids,
        )
        if cursor.fetchone() is not None:
            raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")

        cursor.execute(
            """SELECT c.provider, c.venue, c.symbol, c.interval_seconds,
                      c.through_bar_id, c.through_close_time,
                      b.close_time AS durable_close_time
            FROM astra_operational_market_continuity c
            JOIN astra_operational_market_bars b ON b.bar_id=c.through_bar_id
            WHERE c.checkpoint_id=%s
            FOR UPDATE OF b""",
            (str(row["checkpoint_id"]),),
        )
        checkpoint = cursor.fetchone()
        if checkpoint is None:
            raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")
        through_close = PostgresDecisionLeaseStore._moment(checkpoint["through_close_time"])
        durable_close = PostgresDecisionLeaseStore._moment(checkpoint["durable_close_time"])
        if (
            str(checkpoint["provider"]) != receipt.provider
            or str(checkpoint["venue"]) != receipt.venue
            or str(checkpoint["symbol"]) != receipt.symbol
            or int(str(checkpoint["interval_seconds"])) != receipt.interval_seconds
            or through_close != durable_close
            or through_close < _aware(receipt.bar_close_time, "bar_close_time")
        ):
            raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")
        cursor.execute(
            """SELECT 1 FROM astra_operational_market_bar_conflicts
            WHERE bar_id=%s LIMIT 1""",
            (str(checkpoint["through_bar_id"]),),
        )
        if cursor.fetchone() is not None:
            raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")

    def _receipt_for_evidence(
        self,
        cursor,
        evidence: DecisionSafetyEvidence,
    ) -> DecisionLeaseReceipt:
        cursor.execute(
            """SELECT t.ticket_id, t.strategy_id, t.bar_id, t.created_at,
                      b.provider, b.venue, b.symbol, b.interval_seconds, b.close_time,
                      l.owner_id, l.release_identity, l.fencing_token,
                      l.acquired_at, l.lease_expires_at
            FROM astra_operational_decision_tickets t
            JOIN astra_operational_market_bars b ON b.bar_id=t.bar_id
            JOIN astra_operational_decision_leases l USING(ticket_id)
            WHERE t.ticket_id=%s""",
            (evidence.ticket_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StaleDecisionLease("decision lease is missing")
        receipt = self._receipt(row)
        if (
            receipt.owner_id != evidence.owner_id
            or receipt.release_identity != evidence.release_identity
            or receipt.fencing_token != evidence.fencing_token
        ):
            raise StaleDecisionLease("decision safety evidence uses stale fence")
        return receipt

    @staticmethod
    def _assert_current(
        cursor,
        receipt: DecisionLeaseReceipt,
        *,
        moment: datetime,
        require_unexpired: bool,
    ) -> Mapping[str, object]:
        cursor.execute(
            """SELECT owner_id, release_identity, fencing_token,
                      acquired_at, lease_expires_at
            FROM astra_operational_decision_leases
            WHERE ticket_id=%s FOR UPDATE""",
            (receipt.ticket.ticket_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise StaleDecisionLease("decision lease is missing")
        if (
            str(row["owner_id"]) != receipt.owner_id
            or str(row["release_identity"]) != receipt.release_identity
            or int(str(row["fencing_token"])) != receipt.fencing_token
        ):
            raise StaleDecisionLease("decision lease owner/release/fence is stale")
        acquired = PostgresDecisionLeaseStore._moment(row["acquired_at"])
        expires = PostgresDecisionLeaseStore._moment(row["lease_expires_at"])
        if moment < acquired:
            raise StaleDecisionLease("decision operation predates lease acquisition")
        if require_unexpired and moment >= expires:
            raise StaleDecisionLease("decision lease expired")
        return row

    @staticmethod
    def _ticket(row: Mapping[str, object]) -> OperationalDecisionTicket:
        ticket = OperationalDecisionTicket(
            ticket_id=str(row["ticket_id"]),
            strategy_id=str(row["strategy_id"]),
            bar_id=str(row["bar_id"]),
            created_at=PostgresDecisionLeaseStore._moment(row["created_at"]),
        )
        ticket.validate()
        return ticket

    @classmethod
    def _receipt_from_claim(
        cls,
        row: Mapping[str, object],
        *,
        owner_id: str,
        release_identity: str,
        fencing_token: int,
        acquired_at: datetime,
        expires_at: datetime,
    ) -> DecisionLeaseReceipt:
        receipt = DecisionLeaseReceipt(
            ticket=cls._ticket(row),
            provider=str(row["provider"]),
            venue=str(row["venue"]),
            symbol=str(row["symbol"]),
            interval_seconds=int(str(row["interval_seconds"])),
            bar_close_time=cls._moment(row["close_time"]),
            owner_id=owner_id,
            release_identity=release_identity,
            fencing_token=fencing_token,
            acquired_at=acquired_at,
            expires_at=expires_at,
        )
        receipt.validate()
        return receipt

    @classmethod
    def _receipt(cls, row: Mapping[str, object]) -> DecisionLeaseReceipt:
        return cls._receipt_from_claim(
            row,
            owner_id=str(row["owner_id"]),
            release_identity=str(row["release_identity"]),
            fencing_token=int(str(row["fencing_token"])),
            acquired_at=cls._moment(row["acquired_at"]),
            expires_at=cls._moment(row["lease_expires_at"]),
        )

    @staticmethod
    def _append_event(
        cursor,
        *,
        receipt: DecisionLeaseReceipt,
        kind: DecisionLeaseEventKind,
        occurred_at: datetime,
        lease_expires_at: datetime | None,
        outcome_id: str | None,
    ) -> None:
        event_id = _event_id(
            receipt=receipt,
            kind=kind,
            occurred_at=occurred_at,
            lease_expires_at=lease_expires_at,
            outcome_id=outcome_id,
        )
        cursor.execute(
            """INSERT INTO astra_operational_decision_lease_events(
                event_id, ticket_id, event_type, owner_id, release_identity,
                fencing_token, lease_expires_at, outcome_id, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (event_id) DO NOTHING""",
            (
                event_id,
                receipt.ticket.ticket_id,
                kind.value,
                receipt.owner_id,
                receipt.release_identity,
                receipt.fencing_token,
                None
                if lease_expires_at is None
                else _aware(lease_expires_at, "lease_expires_at"),
                outcome_id,
                _aware(occurred_at, "occurred_at"),
            ),
        )

    @staticmethod
    def _moment(value: object) -> datetime:
        if isinstance(value, datetime):
            return _aware(value, "timestamp")
        return _aware(datetime.fromisoformat(str(value)), "timestamp")


def _json_string_array(value: object, name: str) -> tuple[str, ...]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ValueError(f"{name} is not a JSON string array")
    result = tuple(decoded)
    if any(not item.strip() for item in result):
        raise ValueError(f"{name} cannot contain blanks")
    return result
