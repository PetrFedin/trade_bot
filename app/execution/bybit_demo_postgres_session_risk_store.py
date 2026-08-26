from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_session_risk_ledger import (
    BybitDemoSessionRiskLedger,
    BybitDemoSessionTradeOutcome,
)
from app.execution.bybit_demo_session_risk_store import (
    BybitDemoSessionRiskLedgerCheckpoint,
    _decode_checkpoint,
    _decode_outcome,
    _encode_checkpoint,
    _validate_expected_opening_equity,
    _validate_revision,
)

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency boundary
    psycopg = None
    dict_row = None

_SESSION_NAME = "ACTIVE"


class PostgresBybitDemoSessionRiskLedgerStore:
    """Durable Demo session-risk ledger with CAS and immutable terminal outcomes."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    automatic_reset_allowed = False
    immutable_trade_outcomes = True

    def __init__(self, dsn: str) -> None:
        if not dsn.strip():
            raise ValueError("demo session-risk PostgreSQL DSN is required")
        self._dsn = dsn

    def _connect(self):
        if psycopg is None or dict_row is None:
            raise RuntimeError("PostgreSQL dependency is unavailable")
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    def migrate(
        self,
        path: str | Path = "migrations/v122/001_bybit_demo_postgres_session_risk.sql",
    ) -> None:
        sql = Path(path).read_text(encoding="utf-8")
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql)
            connection.commit()

    def load_active(self) -> BybitDemoSessionRiskLedgerCheckpoint:
        """Load the one initialized operational session without inventing opening equity."""

        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    return _load_and_verify(cursor, lock=False)

    def load(
        self,
        *,
        expected_opening_equity_usdt: Decimal,
    ) -> BybitDemoSessionRiskLedgerCheckpoint:
        _validate_expected_opening_equity(expected_opening_equity_usdt)
        checkpoint = self.load_active()
        if checkpoint.ledger.opening_equity_usdt != expected_opening_equity_usdt:
            raise ValueError("demo session ledger checkpoint opening equity mismatch")
        return checkpoint

    def initialize(
        self,
        ledger: BybitDemoSessionRiskLedger,
    ) -> BybitDemoSessionRiskLedgerCheckpoint:
        ledger.validate()
        if ledger.outcomes:
            raise ValueError("new Demo session-risk ledger must start without historical outcomes")
        canonical, revision = _encode_checkpoint(ledger)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """INSERT INTO astra_bybit_demo_session_risk_v122(
                               session_name,
                               opening_equity_usdt,
                               peak_equity_usdt,
                               ledger_revision,
                               canonical_checkpoint,
                               outcome_count,
                               diagnostics_only,
                               order_writes_supported,
                               live_mainnet_order_routing_allowed,
                               created_at,
                               updated_at
                           ) VALUES (
                               %s, %s, %s, %s, %s, 0,
                               true, false, false, now(), now()
                           )
                           ON CONFLICT (session_name) DO NOTHING""",
                        (
                            _SESSION_NAME,
                            ledger.opening_equity_usdt,
                            ledger.effective_peak_equity_usdt,
                            revision,
                            canonical,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise FileExistsError(
                            "demo session-risk PostgreSQL ledger already exists"
                        )
                    checkpoint = _load_and_verify(cursor, lock=True)
        if checkpoint.revision != revision:
            raise RuntimeError("demo session-risk initialized revision mismatch")
        return checkpoint

    def save(
        self,
        ledger: BybitDemoSessionRiskLedger,
        *,
        expected_revision: str,
    ) -> BybitDemoSessionRiskLedgerCheckpoint:
        ledger.validate()
        _validate_revision(expected_revision)
        canonical, revision = _encode_checkpoint(ledger)
        with self._connect() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    current = _load_and_verify(cursor, lock=True)
                    if current.revision != expected_revision:
                        raise RuntimeError(
                            "demo session ledger checkpoint revision changed concurrently"
                        )
                    _validate_progression(current.ledger, ledger)
                    _persist_outcomes(cursor, ledger.outcomes)
                    cursor.execute(
                        """UPDATE astra_bybit_demo_session_risk_v122
                           SET peak_equity_usdt=%s,
                               ledger_revision=%s,
                               canonical_checkpoint=%s,
                               outcome_count=%s,
                               updated_at=now()
                           WHERE session_name=%s AND ledger_revision=%s""",
                        (
                            ledger.effective_peak_equity_usdt,
                            revision,
                            canonical,
                            len(ledger.outcomes),
                            _SESSION_NAME,
                            expected_revision,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            "demo session ledger checkpoint revision changed concurrently"
                        )
                    checkpoint = _load_and_verify(cursor, lock=True)
        if checkpoint.revision != revision:
            raise RuntimeError("demo session-risk persisted revision mismatch")
        return checkpoint


def _load_and_verify(cursor, *, lock: bool) -> BybitDemoSessionRiskLedgerCheckpoint:
    if lock:
        cursor.execute(
            """SELECT session_name,
                      opening_equity_usdt,
                      peak_equity_usdt,
                      ledger_revision,
                      canonical_checkpoint,
                      outcome_count,
                      diagnostics_only,
                      order_writes_supported,
                      live_mainnet_order_routing_allowed
               FROM astra_bybit_demo_session_risk_v122
               WHERE session_name=%s
               FOR UPDATE""",
            (_SESSION_NAME,),
        )
    else:
        cursor.execute(
            """SELECT session_name,
                      opening_equity_usdt,
                      peak_equity_usdt,
                      ledger_revision,
                      canonical_checkpoint,
                      outcome_count,
                      diagnostics_only,
                      order_writes_supported,
                      live_mainnet_order_routing_allowed
               FROM astra_bybit_demo_session_risk_v122
               WHERE session_name=%s""",
            (_SESSION_NAME,),
        )
    row = cursor.fetchone()
    if row is None:
        raise FileNotFoundError("demo session-risk PostgreSQL ledger does not exist")
    checkpoint = _decode_checkpoint(row["canonical_checkpoint"])
    _validate_active_row(row, checkpoint)
    outcomes = _load_journal(cursor)
    if checkpoint.ledger.outcomes != outcomes:
        raise ValueError("demo session-risk checkpoint and outcome journal disagree")
    return checkpoint


def _validate_active_row(
    row: Any,
    checkpoint: BybitDemoSessionRiskLedgerCheckpoint,
) -> None:
    if row["session_name"] != _SESSION_NAME:
        raise ValueError("demo session-risk active row identity is invalid")
    if row["diagnostics_only"] is not True:
        raise ValueError("demo session-risk active row lost diagnostics-only marker")
    if row["order_writes_supported"] is not False:
        raise ValueError("demo session-risk active row cannot write orders")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("demo session-risk active row cannot permit mainnet routing")
    if row["ledger_revision"] != checkpoint.revision:
        raise ValueError("demo session-risk active row revision mismatch")
    if row["opening_equity_usdt"] != checkpoint.ledger.opening_equity_usdt:
        raise ValueError("demo session-risk active row opening equity mismatch")
    if row["peak_equity_usdt"] != checkpoint.ledger.effective_peak_equity_usdt:
        raise ValueError("demo session-risk active row peak equity mismatch")
    if int(row["outcome_count"]) != len(checkpoint.ledger.outcomes):
        raise ValueError("demo session-risk active row outcome count mismatch")


def _load_journal(cursor) -> tuple[BybitDemoSessionTradeOutcome, ...]:
    cursor.execute(
        """SELECT entry_order_link_id,
                  symbol,
                  created_time_ms,
                  updated_time_ms,
                  all_in_net_pnl_usdt,
                  execution_fees_usdt,
                  record_sha256,
                  canonical_record,
                  immutable_record,
                  diagnostics_only,
                  order_writes_supported,
                  live_mainnet_order_routing_allowed
           FROM astra_bybit_demo_session_trade_outcome_v122
           ORDER BY updated_time_ms, created_time_ms, entry_order_link_id"""
    )
    return tuple(_decode_journal_row(row) for row in cursor.fetchall())


def _decode_journal_row(row: Any) -> BybitDemoSessionTradeOutcome:
    if row["immutable_record"] is not True or row["diagnostics_only"] is not True:
        raise ValueError("demo session-risk journal lost immutable diagnostics marker")
    if row["order_writes_supported"] is not False:
        raise ValueError("demo session-risk journal cannot write orders")
    if row["live_mainnet_order_routing_allowed"] is not False:
        raise ValueError("demo session-risk journal cannot permit mainnet routing")
    canonical = row["canonical_record"]
    record_sha = row["record_sha256"]
    if not isinstance(canonical, str) or not canonical:
        raise ValueError("demo session-risk journal canonical record is missing")
    if not _is_sha256(record_sha):
        raise ValueError("demo session-risk journal checksum is invalid")
    calculated = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if calculated != record_sha:
        raise ValueError("demo session-risk journal checksum mismatch")
    try:
        payload = json.loads(canonical)
    except json.JSONDecodeError as exc:
        raise ValueError("demo session-risk journal record is invalid JSON") from exc
    outcome = _decode_outcome(payload)
    if (
        row["entry_order_link_id"] != outcome.entry_order_link_id
        or row["symbol"] != outcome.symbol
        or int(row["created_time_ms"]) != outcome.created_time_ms
        or int(row["updated_time_ms"]) != outcome.updated_time_ms
        or row["all_in_net_pnl_usdt"] != outcome.all_in_net_pnl_usdt
        or row["execution_fees_usdt"] != outcome.execution_fees_usdt
    ):
        raise ValueError("demo session-risk journal columns disagree with canonical record")
    return outcome


def _persist_outcomes(
    cursor,
    outcomes: tuple[BybitDemoSessionTradeOutcome, ...],
) -> None:
    for outcome in outcomes:
        outcome.validate()
        canonical, record_sha = _encode_outcome(outcome)
        cursor.execute(
            """INSERT INTO astra_bybit_demo_session_trade_outcome_v122(
                   entry_order_link_id,
                   symbol,
                   created_time_ms,
                   updated_time_ms,
                   all_in_net_pnl_usdt,
                   execution_fees_usdt,
                   record_sha256,
                   canonical_record,
                   immutable_record,
                   diagnostics_only,
                   order_writes_supported,
                   live_mainnet_order_routing_allowed,
                   created_at
               ) VALUES (
                   %s, %s, %s, %s, %s, %s, %s, %s,
                   true, true, false, false, now()
               )
               ON CONFLICT (entry_order_link_id) DO NOTHING""",
            (
                outcome.entry_order_link_id,
                outcome.symbol,
                outcome.created_time_ms,
                outcome.updated_time_ms,
                outcome.all_in_net_pnl_usdt,
                outcome.execution_fees_usdt,
                record_sha,
                canonical,
            ),
        )
        if cursor.rowcount == 1:
            continue
        cursor.execute(
            """SELECT entry_order_link_id,
                      symbol,
                      created_time_ms,
                      updated_time_ms,
                      all_in_net_pnl_usdt,
                      execution_fees_usdt,
                      record_sha256,
                      canonical_record,
                      immutable_record,
                      diagnostics_only,
                      order_writes_supported,
                      live_mainnet_order_routing_allowed
               FROM astra_bybit_demo_session_trade_outcome_v122
               WHERE entry_order_link_id=%s""",
            (outcome.entry_order_link_id,),
        )
        existing = cursor.fetchone()
        if existing is None:
            raise RuntimeError("demo session-risk outcome conflict could not be reloaded")
        if _decode_journal_row(existing) != outcome:
            raise ValueError(
                "demo session-risk trade identity has conflicting reconciled economics"
            )


def _encode_outcome(outcome: BybitDemoSessionTradeOutcome) -> tuple[str, str]:
    payload = {
        "entry_order_link_id": outcome.entry_order_link_id,
        "symbol": outcome.symbol,
        "created_time_ms": outcome.created_time_ms,
        "updated_time_ms": outcome.updated_time_ms,
        "all_in_net_pnl_usdt": str(outcome.all_in_net_pnl_usdt),
        "execution_fees_usdt": str(outcome.execution_fees_usdt),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return canonical, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_progression(
    current: BybitDemoSessionRiskLedger,
    proposed: BybitDemoSessionRiskLedger,
) -> None:
    if proposed.opening_equity_usdt != current.opening_equity_usdt:
        raise ValueError("demo session-risk opening equity is immutable")
    if proposed.effective_peak_equity_usdt < current.effective_peak_equity_usdt:
        raise ValueError("demo session-risk peak equity cannot decrease")
    proposed_by_key = {
        outcome.entry_order_link_id: outcome
        for outcome in proposed.outcomes
    }
    for outcome in current.outcomes:
        if proposed_by_key.get(outcome.entry_order_link_id) != outcome:
            raise ValueError("demo session-risk historical outcome cannot change or disappear")


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = ["PostgresBybitDemoSessionRiskLedgerStore"]
