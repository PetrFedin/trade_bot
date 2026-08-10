from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.marketdata.historical import (
    CsvHistoricalBarSource,
    HistoricalDataPolicy,
    HistoricalDataset,
)

_MANIFEST_SCHEMA = "historical-source-manifest-v1"
_ALLOWED_SOURCE_CLASSIFICATIONS = {"THIRD_PARTY_SAMPLE_NON_AUTHORITATIVE"}


@dataclass(frozen=True)
class HistoricalWindow:
    name: str
    start: datetime
    end: datetime

    def validate(self) -> None:
        if not self.name.strip():
            raise ValueError("historical window name is required")
        for field, value in (("start", self.start), ("end", self.end)):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"historical window {field} must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("historical window start must precede end")


@dataclass(frozen=True)
class HistoricalSourceManifest:
    dataset_file: str
    symbol: str
    source_name: str
    source_classification: str
    upstream_repository: str
    upstream_path: str
    upstream_git_blob_sha: str
    upstream_license: str
    transformation: str
    row_count: int
    snapshot_sha256: str
    canonical_sha256: str
    windows: tuple[HistoricalWindow, ...]

    @classmethod
    def load(cls, path: str | Path) -> tuple[HistoricalSourceManifest, str]:
        manifest_path = Path(path)
        raw = manifest_path.read_bytes()
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("historical source manifest must be a JSON object")
        if data.get("schema_version") != _MANIFEST_SCHEMA:
            raise ValueError("historical source manifest schema mismatch")

        windows_value = data.get("windows")
        if not isinstance(windows_value, list) or not windows_value:
            raise ValueError("historical source manifest windows are required")
        windows = tuple(_window(item) for item in windows_value)

        manifest = cls(
            dataset_file=_required_text(data, "dataset_file"),
            symbol=_required_text(data, "symbol").upper(),
            source_name=_required_text(data, "source_name"),
            source_classification=_required_text(data, "source_classification"),
            upstream_repository=_required_text(data, "upstream_repository"),
            upstream_path=_required_text(data, "upstream_path"),
            upstream_git_blob_sha=_required_hex(data, "upstream_git_blob_sha", length=40),
            upstream_license=_required_text(data, "upstream_license"),
            transformation=_required_text(data, "transformation"),
            row_count=_required_positive_int(data, "row_count"),
            snapshot_sha256=_required_hex(data, "snapshot_sha256", length=64),
            canonical_sha256=_required_hex(data, "canonical_sha256", length=64),
            windows=windows,
        )
        manifest.validate()
        return manifest, hashlib.sha256(raw).hexdigest()

    def validate(self) -> None:
        if self.source_classification not in _ALLOWED_SOURCE_CLASSIFICATIONS:
            raise ValueError("historical source classification is not allowed")
        if self.symbol != self.symbol.upper() or not self.symbol:
            raise ValueError("historical source symbol must be uppercase")
        if Path(self.dataset_file).is_absolute() or ".." in Path(self.dataset_file).parts:
            raise ValueError("historical dataset_file must remain within the manifest directory")
        if self.row_count < 3:
            raise ValueError("historical source row_count must be at least three")
        previous: HistoricalWindow | None = None
        names: set[str] = set()
        for window in self.windows:
            window.validate()
            if window.name in names:
                raise ValueError("historical window names must be unique")
            names.add(window.name)
            if previous is not None and window.start < previous.end:
                raise ValueError("historical windows must be ordered and non-overlapping")
            previous = window


@dataclass(frozen=True)
class ManifestedHistoricalDataset:
    dataset: HistoricalDataset
    manifest: HistoricalSourceManifest
    manifest_sha256: str


class ManifestedCsvHistoricalBarSource:
    def __init__(
        self,
        manifest_path: str | Path,
        *,
        policy: HistoricalDataPolicy | None = None,
    ) -> None:
        self.manifest_path = Path(manifest_path)
        self.policy = HistoricalDataPolicy() if policy is None else policy
        self.policy.validate()

    def load(self) -> ManifestedHistoricalDataset:
        manifest, manifest_sha256 = HistoricalSourceManifest.load(self.manifest_path)
        base = self.manifest_path.parent.resolve()
        dataset_path = (base / manifest.dataset_file).resolve()
        if not dataset_path.is_relative_to(base):
            raise ValueError("historical dataset escaped the manifest directory")
        snapshot_sha256 = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
        if snapshot_sha256 != manifest.snapshot_sha256:
            raise ValueError("HISTORICAL_SNAPSHOT_SHA256_MISMATCH")
        dataset = CsvHistoricalBarSource(
            dataset_path,
            policy=self.policy,
            expected_symbol=manifest.symbol,
        ).load()
        if len(dataset.bars) != manifest.row_count:
            raise ValueError("HISTORICAL_SNAPSHOT_ROW_COUNT_MISMATCH")
        if dataset.canonical_sha256 != manifest.canonical_sha256:
            raise ValueError("HISTORICAL_CANONICAL_SHA256_MISMATCH")
        for window in manifest.windows:
            if not any(window.start <= bar.timestamp < window.end for bar in dataset.bars):
                raise ValueError(f"HISTORICAL_WINDOW_EMPTY:{window.name}")
        return ManifestedHistoricalDataset(
            dataset=HistoricalDataset(
                dataset_id=dataset.dataset_id,
                symbol=dataset.symbol,
                bars=dataset.bars,
                canonical_sha256=dataset.canonical_sha256,
                source_name=manifest.source_name,
                schema_version=dataset.schema_version,
            ),
            manifest=manifest,
            manifest_sha256=manifest_sha256,
        )


def _required_text(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _required_positive_int(data: dict[str, Any], field: str) -> int:
    value = data.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _required_hex(data: dict[str, Any], field: str, *, length: int) -> str:
    value = _required_text(data, field).lower()
    if len(value) != length or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field} must be {length} lowercase hexadecimal characters")
    return value


def _window(value: Any) -> HistoricalWindow:
    if not isinstance(value, dict):
        raise ValueError("historical window must be a JSON object")
    return HistoricalWindow(
        name=_required_text(value, "name"),
        start=_aware_datetime(_required_text(value, "start"), field="window.start"),
        end=_aware_datetime(_required_text(value, "end"), field="window.end"),
    )


def _aware_datetime(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed
