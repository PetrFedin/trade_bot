from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.strategy.crypto_readonly_account_context import (
    CryptoReadOnlyAccountAwareRegistrySnapshot,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None


class PostgresCryptoReadOnlyAccountContextStore:
    """Append-only audit store for the real-account context used by a ranking."""

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("read-only account context PostgreSQL DSN is required")
        self._dsn = dsn

    @property
    def order_writes_supported(self) -> bool:
        return False

    @property
    def live_mainnet_order_routing_allowed(self) -> bool:
        return False

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    def migrate(
        self,
        path: str | Path = "migrations/v115/001_bybit_mainnet_readonly_ranking_context.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def persist(
        self,
        snapshot: CryptoReadOnlyAccountAwareRegistrySnapshot,
        *,
        created_at: datetime | None = None,
    ) -> str:
        snapshot.validate()
        moment = datetime.now(UTC) if created_at is None else _utc(created_at)
        account = snapshot.account
        payload = snapshot.to_payload()
        payload_json = _canonical_json(payload)
        context_id = snapshot.snapshot_id
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_mainnet_readonly_context_v115
                        (context_snapshot_id, ranking_snapshot_id, observed_at, api_host,
                         api_key_fingerprint_sha256, equity_source, total_equity_usd,
                         total_wallet_balance_usd, total_margin_balance_usd,
                         total_available_balance_usd, total_perp_upl_usd,
                         total_initial_margin_usd, total_maintenance_margin_usd,
                         sizing_capital_usd_equivalent, gross_position_value_usd,
                         long_position_value_usd, short_position_value_usd,
                         net_position_value_usd, open_position_count,
                         position_exposure_complete, context_json, read_only_verified,
                         ip_binding_verified, operator_review_required, trade_actionable,
                         order_writes_supported, bybit_live_order_routing_allowed, created_at)
                        VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         %s, %s, %s, %s, %s, %s, %s::jsonb, true, true, true, false,
                         false, false, %s)
                        ON CONFLICT (context_snapshot_id) DO NOTHING""",
                        (
                            context_id,
                            snapshot.ranking_snapshot_id,
                            datetime.fromisoformat(snapshot.observed_at),
                            account.api_host,
                            account.api_key_fingerprint_sha256,
                            account.equity_source,
                            account.total_equity_usd,
                            account.total_wallet_balance_usd,
                            account.total_margin_balance_usd,
                            account.total_available_balance_usd,
                            account.total_perp_upl_usd,
                            account.total_initial_margin_usd,
                            account.total_maintenance_margin_usd,
                            account.sizing_capital_usd_equivalent,
                            account.gross_position_value_usd,
                            account.long_position_value_usd,
                            account.short_position_value_usd,
                            account.net_position_value_usd,
                            account.open_position_count,
                            account.position_exposure_complete,
                            payload_json,
                            moment,
                        ),
                    )
                    if cursor.rowcount == 0:
                        cursor.execute(
                            """SELECT ranking_snapshot_id, context_json
                            FROM astra_bybit_mainnet_readonly_context_v115
                            WHERE context_snapshot_id=%s""",
                            (context_id,),
                        )
                        row = cursor.fetchone()
                        if row is None:
                            raise RuntimeError(
                                "read-only account context idempotency lookup lost row"
                            )
                        if row["ranking_snapshot_id"] != snapshot.ranking_snapshot_id:
                            raise ValueError("read-only account context ranking identity mismatch")
                        if _canonical_json(row["context_json"]) != payload_json:
                            raise ValueError("read-only account context immutable payload mismatch")
        return context_id


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("read-only account context timestamp must be timezone-aware")
    return value.astimezone(UTC)
