"""Independent structural and cross-source audit of retained 60m Mark Price CSVs.

The verifier compares Mark Price candles, settlement intersections and the
separately retained funding archive. A passing audit checks internal evidence
consistency, NOT cryptographic provider authenticity or an actual account ledger.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

HOUR = timedelta(hours=1)
LIMIT = 1000
MARK_ENDPOINT = "https://api.bytick.com/v5/market/mark-price-kline"
MARK_HEADER = ["timestamp", "symbol", "open", "high", "low", "close"]
FUNDING_HEADER = ["timestamp", "symbol", "funding_rate"]
INTERSECTION_HEADER = [
    "timestamp", "symbol", "funding_rate", "mark_open", "mark_high",
    "mark_low", "mark_close", "hour_range_fraction",
]


class MarkArchiveError(ValueError):
    """Refuse an incomplete or contradictory Mark Price / funding source."""


def _file_hash(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        raise MarkArchiveError(f"missing or symlinked source: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict:
    _file_hash(path)
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise MarkArchiveError(f"invalid JSON source: {path}") from exc
    if not isinstance(result, dict):
        raise MarkArchiveError(f"non-object JSON source: {path}")
    return result


def _csv(path: Path, expected_header: list[str]) -> list[dict[str, str]]:
    _file_hash(path)
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, restkey="__extra__")
            if reader.fieldnames != expected_header:
                raise MarkArchiveError(f"unexpected CSV header: {path}")
            rows = list(reader)
    except (csv.Error, UnicodeError) as exc:
        raise MarkArchiveError(f"invalid CSV: {path}") from exc
    if not rows or any(
        row.get("__extra__") is not None or any(row.get(key) is None for key in expected_header)
        for row in rows
    ):
        raise MarkArchiveError(f"empty or malformed CSV rows: {path}")
    return rows


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise MarkArchiveError("invalid UTC timestamp") from exc
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() != timedelta(0)
        or (parsed.minute, parsed.second, parsed.microsecond) != (0, 0, 0)
    ):
        raise MarkArchiveError("timestamp must be exactly on a UTC hour")
    return parsed.astimezone(UTC)


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise MarkArchiveError("invalid numeric evidence") from exc
    if not parsed.is_finite():
        raise MarkArchiveError("non-finite numeric evidence")
    return parsed


def verify_symbol_archive(
    *, root: Path, funding_root: Path, symbol: str, mark_manifest: dict
) -> dict:
    """Reconstruct all archive invariants without trusting manifest counts.

    funding_root is mandatory: it prevents a self-consistent but falsified
    settlement-intersection CSV from silently changing historical funding rates.
    """
    directory = root / symbol
    raw_path = directory / f"{symbol}_mark_60m.csv"
    joined_path = directory / f"{symbol}_funding_mark_60m.csv"
    funding_path = funding_root / f"{symbol}_funding.csv"
    funding_manifest_path = funding_root / f"{symbol}_funding.manifest.json"

    if mark_manifest.get("endpoint") != MARK_ENDPOINT:
        raise MarkArchiveError("unexpected public Mark Price endpoint claim")
    for key in (
        "provider_payload_signature_verified", "exact_settlement_mark_verified",
        "account_funding_ledger_verified", "compatible_with_final_1m_admission",
        "strategy_replay_completed", "strategy_promotion_allowed", "profitability_proven",
    ):
        if mark_manifest.get(key) is not False:
            raise MarkArchiveError(f"unsupported proof flag: {key}")
    if mark_manifest.get("evaluation_class") != "COARSE_PUBLIC_MARK_PRICE_ENVELOPE_RESEARCH_ONLY":
        raise MarkArchiveError("unsupported research classification")
    if _file_hash(raw_path) != mark_manifest.get("raw_mark_sha256"):
        raise MarkArchiveError("raw Mark Price hash mismatch")
    if _file_hash(joined_path) != mark_manifest.get("settlement_mark_sha256"):
        raise MarkArchiveError("settlement Mark Price hash mismatch")

    start = _utc(mark_manifest.get("start"))
    end = _utc(mark_manifest.get("end"))
    if end < start:
        raise MarkArchiveError("invalid Mark Price date range")
    expected_hours = int((end - start) / HOUR) + 1
    marks = {}
    raw_rows = _csv(raw_path, MARK_HEADER)
    for index, row in enumerate(raw_rows):
        stamp = _utc(row["timestamp"])
        if stamp != start + index * HOUR or row["symbol"] != symbol:
            raise MarkArchiveError("Mark Price hours are missing, duplicated or contaminated")
        values = tuple(_decimal(row[key]) for key in MARK_HEADER[2:])
        open_, high, low, close = values
        if min(values) <= 0 or high < max(open_, low, close) or low > min(open_, high, close):
            raise MarkArchiveError("invalid Mark Price OHLC")
        marks[stamp] = values
    if len(raw_rows) != expected_hours or _utc(raw_rows[-1]["timestamp"]) != end:
        raise MarkArchiveError("Mark Price chronology is incomplete")
    if mark_manifest.get("hourly_rows") != expected_hours:
        raise MarkArchiveError("Mark Price row count differs from manifest")

    funding_manifest = _json(funding_manifest_path)
    if (
        funding_manifest.get("schema_version") != "bybit-funding-snapshot-v2"
        or funding_manifest.get("category") != "linear"
        or funding_manifest.get("symbol") != symbol
        or funding_manifest.get("dataset_sha256") != _file_hash(funding_path)
        or mark_manifest.get("funding_source_sha256") != _file_hash(funding_path)
        or mark_manifest.get("funding_manifest_sha256") != _file_hash(funding_manifest_path)
    ):
        raise MarkArchiveError("retained funding source or manifest mismatch")
    funding_rows = _csv(funding_path, FUNDING_HEADER)
    funding = {}
    previous = None
    for row in funding_rows:
        stamp = _utc(row["timestamp"])
        if row["symbol"] != symbol or (previous is not None and stamp <= previous):
            raise MarkArchiveError("funding chronology or symbol mismatch")
        funding[stamp] = _decimal(row["funding_rate"])
        previous = stamp
    if (
        funding_manifest.get("settlement_count") != len(funding)
        or _utc(funding_manifest.get("first_settlement")) != next(iter(funding))
        or _utc(funding_manifest.get("last_settlement")) != previous
    ):
        raise MarkArchiveError("funding manifest chronology mismatch")
    in_window = {stamp: rate for stamp, rate in funding.items() if start <= stamp <= end}
    if not in_window:
        raise MarkArchiveError("no funding events inside Mark Price window")

    settlement_rows = _csv(joined_path, INTERSECTION_HEADER)
    if len(settlement_rows) != len(in_window):
        raise MarkArchiveError("funding settlement identity/count mismatch")
    widths = []
    for (stamp, rate), row in zip(in_window.items(), settlement_rows, strict=True):
        if _utc(row["timestamp"]) != stamp or row["symbol"] != symbol:
            raise MarkArchiveError("funding settlement timestamp/symbol mismatch")
        if _decimal(row["funding_rate"]) != rate:
            raise MarkArchiveError("funding rate differs from independently retained source")
        values = tuple(_decimal(row[key]) for key in INTERSECTION_HEADER[3:7])
        if values != marks[stamp]:
            raise MarkArchiveError("settlement Mark Price differs from hourly source")
        high, low = values[1], values[2]
        width = (high - low) / ((high + low) / Decimal("2"))
        if _decimal(row["hour_range_fraction"]) != width:
            raise MarkArchiveError("settlement Mark Price range fraction mismatch")
        widths.append(width)
    mean = sum(widths, Decimal("0")) / Decimal(len(widths))
    maximum = max(widths)
    if (
        mark_manifest.get("funding_settlements") != len(widths)
        or _decimal(mark_manifest.get("mean_settlement_hour_range_fraction")) != mean
        or _decimal(mark_manifest.get("maximum_settlement_hour_range_fraction")) != maximum
    ):
        raise MarkArchiveError("settlement count/range statistics mismatch")

    pages = mark_manifest.get("provider_query_pages")
    if not isinstance(pages, list) or len(pages) != (expected_hours + LIMIT - 1) // LIMIT:
        raise MarkArchiveError("provider pagination coverage mismatch")
    if (
        mark_manifest.get("provider_response_representation")
        != "CANONICAL_DECODED_JSON_NOT_HTTP_WIRE"
    ):
        raise MarkArchiveError("canonical provider response archive missing")
    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            raise MarkArchiveError("malformed provider page")
        first = start + HOUR * (index * LIMIT)
        last = min(end, first + HOUR * (LIMIT - 1))
        size = int((last - first) / HOUR) + 1
        query = {
            "category": "linear", "symbol": symbol, "interval": "60",
            "start": int(first.timestamp() * 1000),
            "end": int(last.timestamp() * 1000), "limit": LIMIT,
        }
        if (
            page.get("page") != index or page.get("rows") != size
            or page.get("first_hour") != first.isoformat()
            or page.get("last_hour") != last.isoformat()
            or page.get("query") != query
            or not isinstance(page.get("provider_result_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", page["provider_result_sha256"]) is None
        ):
            raise MarkArchiveError("provider page chronology/query evidence mismatch")
        relative = page.get("provider_response_path")
        filename = f"{symbol}_mark_60m_page_{index:03d}.json"
        if relative != f"provider_pages/{filename}":
            raise MarkArchiveError("provider response archive path mismatch")
        source = directory / "provider_pages" / filename
        raw = source.read_bytes() if source.is_file() and not source.is_symlink() else None
        if raw is None or hashlib.sha256(raw).hexdigest() != page.get("provider_response_sha256"):
            raise MarkArchiveError("provider response archive hash mismatch")
        try:
            payload = json.loads(raw)
        except (UnicodeError, ValueError) as exc:
            raise MarkArchiveError("invalid archived provider response") from exc
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if canonical != raw:
            raise MarkArchiveError("provider response not canonical JSON")
        result = payload.get("result") if isinstance(payload, dict) else None
        if (
            payload.get("retCode") != 0
            or not isinstance(result, dict)
            or result.get("category") != "linear"
            or result.get("symbol") != symbol
            or not isinstance(result.get("list"), list)
            or len(result["list"]) != size
        ):
            raise MarkArchiveError("provider response contract mismatch")
        digest = hashlib.sha256(
            json.dumps(
                result, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
        ).hexdigest()
        if digest != page.get("provider_result_sha256"):
            raise MarkArchiveError("provider result digest mismatch")
        seen = set()
        for provider_row in result["list"]:
            if not isinstance(provider_row, list) or len(provider_row) < 5:
                raise MarkArchiveError("malformed provider candle")
            try:
                milliseconds = int(provider_row[0])
                stamp = datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
            except (ValueError, TypeError, OverflowError, OSError) as exc:
                raise MarkArchiveError("invalid provider candle timestamp") from exc
            if milliseconds != int(stamp.timestamp() * 1000) or stamp in seen:
                raise MarkArchiveError("provider timestamp precision or duplication")
            seen.add(stamp)
            if not first <= stamp <= last or stamp not in marks:
                raise MarkArchiveError("provider candle outside archived chronology")
            if tuple(_decimal(value) for value in provider_row[1:5]) != marks[stamp]:
                raise MarkArchiveError("archived provider candle differs from Mark Price CSV")
        if seen != {first + HOUR * offset for offset in range(size)}:
            raise MarkArchiveError("provider page coverage mismatch")
    return {
        "hourly_rows": expected_hours,
        "funding_settlements": len(widths),
        "provider_pages": len(pages),
        "mean_settlement_hour_range_fraction": mean,
        "maximum_settlement_hour_range_fraction": maximum,
    }
