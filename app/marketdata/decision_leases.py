from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.marketdata.operational import OperationalDecisionTicket, _aware

_MAX_LEASE_TTL = timedelta(minutes=5)


class StaleDecisionLease(RuntimeError):
    pass


class DecisionLeaseEventKind(StrEnum):
    CLAIM = "CLAIM"
    RECLAIM = "RECLAIM"
    RENEW = "RENEW"
    RELEASE = "RELEASE"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True)
class DecisionLeasePolicy:
    lease_ttl: timedelta = timedelta(seconds=30)

    def validate(self) -> None:
        if self.lease_ttl <= timedelta(0) or self.lease_ttl > _MAX_LEASE_TTL:
            raise ValueError("decision lease_ttl must be within (0, 300] seconds")


@dataclass(frozen=True)
class DecisionLeaseReceipt:
    ticket: OperationalDecisionTicket
    provider: str
    venue: str
    symbol: str
    interval_seconds: int
    bar_close_time: datetime
    owner_id: str
    release_identity: str
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime

    def validate(self) -> None:
        self.ticket.validate()
        for name, value in (
            ("provider", self.provider),
            ("venue", self.venue),
            ("symbol", self.symbol),
        ):
            if not value or value != value.strip().upper():
                raise ValueError(f"{name} must be normalized uppercase")
        if self.interval_seconds < 1:
            raise ValueError("interval_seconds must be positive")
        _aware(self.bar_close_time, "bar_close_time")
        acquired = _aware(self.acquired_at, "acquired_at")
        expires = _aware(self.expires_at, "expires_at")
        if not self.owner_id.strip() or not self.release_identity.strip():
            raise ValueError("lease owner_id and release_identity are required")
        if self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        if expires <= acquired:
            raise ValueError("lease expires_at must follow acquired_at")


@dataclass(frozen=True)
class DecisionSafetyEvidence:
    evidence_id: str
    ticket_id: str
    owner_id: str
    release_identity: str
    fencing_token: int
    checkpoint_id: str | None
    first_bar_id: str | None
    last_bar_id: str | None
    bar_ids: tuple[str, ...]
    continuity_reasons: tuple[str, ...]
    readiness_reasons: tuple[str, ...]
    control_mode: str
    control_version: int
    observed_at: datetime

    @property
    def ready_for_evaluation(self) -> bool:
        return not self.continuity_reasons and not self.readiness_reasons

    def validate(self) -> None:
        if not self.evidence_id.strip() or not self.ticket_id.strip():
            raise ValueError("decision safety evidence identity is required")
        if not self.owner_id.strip() or not self.release_identity.strip():
            raise ValueError("decision safety owner/release identity is required")
        if self.fencing_token < 1:
            raise ValueError("fencing_token must be positive")
        if (self.first_bar_id is None) != (self.last_bar_id is None):
            raise ValueError("decision safety bar window must be complete or absent")
        if any(not bar_id.strip() for bar_id in self.bar_ids):
            raise ValueError("decision safety bar_ids cannot contain blanks")
        if len(self.bar_ids) != len(set(self.bar_ids)):
            raise ValueError("decision safety bar_ids must be unique")
        if self.bar_ids:
            if self.first_bar_id != self.bar_ids[0] or self.last_bar_id != self.bar_ids[-1]:
                raise ValueError("decision safety endpoints disagree with ordered bar_ids")
        elif self.first_bar_id is not None or self.last_bar_id is not None:
            raise ValueError("decision safety endpoints require ordered bar_ids")
        if self.checkpoint_id is not None and not self.checkpoint_id.strip():
            raise ValueError("checkpoint_id cannot be blank")
        for reasons in (self.continuity_reasons, self.readiness_reasons):
            if reasons != tuple(sorted(set(reasons))):
                raise ValueError("decision safety reasons must be sorted and unique")
            if any(not reason.strip() for reason in reasons):
                raise ValueError("decision safety reasons cannot be blank")
        if self.ready_for_evaluation and (self.checkpoint_id is None or not self.bar_ids):
            raise ValueError("ready decision safety evidence requires checkpoint and bar window")
        if self.control_mode not in {"HALTED", "ARMED"}:
            raise ValueError("control_mode must be HALTED or ARMED")
        if self.control_version < 0:
            raise ValueError("control_version must be non-negative")
        _aware(self.observed_at, "observed_at")
        expected = decision_safety_evidence_id(
            ticket_id=self.ticket_id,
            owner_id=self.owner_id,
            release_identity=self.release_identity,
            fencing_token=self.fencing_token,
            checkpoint_id=self.checkpoint_id,
            first_bar_id=self.first_bar_id,
            last_bar_id=self.last_bar_id,
            bar_ids=self.bar_ids,
            continuity_reasons=self.continuity_reasons,
            readiness_reasons=self.readiness_reasons,
            control_mode=self.control_mode,
            control_version=self.control_version,
            observed_at=self.observed_at,
        )
        if self.evidence_id != expected:
            raise ValueError("decision safety evidence_id disagrees with payload")


def decision_safety_evidence_id(
    *,
    ticket_id: str,
    owner_id: str,
    release_identity: str,
    fencing_token: int,
    checkpoint_id: str | None,
    first_bar_id: str | None,
    last_bar_id: str | None,
    bar_ids: tuple[str, ...],
    continuity_reasons: tuple[str, ...],
    readiness_reasons: tuple[str, ...],
    control_mode: str,
    control_version: int,
    observed_at: datetime,
) -> str:
    if not ticket_id.strip() or not owner_id.strip() or not release_identity.strip():
        raise ValueError("decision safety identity is incomplete")
    if fencing_token < 1 or control_version < 0:
        raise ValueError("decision safety numeric identity is invalid")
    material = json.dumps(
        {
            "ticket_id": ticket_id,
            "owner_id": owner_id,
            "release_identity": release_identity,
            "fencing_token": fencing_token,
            "checkpoint_id": checkpoint_id,
            "first_bar_id": first_bar_id,
            "last_bar_id": last_bar_id,
            "bar_ids": list(bar_ids),
            "continuity_reasons": list(continuity_reasons),
            "readiness_reasons": list(readiness_reasons),
            "control_mode": control_mode,
            "control_version": control_version,
            "observed_at": _aware(observed_at, "observed_at").isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


class DecisionLeaseStore(Protocol):
    def claim_next(
        self,
        *,
        strategy_id: str,
        owner_id: str,
        release_identity: str,
        occurred_at: datetime,
        policy: DecisionLeasePolicy | None = None,
    ) -> DecisionLeaseReceipt | None: ...

    def renew(
        self,
        receipt: DecisionLeaseReceipt,
        *,
        occurred_at: datetime,
        policy: DecisionLeasePolicy | None = None,
    ) -> DecisionLeaseReceipt: ...

    def release(self, receipt: DecisionLeaseReceipt, *, occurred_at: datetime) -> bool: ...

    def record_safety(self, evidence: DecisionSafetyEvidence) -> bool: ...

    def complete(
        self,
        receipt: DecisionLeaseReceipt,
        *,
        outcome_id: str,
        occurred_at: datetime,
    ) -> bool: ...


class SQLiteDecisionLeaseStore:
    """Crash-reclaimable decision lease with fencing and append-only safety evidence."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS operational_decision_leases (
                    ticket_id TEXT PRIMARY KEY
                        REFERENCES operational_decision_tickets(ticket_id),
                    owner_id TEXT NOT NULL,
                    release_identity TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
                    acquired_at TEXT NOT NULL,
                    lease_expires_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_operational_decision_leases_expiry
                ON operational_decision_leases(lease_expires_at, ticket_id);
                CREATE TABLE IF NOT EXISTS operational_decision_lease_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    ticket_id TEXT NOT NULL
                        REFERENCES operational_decision_tickets(ticket_id),
                    event_type TEXT NOT NULL CHECK (
                        event_type IN ('CLAIM', 'RECLAIM', 'RENEW', 'RELEASE', 'COMPLETE')
                    ),
                    owner_id TEXT NOT NULL,
                    release_identity TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
                    lease_expires_at TEXT,
                    outcome_id TEXT,
                    occurred_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_operational_decision_lease_events_ticket
                ON operational_decision_lease_events(ticket_id, sequence);
                CREATE TABLE IF NOT EXISTS operational_decision_safety_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL
                        REFERENCES operational_decision_tickets(ticket_id),
                    owner_id TEXT NOT NULL,
                    release_identity TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL CHECK (fencing_token > 0),
                    checkpoint_id TEXT
                        REFERENCES operational_market_continuity(checkpoint_id),
                    first_bar_id TEXT
                        REFERENCES operational_market_bars(bar_id),
                    last_bar_id TEXT
                        REFERENCES operational_market_bars(bar_id),
                    bar_ids TEXT NOT NULL,
                    continuity_reasons TEXT NOT NULL,
                    readiness_reasons TEXT NOT NULL,
                    control_mode TEXT NOT NULL CHECK (control_mode IN ('HALTED', 'ARMED')),
                    control_version INTEGER NOT NULL CHECK (control_version >= 0),
                    observed_at TEXT NOT NULL,
                    CHECK (
                        (first_bar_id IS NULL AND last_bar_id IS NULL)
                        OR (first_bar_id IS NOT NULL AND last_bar_id IS NOT NULL)
                    )
                );
                CREATE INDEX IF NOT EXISTS idx_operational_decision_safety_ticket
                ON operational_decision_safety_evidence(ticket_id, observed_at, evidence_id);
                CREATE TRIGGER IF NOT EXISTS operational_decision_lease_events_no_update
                BEFORE UPDATE ON operational_decision_lease_events BEGIN
                    SELECT RAISE(ABORT, 'operational_decision_lease_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS operational_decision_lease_events_no_delete
                BEFORE DELETE ON operational_decision_lease_events BEGIN
                    SELECT RAISE(ABORT, 'operational_decision_lease_events is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS operational_decision_safety_no_update
                BEFORE UPDATE ON operational_decision_safety_evidence BEGIN
                    SELECT RAISE(ABORT, 'operational_decision_safety_evidence is append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS operational_decision_safety_no_delete
                BEFORE DELETE ON operational_decision_safety_evidence BEGIN
                    SELECT RAISE(ABORT, 'operational_decision_safety_evidence is append-only');
                END;
                """
            )
        finally:
            connection.close()

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
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT t.ticket_id, t.strategy_id, t.bar_id, t.created_at,
                          b.provider, b.venue, b.symbol, b.interval_seconds, b.close_time,
                          l.fencing_token AS lease_fencing_token
                FROM operational_decision_tickets t
                JOIN operational_market_bars b ON b.bar_id=t.bar_id
                LEFT JOIN operational_decision_completions c USING(ticket_id)
                LEFT JOIN operational_market_bar_conflicts x ON x.bar_id=t.bar_id
                LEFT JOIN operational_decision_leases l USING(ticket_id)
                WHERE t.strategy_id=? AND t.created_at<=?
                  AND c.ticket_id IS NULL AND x.bar_id IS NULL
                  AND (l.ticket_id IS NULL OR l.lease_expires_at<=?)
                ORDER BY t.created_at, t.ticket_id LIMIT 1""",
                (normalized_strategy, moment.isoformat(), moment.isoformat()),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                return None
            expires = moment + resolved.lease_ttl
            prior_token = row["lease_fencing_token"]
            if prior_token is None:
                token = 1
                kind = DecisionLeaseEventKind.CLAIM
                connection.execute(
                    """INSERT INTO operational_decision_leases(
                        ticket_id, owner_id, release_identity, fencing_token,
                        acquired_at, lease_expires_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(row["ticket_id"]), normalized_owner, normalized_release, token,
                        moment.isoformat(), expires.isoformat(), moment.isoformat(),
                    ),
                )
            else:
                token = int(prior_token) + 1
                kind = DecisionLeaseEventKind.RECLAIM
                connection.execute(
                    """UPDATE operational_decision_leases
                    SET owner_id=?, release_identity=?, fencing_token=?, acquired_at=?,
                        lease_expires_at=?, updated_at=? WHERE ticket_id=?""",
                    (
                        normalized_owner, normalized_release, token, moment.isoformat(),
                        expires.isoformat(), moment.isoformat(), str(row["ticket_id"]),
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
                connection,
                receipt=receipt,
                kind=kind,
                occurred_at=moment,
                lease_expires_at=expires,
                outcome_id=None,
            )
            connection.execute("COMMIT")
            return receipt
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

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
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_current(connection, receipt, moment=moment, require_unexpired=True)
            expires = moment + resolved.lease_ttl
            connection.execute(
                """UPDATE operational_decision_leases
                SET lease_expires_at=?, updated_at=? WHERE ticket_id=?""",
                (expires.isoformat(), moment.isoformat(), receipt.ticket.ticket_id),
            )
            renewed = DecisionLeaseReceipt(**{**receipt.__dict__, "expires_at": expires})
            self._append_event(
                connection,
                receipt=renewed,
                kind=DecisionLeaseEventKind.RENEW,
                occurred_at=moment,
                lease_expires_at=expires,
                outcome_id=None,
            )
            connection.execute("COMMIT")
            return renewed
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def release(self, receipt: DecisionLeaseReceipt, *, occurred_at: datetime) -> bool:
        receipt.validate()
        moment = _aware(occurred_at, "occurred_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = self._assert_current(
                connection, receipt, moment=moment, require_unexpired=False
            )
            if _parse_moment(row["lease_expires_at"], "lease_expires_at") <= moment:
                connection.execute("COMMIT")
                return False
            connection.execute(
                """UPDATE operational_decision_leases
                SET lease_expires_at=?, updated_at=? WHERE ticket_id=?""",
                (moment.isoformat(), moment.isoformat(), receipt.ticket.ticket_id),
            )
            self._append_event(
                connection,
                receipt=receipt,
                kind=DecisionLeaseEventKind.RELEASE,
                occurred_at=moment,
                lease_expires_at=moment,
                outcome_id=None,
            )
            connection.execute("COMMIT")
            return True
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    def record_safety(self, evidence: DecisionSafetyEvidence) -> bool:
        evidence.validate()
        moment = _aware(evidence.observed_at, "observed_at")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            receipt = self._receipt_for_evidence(connection, evidence)
            self._assert_current(connection, receipt, moment=moment, require_unexpired=True)
            cursor = connection.execute(
                """INSERT OR IGNORE INTO operational_decision_safety_evidence(
                    evidence_id, ticket_id, owner_id, release_identity, fencing_token,
                    checkpoint_id, first_bar_id, last_bar_id, bar_ids, continuity_reasons,
                    readiness_reasons, control_mode, control_version, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    moment.isoformat(),
                ),
            )
            connection.execute("COMMIT")
            return cursor.rowcount == 1
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

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
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._completion(connection, receipt.ticket.ticket_id)
            if existing is not None:
                if str(existing["outcome_id"]) != normalized_outcome:
                    raise ValueError("OPERATIONAL_DECISION_COMPLETION_CONFLICT")
                connection.execute("COMMIT")
                return False
            self._assert_current(connection, receipt, moment=moment, require_unexpired=True)
            self._require_ready_safety(connection, receipt=receipt, completed_at=moment)
            cursor = connection.execute(
                """INSERT OR IGNORE INTO operational_decision_completions(
                    ticket_id, outcome_id, completed_at
                ) VALUES (?, ?, ?)""",
                (receipt.ticket.ticket_id, normalized_outcome, moment.isoformat()),
            )
            if cursor.rowcount == 0:
                existing = self._completion(connection, receipt.ticket.ticket_id)
                if existing is None:
                    raise RuntimeError("decision completion idempotency lookup failed")
                if str(existing["outcome_id"]) != normalized_outcome:
                    raise ValueError("OPERATIONAL_DECISION_COMPLETION_CONFLICT")
                connection.execute("COMMIT")
                return False
            connection.execute(
                """UPDATE operational_decision_leases
                SET lease_expires_at=?, updated_at=? WHERE ticket_id=?""",
                (moment.isoformat(), moment.isoformat(), receipt.ticket.ticket_id),
            )
            self._append_event(
                connection,
                receipt=receipt,
                kind=DecisionLeaseEventKind.COMPLETE,
                occurred_at=moment,
                lease_expires_at=moment,
                outcome_id=normalized_outcome,
            )
            connection.execute("COMMIT")
            return True
        except Exception:
            _rollback(connection)
            raise
        finally:
            connection.close()

    @staticmethod
    def _completion(connection: sqlite3.Connection, ticket_id: str) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT outcome_id FROM operational_decision_completions WHERE ticket_id=?",
            (ticket_id,),
        ).fetchone()

    @staticmethod
    def _require_ready_safety(
        connection: sqlite3.Connection,
        *,
        receipt: DecisionLeaseReceipt,
        completed_at: datetime,
    ) -> None:
        row = connection.execute(
            """SELECT checkpoint_id, first_bar_id, last_bar_id, bar_ids,
                      continuity_reasons, readiness_reasons, observed_at
            FROM operational_decision_safety_evidence
            WHERE ticket_id=? AND owner_id=? AND release_identity=? AND fencing_token=?
            ORDER BY observed_at DESC, evidence_id DESC LIMIT 1""",
            (
                receipt.ticket.ticket_id,
                receipt.owner_id,
                receipt.release_identity,
                receipt.fencing_token,
            ),
        ).fetchone()
        if row is None:
            raise ValueError("DECISION_READY_SAFETY_EVIDENCE_REQUIRED")
        observed_at = _parse_moment(row["observed_at"], "observed_at")
        if observed_at < _aware(receipt.acquired_at, "acquired_at") or observed_at > completed_at:
            raise ValueError("DECISION_SAFETY_EVIDENCE_TIME_INVALID")
        if tuple(json.loads(str(row["continuity_reasons"]))) or tuple(
            json.loads(str(row["readiness_reasons"]))
        ):
            raise ValueError("DECISION_READY_SAFETY_EVIDENCE_REQUIRED")
        bar_ids = _json_string_tuple(row["bar_ids"], "bar_ids")
        if (
            row["checkpoint_id"] is None
            or not bar_ids
            or str(row["first_bar_id"]) != bar_ids[0]
            or str(row["last_bar_id"]) != bar_ids[-1]
            or bar_ids[-1] != receipt.ticket.bar_id
        ):
            raise ValueError("DECISION_READY_SAFETY_EVIDENCE_REQUIRED")

        bars: list[sqlite3.Row] = []
        for bar_id in bar_ids:
            value = connection.execute(
                """SELECT b.bar_id, b.provider, b.venue, b.symbol, b.interval_seconds,
                          b.open_time, b.close_time,
                          EXISTS(
                              SELECT 1 FROM operational_market_bar_conflicts x
                              WHERE x.bar_id=b.bar_id
                          ) AS conflicted
                FROM operational_market_bars b
                WHERE b.bar_id=?""",
                (bar_id,),
            ).fetchone()
            if value is None:
                raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")
            bars.append(value)
        if tuple(str(value["bar_id"]) for value in bars) != bar_ids:
            raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")
        previous_close: datetime | None = None
        for value in bars:
            if (
                str(value["provider"]) != receipt.provider
                or str(value["venue"]) != receipt.venue
                or str(value["symbol"]) != receipt.symbol
                or int(value["interval_seconds"]) != receipt.interval_seconds
                or bool(value["conflicted"])
            ):
                raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")
            open_time = _parse_moment(value["open_time"], "open_time")
            close_time = _parse_moment(value["close_time"], "close_time")
            if previous_close is not None and previous_close != open_time:
                raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")
            previous_close = close_time
        if previous_close != _aware(receipt.bar_close_time, "bar_close_time"):
            raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")

        checkpoint = connection.execute(
            """SELECT c.provider, c.venue, c.symbol, c.interval_seconds,
                      c.through_bar_id, c.through_close_time, b.close_time AS durable_close_time,
                      EXISTS(
                          SELECT 1 FROM operational_market_bar_conflicts x
                          WHERE x.bar_id=c.through_bar_id
                      ) AS conflicted
            FROM operational_market_continuity c
            JOIN operational_market_bars b ON b.bar_id=c.through_bar_id
            WHERE c.checkpoint_id=?""",
            (str(row["checkpoint_id"]),),
        ).fetchone()
        if checkpoint is None:
            raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")
        through_close = _parse_moment(checkpoint["through_close_time"], "through_close_time")
        durable_close = _parse_moment(checkpoint["durable_close_time"], "durable_close_time")
        if (
            str(checkpoint["provider"]) != receipt.provider
            or str(checkpoint["venue"]) != receipt.venue
            or str(checkpoint["symbol"]) != receipt.symbol
            or int(checkpoint["interval_seconds"]) != receipt.interval_seconds
            or through_close != durable_close
            or through_close < _aware(receipt.bar_close_time, "bar_close_time")
            or bool(checkpoint["conflicted"])
        ):
            raise ValueError("DECISION_SAFETY_EVIDENCE_INVALIDATED")

    def _receipt_for_evidence(
        self,
        connection: sqlite3.Connection,
        evidence: DecisionSafetyEvidence,
    ) -> DecisionLeaseReceipt:
        row = connection.execute(
            """SELECT t.ticket_id, t.strategy_id, t.bar_id, t.created_at,
                      b.provider, b.venue, b.symbol, b.interval_seconds, b.close_time,
                      l.owner_id, l.release_identity, l.fencing_token,
                      l.acquired_at, l.lease_expires_at
            FROM operational_decision_tickets t
            JOIN operational_market_bars b ON b.bar_id=t.bar_id
            JOIN operational_decision_leases l USING(ticket_id)
            WHERE t.ticket_id=?""",
            (evidence.ticket_id,),
        ).fetchone()
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
        connection: sqlite3.Connection,
        receipt: DecisionLeaseReceipt,
        *,
        moment: datetime,
        require_unexpired: bool,
    ) -> sqlite3.Row:
        row = connection.execute(
            """SELECT owner_id, release_identity, fencing_token,
                      acquired_at, lease_expires_at
            FROM operational_decision_leases WHERE ticket_id=?""",
            (receipt.ticket.ticket_id,),
        ).fetchone()
        if row is None:
            raise StaleDecisionLease("decision lease is missing")
        if (
            str(row["owner_id"]) != receipt.owner_id
            or str(row["release_identity"]) != receipt.release_identity
            or int(row["fencing_token"]) != receipt.fencing_token
        ):
            raise StaleDecisionLease("decision lease owner/release/fence is stale")
        acquired = _parse_moment(row["acquired_at"], "acquired_at")
        expires = _parse_moment(row["lease_expires_at"], "lease_expires_at")
        if moment < acquired:
            raise StaleDecisionLease("decision operation predates lease acquisition")
        if require_unexpired and moment >= expires:
            raise StaleDecisionLease("decision lease expired")
        return row

    @staticmethod
    def _ticket(row: sqlite3.Row) -> OperationalDecisionTicket:
        ticket = OperationalDecisionTicket(
            ticket_id=str(row["ticket_id"]),
            strategy_id=str(row["strategy_id"]),
            bar_id=str(row["bar_id"]),
            created_at=_parse_moment(row["created_at"], "created_at"),
        )
        ticket.validate()
        return ticket

    @classmethod
    def _receipt_from_claim(
        cls,
        row: sqlite3.Row,
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
            interval_seconds=int(row["interval_seconds"]),
            bar_close_time=_parse_moment(row["close_time"], "close_time"),
            owner_id=owner_id,
            release_identity=release_identity,
            fencing_token=fencing_token,
            acquired_at=acquired_at,
            expires_at=expires_at,
        )
        receipt.validate()
        return receipt

    @classmethod
    def _receipt(cls, row: sqlite3.Row) -> DecisionLeaseReceipt:
        return cls._receipt_from_claim(
            row,
            owner_id=str(row["owner_id"]),
            release_identity=str(row["release_identity"]),
            fencing_token=int(row["fencing_token"]),
            acquired_at=_parse_moment(row["acquired_at"], "acquired_at"),
            expires_at=_parse_moment(row["lease_expires_at"], "lease_expires_at"),
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
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
        connection.execute(
            """INSERT OR IGNORE INTO operational_decision_lease_events(
                event_id, ticket_id, event_type, owner_id, release_identity,
                fencing_token, lease_expires_at, outcome_id, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event_id,
                receipt.ticket.ticket_id,
                kind.value,
                receipt.owner_id,
                receipt.release_identity,
                receipt.fencing_token,
                None
                if lease_expires_at is None
                else _aware(lease_expires_at, "lease_expires_at").isoformat(),
                outcome_id,
                _aware(occurred_at, "occurred_at").isoformat(),
            ),
        )


def _event_id(
    *,
    receipt: DecisionLeaseReceipt,
    kind: DecisionLeaseEventKind,
    occurred_at: datetime,
    lease_expires_at: datetime | None,
    outcome_id: str | None,
) -> str:
    material = json.dumps(
        {
            "ticket_id": receipt.ticket.ticket_id,
            "event_type": kind.value,
            "owner_id": receipt.owner_id,
            "release_identity": receipt.release_identity,
            "fencing_token": receipt.fencing_token,
            "lease_expires_at": None
            if lease_expires_at is None
            else _aware(lease_expires_at, "lease_expires_at").isoformat(),
            "outcome_id": outcome_id,
            "occurred_at": _aware(occurred_at, "occurred_at").isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _json_string_tuple(value: object, name: str) -> tuple[str, ...]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ValueError(f"{name} must be a JSON string array")
    result = tuple(decoded)
    if any(not item.strip() for item in result) or len(result) != len(set(result)):
        raise ValueError(f"{name} must contain unique non-empty strings")
    return result


def _required(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def _parse_moment(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        return _aware(value, name)
    return _aware(datetime.fromisoformat(str(value)), name)


def _rollback(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass
