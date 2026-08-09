from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Protocol, Sequence

from app.runtime.campaign_control_plane_v103 import (
    LeaseReceiptV103,
    LeaseUnavailable,
    StaleFencingToken,
    StaleGeneration,
)

UTC = timezone.utc


class CursorLike(Protocol):
    def execute(self, query: str, params: Sequence[Any] | None = None) -> Any: ...
    def fetchone(self) -> Sequence[Any] | None: ...
    def fetchall(self) -> Sequence[Sequence[Any]]: ...
    def __enter__(self) -> "CursorLike": ...
    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...


class ConnectionLike(Protocol):
    def cursor(self) -> CursorLike: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def close(self) -> None: ...


ConnectionFactory = Callable[[], ConnectionLike]


@dataclass(frozen=True, slots=True)
class PostgresLeaseClaimV103:
    campaign_id: str
    owner_id: str
    generation: int
    fencing_token: int
    acquired_at: datetime
    expires_at: datetime


class PostgresControlPlaneRepositoryV103:
    """DB-API compatible PostgreSQL adapter with explicit transactions.

    It intentionally imports no driver. Deployments inject a psycopg-compatible
    connection factory and can therefore keep credentials outside the package.
    """

    backend_kind = "postgresql"

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    @contextmanager
    def transaction(self) -> Iterator[ConnectionLike]:
        connection = self._connection_factory()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def claim_due_campaign(
        self,
        campaign_id: str,
        owner_id: str,
        generation: int,
        now: datetime,
        lease_ttl: timedelta,
    ) -> LeaseReceiptV103:
        now = now.astimezone(UTC)
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT campaign_id, owner_id, generation, fencing_token, acquired_at, expires_at "
                "FROM astra_v103.claim_campaign_lease(%s, %s, %s, %s, %s)",
                (campaign_id, owner_id, generation, now, lease_ttl),
            )
            row = cursor.fetchone()
            if row is None:
                raise LeaseUnavailable("campaign lease was not granted")
            return LeaseReceiptV103(
                campaign_id=str(row[0]),
                owner_id=str(row[1]),
                generation=int(row[2]),
                fencing_token=int(row[3]),
                acquired_at=row[4],
                expires_at=row[5],
            )

    def heartbeat(
        self,
        campaign_id: str,
        owner_id: str,
        generation: int,
        fencing_token: int,
        deployment_id: str,
        build_identity: str,
        observed_at: datetime,
        lease_ttl: timedelta,
    ) -> datetime:
        observed_at = observed_at.astimezone(UTC)
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT result_code, expires_at FROM astra_v103.record_worker_heartbeat"
                "(%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    campaign_id,
                    owner_id,
                    generation,
                    fencing_token,
                    deployment_id,
                    build_identity,
                    observed_at,
                    lease_ttl,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise LeaseUnavailable("heartbeat returned no result")
            code = str(row[0])
            if code == "STALE_GENERATION":
                raise StaleGeneration(code)
            if code == "STALE_FENCING_TOKEN":
                raise StaleFencingToken(code)
            if code != "OK":
                raise LeaseUnavailable(code)
            return row[1]

    def append_event(
        self,
        campaign_id: str,
        event_type: str,
        generation: int,
        fencing_token: int,
        occurred_at: datetime,
        attributes_json: str,
        previous_digest: str,
        event_digest: str,
    ) -> int:
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT astra_v103.append_control_plane_event"
                "(%s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                (
                    campaign_id,
                    event_type,
                    generation,
                    fencing_token,
                    occurred_at.astimezone(UTC),
                    attributes_json,
                    previous_digest,
                    event_digest,
                ),
            )
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("append event returned no sequence")
            return int(row[0])

    def due_campaign_ids(self, now: datetime, limit: int = 100) -> tuple[str, ...]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self.transaction() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT campaign_id FROM astra_v103.control_plane_campaign "
                "WHERE state = 'READY' AND next_due_at <= %s AND ends_at >= %s "
                "ORDER BY next_due_at, campaign_id FOR UPDATE SKIP LOCKED LIMIT %s",
                (now.astimezone(UTC), now.astimezone(UTC), limit),
            )
            return tuple(str(row[0]) for row in cursor.fetchall())
