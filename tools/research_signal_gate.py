"""Offline research signal authenticity and durable replay gate.

This module only records directional research observations. It is not a webhook
server, trading strategy, broker adapter, OMS authority, or routing permission.
Even a valid signed signal must never create or authorize an order.

Signer keys and the SQLite path are trusted operator configuration; they are
never accepted from an untrusted signal body.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = "astra-signed-research-signal-v1"
RECEIPT_SCHEMA = "astra-research-signal-receipt-v1"
SIGN_DOMAIN = b"ASTRA_RESEARCH_SIGNAL_V1\n"
MAX_BODY_BYTES = 2048
MAX_TTL_SECONDS = 900
MAX_FUTURE_SKEW_SECONDS = 30
SUPPORTED_TIMEFRAMES = frozenset({1, 5, 15, 60, 240, 1440})
SUPPORTED_BIASES = frozenset({"BULLISH", "BEARISH", "NEUTRAL"})
FIELDS = frozenset({
    "schema_version", "source", "signal_id", "strategy_id", "symbol",
    "observed_at_utc", "expires_at_utc", "bias", "timeframe_minutes",
})
ID_RE = re.compile(r"[A-Za-z0-9_-]{12,64}\Z")
KEY_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
SYMBOL_RE = re.compile(r"[A-Z0-9]{3,20}\Z")
UTC_RE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
SIGNATURE_RE = re.compile(r"[0-9a-f]{64}\Z")


class ResearchSignalRejected(ValueError):
    """Reject untrusted, out-of-window or already-observed research signals."""


@dataclass(frozen=True)
class ResearchSigner:
    """Operator-held configuration; key bytes must never appear in receipts."""

    secret: bytes = field(repr=False)
    source: str
    symbols: frozenset[str]
    strategies: frozenset[str]


def _strict_json(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BODY_BYTES:
        raise ResearchSignalRejected("research signal body size or type rejected")

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict:
        parsed = {}
        for name, value in pairs:
            if name in parsed:
                raise ResearchSignalRejected("duplicate research signal JSON field")
            parsed[name] = value
        return parsed

    def no_float(_value: str):
        raise ResearchSignalRejected("non-integer research signal number forbidden")

    try:
        body = json.loads(
            raw, object_pairs_hook=unique_pairs, parse_float=no_float,
            parse_constant=no_float,
        )
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ResearchSignalRejected("invalid research signal JSON") from exc
    if not isinstance(body, dict) or set(body) != FIELDS:
        raise ResearchSignalRejected("research signal schema or field set rejected")
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    if raw != canonical:
        raise ResearchSignalRejected("noncanonical research signal payload")
    return body


def _utc(value: object) -> datetime:
    if not isinstance(value, str) or UTC_RE.fullmatch(value) is None:
        raise ResearchSignalRejected("noncanonical UTC research signal timestamp")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ResearchSignalRejected("invalid UTC research signal timestamp") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ResearchSignalRejected("noncanonical UTC research signal timestamp")
    return parsed


def _validate_body(body: dict, signer: ResearchSigner, now_utc: datetime) -> None:
    if (
        body["schema_version"] != SCHEMA
        or type(body["source"]) is not str
        or body["source"] != signer.source
        or type(body["signal_id"]) is not str
        or ID_RE.fullmatch(body["signal_id"]) is None
        or type(body["strategy_id"]) is not str
        or ID_RE.fullmatch(body["strategy_id"]) is None
        or body["strategy_id"] not in signer.strategies
        or type(body["symbol"]) is not str
        or SYMBOL_RE.fullmatch(body["symbol"]) is None
        or body["symbol"] not in signer.symbols
        or type(body["bias"]) is not str
        or body["bias"] not in SUPPORTED_BIASES
        or type(body["timeframe_minutes"]) is not int
        or body["timeframe_minutes"] not in SUPPORTED_TIMEFRAMES
    ):
        raise ResearchSignalRejected("research signal source, scope or schema rejected")

    observed = _utc(body["observed_at_utc"])
    expires = _utc(body["expires_at_utc"])
    if (
        not observed < expires
        or expires - observed > timedelta(seconds=MAX_TTL_SECONDS)
        or observed - now_utc > timedelta(seconds=MAX_FUTURE_SKEW_SECONDS)
        or expires <= now_utc
    ):
        raise ResearchSignalRejected("stale, premature or oversized research signal window")


def _journal_path(path: Path) -> Path:
    path = Path(path)
    if any(part.is_symlink() for part in (path, path.parent, *path.parent.parents)):
        raise ResearchSignalRejected("symlinked research replay journal path forbidden")
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ResearchSignalRejected("unsafe research replay journal inode")
    return path


def verify_and_record_research_signal(
    *, raw_body: bytes, key_id: str, signature_hex: str,
    trusted_keys: Mapping[str, ResearchSigner], now_utc: datetime,
    journal_path: Path,
) -> dict:
    """Verify HMAC and time scope; atomically remember source/signal ID.

    Receipt means authenticated research observation ONLY, not a signal to buy,
    a risk approval, a strategy promotion, or authorization to contact a broker.
    On journal corruption, contention or unavailability, fail closed.
    """
    if not isinstance(now_utc, datetime) or now_utc.tzinfo is None:
        raise ResearchSignalRejected("trusted UTC clock required")
    if now_utc.utcoffset() != timedelta(0):
        raise ResearchSignalRejected("trusted UTC clock required")
    if not isinstance(key_id, str) or KEY_RE.fullmatch(key_id) is None:
        raise ResearchSignalRejected("research signer key identifier rejected")
    if not isinstance(signature_hex, str) or SIGNATURE_RE.fullmatch(signature_hex) is None:
        raise ResearchSignalRejected("research signature format rejected")
    signer = trusted_keys.get(key_id)
    if (
        not isinstance(signer, ResearchSigner)
        or type(signer.secret) is not bytes
        or len(signer.secret) < 32
        or not isinstance(signer.source, str)
        or not isinstance(signer.symbols, frozenset)
        or not isinstance(signer.strategies, frozenset)
    ):
        raise ResearchSignalRejected("research signing key unavailable or misconfigured")

    body = _strict_json(raw_body)
    expected = hmac.new(
        signer.secret, SIGN_DOMAIN + key_id.encode("ascii") + b"\n" + raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature_hex):
        raise ResearchSignalRejected("research signature mismatch")
    _validate_body(body, signer, now_utc)

    journal_path = _journal_path(journal_path)
    payload_sha256 = hashlib.sha256(raw_body).hexdigest()
    received = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        # The dedicated journal is not a trading ledger. SQLite's UNIQUE index
        # and BEGIN IMMEDIATE make independent competing writers serialize.
        connection = sqlite3.connect(
            str(journal_path), timeout=0, isolation_level=None,
        )
        try:
            connection.execute("PRAGMA synchronous=FULL")
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or mode[0].lower() != "wal":
                raise ResearchSignalRejected("durable replay journal mode unavailable")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS research_observations ("
                    " source TEXT NOT NULL, signal_id TEXT NOT NULL,"
                    " payload_sha256 TEXT NOT NULL, received_at_utc TEXT NOT NULL,"
                    " PRIMARY KEY (source, signal_id)) WITHOUT ROWID"
                )
                connection.execute(
                    "INSERT INTO research_observations "
                    "(source, signal_id, payload_sha256, received_at_utc)"
                    " VALUES (?, ?, ?, ?)",
                    (body["source"], body["signal_id"], payload_sha256, received),
                )
                connection.execute("COMMIT")
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()
    except sqlite3.IntegrityError as exc:
        raise ResearchSignalRejected("duplicate research signal identifier") from exc
    except sqlite3.Error as exc:
        raise ResearchSignalRejected("research replay journal unavailable") from exc

    return {
        "schema_version": RECEIPT_SCHEMA,
        "decision": "RECORDED_RESEARCH_ONLY",
        "source": body["source"],
        "signal_id": body["signal_id"],
        "payload_sha256": payload_sha256,
        "observed_at_utc": body["observed_at_utc"],
        "received_at_utc": received,
        "broker_order_routing_allowed": False,
        "demo_trading_allowed": False,
        "live_trading_allowed": False,
        "strategy_promotion_allowed": False,
        "profitability_proven": False,
    }
