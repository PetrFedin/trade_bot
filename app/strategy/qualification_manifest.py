from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from app.marketdata.historical import HistoricalDataset
from app.strategy.momentum import LongOnlyMomentumStrategy
from app.strategy.qualification import StrategyQualification
from app.strategy.regimes import HistoricalRegime, MultiRegimeQualification, MultiRegimeQualifier

MANIFEST_SCHEMA = "strategy-qualification-manifest-v1"
IMPLEMENTATION_ID = "app.strategy.momentum.LongOnlyMomentumStrategy"


class QualificationManifestError(ValueError):
    pass


def _decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise QualificationManifestError("manifest decimals must be finite")
    normalized = value.normalize()
    if normalized == 0:
        return "0"
    return format(normalized, "f")


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise QualificationManifestError("manifest timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_sha256(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strategy_implementation_sha256() -> str:
    return file_sha256(Path(__file__).with_name("momentum.py"))


@dataclass(frozen=True)
class DatasetProvenance:
    classification: str
    provider: str
    source_reference: str

    def validate(self) -> None:
        allowed = {"synthetic_fixture", "external_historical"}
        if self.classification not in allowed:
            raise QualificationManifestError(
                f"dataset classification must be one of {sorted(allowed)}"
            )
        if not self.provider.strip():
            raise QualificationManifestError("dataset provider is required")
        if not self.source_reference.strip():
            raise QualificationManifestError("dataset source_reference is required")
        if self.classification == "external_historical" and self.provider.strip().lower() in {
            "fixture",
            "synthetic",
        }:
            raise QualificationManifestError(
                "external_historical dataset cannot use a fixture/synthetic provider"
            )

    def to_dict(self) -> dict[str, str]:
        self.validate()
        return {
            "classification": self.classification,
            "provider": self.provider.strip(),
            "source_reference": self.source_reference.strip(),
        }


def _strategy_payload(strategy: LongOnlyMomentumStrategy) -> dict[str, Any]:
    return {
        "implementation": IMPLEMENTATION_ID,
        "implementation_sha256": strategy_implementation_sha256(),
        "strategy_id": strategy.strategy_id,
        "parameters": {"target_quantity": _decimal(strategy.target_quantity)},
    }


def _dataset_payload(
    dataset: HistoricalDataset,
    provenance: DatasetProvenance,
) -> dict[str, Any]:
    return {
        "dataset_id": dataset.dataset_id,
        "canonical_sha256": dataset.canonical_sha256,
        "schema_version": dataset.schema_version,
        "symbol": dataset.symbol,
        "bars": len(dataset.bars),
        "first_timestamp": _timestamp(dataset.first_timestamp),
        "last_timestamp": _timestamp(dataset.last_timestamp),
        "source_name": dataset.source_name,
        "provenance": provenance.to_dict(),
    }


def _contract_payload(
    qualifier: MultiRegimeQualifier,
    regimes: tuple[HistoricalRegime, ...],
    *,
    spec_sha256: str,
) -> dict[str, Any]:
    if len(spec_sha256) != 64 or any(char not in "0123456789abcdef" for char in spec_sha256):
        raise QualificationManifestError("spec_sha256 must be lowercase SHA-256 hex")
    walk = qualifier.qualifier
    policy = walk.policy
    backtest = walk.backtest_config
    return {
        "spec_sha256": spec_sha256,
        "minimum_regimes": qualifier.minimum_regimes,
        "walk_forward_policy": {
            "training_bars": policy.training_bars,
            "testing_bars": policy.testing_bars,
            "step_bars": policy.step_bars,
            "minimum_windows": policy.minimum_windows,
            "maximum_drawdown_fraction": _decimal(policy.maximum_drawdown_fraction),
            "minimum_mean_oos_return": _decimal(policy.minimum_mean_oos_return),
            "minimum_mean_excess_return": _decimal(policy.minimum_mean_excess_return),
            "require_trade_in_each_window": policy.require_trade_in_each_window,
        },
        "backtest_config": {
            "opening_cash": _decimal(backtest.opening_cash),
            "fee_per_fill": _decimal(backtest.fee_per_fill),
            "slippage_bps": _decimal(backtest.slippage_bps),
            "minimum_history_bars": backtest.minimum_history_bars,
        },
        "regimes": [
            {
                "name": regime.name,
                "start": _timestamp(regime.start),
                "end": _timestamp(regime.end),
            }
            for regime in regimes
        ],
    }


def _qualification_payload(value: StrategyQualification) -> dict[str, Any]:
    return {
        "qualified": value.qualified,
        "reasons": list(value.reasons),
        "mean_oos_return": _decimal(value.mean_oos_return),
        "mean_excess_return": _decimal(value.mean_excess_return),
        "worst_drawdown_fraction": _decimal(value.worst_drawdown_fraction),
        "total_trades": value.total_trades,
        "windows": [
            {
                "window_number": window.window_number,
                "training_start": window.training_start,
                "execution_start": window.execution_start,
                "execution_end": window.execution_end,
                "strategy_return": _decimal(window.strategy_return),
                "benchmark_return": _decimal(window.benchmark_return),
                "excess_return": _decimal(window.excess_return),
                "max_drawdown_fraction": _decimal(window.max_drawdown_fraction),
                "trades": window.trades,
            }
            for window in value.windows
        ],
    }


def _result_payload(result: MultiRegimeQualification) -> dict[str, Any]:
    return {
        "qualified": result.qualified,
        "dataset_id": result.dataset_id,
        "dataset_sha256": result.dataset_sha256,
        "reasons": list(result.reasons),
        "regimes": [
            {
                "name": item.regime.name,
                "start": _timestamp(item.regime.start),
                "end": _timestamp(item.regime.end),
                "bars": item.bars,
                "qualification": _qualification_payload(item.qualification),
            }
            for item in result.results
        ],
    }


def build_qualification_manifest(
    *,
    dataset: HistoricalDataset,
    provenance: DatasetProvenance,
    strategy: LongOnlyMomentumStrategy,
    qualifier: MultiRegimeQualifier,
    regimes: tuple[HistoricalRegime, ...],
    spec_sha256: str,
) -> dict[str, Any]:
    result = qualifier.qualify(dataset, regimes)
    strategy_payload = _strategy_payload(strategy)
    dataset_payload = _dataset_payload(dataset, provenance)
    contract_payload = _contract_payload(qualifier, regimes, spec_sha256=spec_sha256)
    input_payload = {
        "schema_version": MANIFEST_SCHEMA,
        "strategy": strategy_payload,
        "dataset": dataset_payload,
        "qualification_contract": contract_payload,
    }
    result_payload = _result_payload(result)
    manifest: dict[str, Any] = {
        **input_payload,
        "qualification_id": canonical_sha256(input_payload),
        "result": result_payload,
        "result_sha256": canonical_sha256(result_payload),
    }
    manifest["manifest_sha256"] = canonical_sha256(manifest)
    verify_qualification_manifest(manifest)
    return manifest


def verify_qualification_manifest(manifest: Mapping[str, Any]) -> None:
    required = {
        "schema_version",
        "strategy",
        "dataset",
        "qualification_contract",
        "qualification_id",
        "result",
        "result_sha256",
        "manifest_sha256",
    }
    if set(manifest) != required:
        raise QualificationManifestError("manifest keys do not match schema")
    if manifest["schema_version"] != MANIFEST_SCHEMA:
        raise QualificationManifestError("unsupported qualification manifest schema")
    input_payload = {
        "schema_version": manifest["schema_version"],
        "strategy": manifest["strategy"],
        "dataset": manifest["dataset"],
        "qualification_contract": manifest["qualification_contract"],
    }
    if manifest["qualification_id"] != canonical_sha256(input_payload):
        raise QualificationManifestError("qualification_id mismatch")
    result = manifest["result"]
    if not isinstance(result, Mapping):
        raise QualificationManifestError("result must be an object")
    if manifest["result_sha256"] != canonical_sha256(result):
        raise QualificationManifestError("result_sha256 mismatch")
    dataset = manifest["dataset"]
    if not isinstance(dataset, Mapping):
        raise QualificationManifestError("dataset must be an object")
    if result.get("dataset_id") != dataset.get("dataset_id"):
        raise QualificationManifestError("result dataset_id mismatch")
    if result.get("dataset_sha256") != dataset.get("canonical_sha256"):
        raise QualificationManifestError("result dataset_sha256 mismatch")
    without_manifest_hash = dict(manifest)
    supplied = without_manifest_hash.pop("manifest_sha256")
    if supplied != canonical_sha256(without_manifest_hash):
        raise QualificationManifestError("manifest_sha256 mismatch")


def write_qualification_manifest(path: str | Path, manifest: Mapping[str, Any]) -> None:
    verify_qualification_manifest(manifest)
    Path(path).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_qualification_manifest(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise QualificationManifestError("manifest root must be an object")
    verify_qualification_manifest(payload)
    return payload
