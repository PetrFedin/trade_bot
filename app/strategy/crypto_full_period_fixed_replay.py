from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.marketdata.bybit_research_universe import BybitResearchInstrument
from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_historical_diagnostics import diagnose_crypto_historical_conditions
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_profit_runner import CryptoProfitRunnerPolicy
from app.strategy.crypto_runner_admission import CryptoRunnerAdmissionPolicy
from app.strategy.crypto_trade_management import CryptoProtectionPolicy
from tools.replay_bybit_crypto_single_symbol import (
    replay_open_ended_crypto_runner_single_symbol,
)

_INTERVAL = timedelta(minutes=5)
_INTERVAL_MS = 5 * 60 * 1000
_QUALIFIED_RUNNER_EDGE_MULTIPLE = Decimal("1.50")
_CONTRACT_VERSION = "BYBIT_FIXED_STRATEGY_FULL_PERIOD_PRICE_REPLAY_V1"


@dataclass(frozen=True)
class CryptoFullPeriodPriceGridCoverage:
    symbol: str
    launch_time: str
    first_expected_bar_at: str
    last_archive_date: str
    expected_bar_count: int
    actual_bar_count: int
    missing_bar_count: int
    extra_bar_count: int
    first_missing_bar_at: str | None
    first_extra_bar_at: str | None

    def validate(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("full-period replay coverage symbol is invalid")
        launch = _parse_time(self.launch_time)
        first = _parse_time(self.first_expected_bar_at)
        if first > launch or launch - first >= _INTERVAL:
            raise ValueError("full-period replay first expected bar is not launch bucket")
        last_archive = date.fromisoformat(self.last_archive_date)
        if last_archive < launch.date():
            raise ValueError("full-period replay archive cutoff precedes launch")
        if self.expected_bar_count <= 0:
            raise ValueError("full-period replay expected bar count must be positive")
        if self.actual_bar_count < 0:
            raise ValueError("full-period replay actual bar count cannot be negative")
        if self.missing_bar_count < 0 or self.extra_bar_count < 0:
            raise ValueError("full-period replay grid gap counts cannot be negative")
        if self.missing_bar_count == 0 and self.first_missing_bar_at is not None:
            raise ValueError("full-period replay missing-bar timestamp is inconsistent")
        if self.missing_bar_count > 0 and self.first_missing_bar_at is None:
            raise ValueError("full-period replay missing-bar timestamp is required")
        if self.extra_bar_count == 0 and self.first_extra_bar_at is not None:
            raise ValueError("full-period replay extra-bar timestamp is inconsistent")
        if self.extra_bar_count > 0 and self.first_extra_bar_at is None:
            raise ValueError("full-period replay extra-bar timestamp is required")
        if self.first_missing_bar_at is not None:
            _parse_time(self.first_missing_bar_at)
        if self.first_extra_bar_at is not None:
            _parse_time(self.first_extra_bar_at)

    @property
    def full_period_price_grid_complete(self) -> bool:
        return self.missing_bar_count == 0 and self.extra_bar_count == 0

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "symbol": self.symbol,
            "launch_time": self.launch_time,
            "first_expected_bar_at": self.first_expected_bar_at,
            "last_archive_date": self.last_archive_date,
            "expected_bar_count": self.expected_bar_count,
            "actual_bar_count": self.actual_bar_count,
            "missing_bar_count": self.missing_bar_count,
            "extra_bar_count": self.extra_bar_count,
            "first_missing_bar_at": self.first_missing_bar_at,
            "first_extra_bar_at": self.first_extra_bar_at,
            "full_period_price_grid_complete": self.full_period_price_grid_complete,
        }


def audit_full_period_5m_price_grid(
    instrument: BybitResearchInstrument,
    bars: Sequence[BybitKlineBar],
    *,
    last_archive_date: date,
) -> CryptoFullPeriodPriceGridCoverage:
    if instrument.symbol != instrument.symbol.strip().upper():
        raise ValueError("full-period replay instrument symbol is invalid")
    launch = datetime.fromtimestamp(instrument.launch_time_ms / 1000, tz=UTC)
    first_expected = _floor_five_minutes(launch)
    end_exclusive = datetime.combine(
        last_archive_date + timedelta(days=1),
        datetime.min.time(),
        tzinfo=UTC,
    )
    if end_exclusive <= first_expected:
        raise ValueError("full-period replay price grid interval is empty")
    ordered = tuple(sorted(bars, key=lambda item: item.start_time))
    if tuple(bars) != ordered:
        raise ValueError("full-period replay bars must be chronological")
    actual_times: list[datetime] = []
    seen: set[datetime] = set()
    for bar in ordered:
        bar.validate()
        if bar.symbol != instrument.symbol:
            raise ValueError("full-period replay bars contain another symbol")
        moment = bar.start_time.astimezone(UTC)
        if moment.second or moment.microsecond or moment.minute % 5:
            raise ValueError("full-period replay bar is not aligned to 5 minutes")
        if moment in seen:
            raise ValueError("full-period replay bars contain duplicate timestamps")
        seen.add(moment)
        actual_times.append(moment)

    expected_count = int((end_exclusive - first_expected) / _INTERVAL)
    expected_times = {
        first_expected + index * _INTERVAL for index in range(expected_count)
    }
    actual_set = set(actual_times)
    missing = sorted(expected_times - actual_set)
    extra = sorted(actual_set - expected_times)
    coverage = CryptoFullPeriodPriceGridCoverage(
        symbol=instrument.symbol,
        launch_time=launch.isoformat(),
        first_expected_bar_at=first_expected.isoformat(),
        last_archive_date=last_archive_date.isoformat(),
        expected_bar_count=expected_count,
        actual_bar_count=len(actual_times),
        missing_bar_count=len(missing),
        extra_bar_count=len(extra),
        first_missing_bar_at=None if not missing else missing[0].isoformat(),
        first_extra_bar_at=None if not extra else extra[0].isoformat(),
    )
    coverage.validate()
    return coverage


def qualified_fixed_strategy_contract_fingerprint() -> str:
    payload = {
        "contract_version": _CONTRACT_VERSION,
        "strategy_config": _json_safe(asdict(CryptoPerpStrategyConfig())),
        "protection_policy": _json_safe(asdict(CryptoProtectionPolicy())),
        "runner_policy": _json_safe(asdict(CryptoProfitRunnerPolicy())),
        "runner_admission_policy": _json_safe(
            asdict(
                CryptoRunnerAdmissionPolicy(
                    minimum_expected_edge_multiple=_QUALIFIED_RUNNER_EDGE_MULTIPLE
                )
            )
        ),
        "interval": "5",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()


def run_qualified_full_period_symbol_replay(
    instrument: BybitResearchInstrument,
    bars: Sequence[BybitKlineBar],
    *,
    last_archive_date: date,
    opening_equity_usdt: Decimal = Decimal("1000"),
    strategy_config: CryptoPerpStrategyConfig | None = None,
) -> dict[str, Any]:
    if not opening_equity_usdt.is_finite() or opening_equity_usdt <= 0:
        raise ValueError("full-period replay opening equity must be positive and finite")
    active_config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    active_config.validate()
    if active_config != CryptoPerpStrategyConfig():
        raise ValueError("full-period replay requires the qualified fixed strategy config")
    coverage = audit_full_period_5m_price_grid(
        instrument,
        bars,
        last_archive_date=last_archive_date,
    )
    if not coverage.full_period_price_grid_complete:
        raise ValueError(
            "full-period replay refused incomplete 5m price grid:"
            f"{instrument.symbol}:missing={coverage.missing_bar_count}:"
            f"extra={coverage.extra_bar_count}"
        )
    acquisition = BybitKlineAcquisition(
        bars=tuple(bars),
        pages_by_symbol={instrument.symbol: 1},
    )
    acquisition.validate(requested_symbols=(instrument.symbol,), minimum_bars=60)
    replay = replay_open_ended_crypto_runner_single_symbol(
        acquisition,
        opening_equity_usdt=opening_equity_usdt,
        base_config=active_config,
        protection_policy=CryptoProtectionPolicy(),
        runner_policy=CryptoProfitRunnerPolicy(),
        runner_admission_policy=CryptoRunnerAdmissionPolicy(
            minimum_expected_edge_multiple=_QUALIFIED_RUNNER_EDGE_MULTIPLE
        ),
        interval="5",
    )
    if replay.get("strategy_promotion_allowed") is not False:
        raise ValueError("full-period fixed replay unexpectedly enabled strategy promotion")
    if replay.get("bybit_demo_order_writes_enabled") is not False:
        raise ValueError("full-period fixed replay unexpectedly enabled demo order writes")
    if replay.get("bybit_live_order_routing_allowed") is not False:
        raise ValueError("full-period fixed replay unexpectedly enabled live routing")
    diagnostics = diagnose_crypto_historical_conditions(acquisition, replay)
    return {
        "diagnostic": "BYBIT_FULL_PERIOD_FIXED_STRATEGY_PRICE_REPLAY",
        "symbol": instrument.symbol,
        "coverage": coverage.to_payload(),
        "strategy_contract_fingerprint": qualified_fixed_strategy_contract_fingerprint(),
        "replay": replay,
        "historical_conditions": diagnostics,
        "price_history_full_period": True,
        "derivatives_history_full_period": False,
        "full_period_evidence_matrix_allowed": False,
        "reason_full_evidence_matrix_blocked": (
            "FULL_PERIOD_DERIVATIVES_COVERAGE_NOT_VALIDATED"
        ),
        "strategy_parameters_changed": False,
        "parameter_retuning_performed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _floor_five_minutes(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("full-period replay timestamp must be timezone-aware")
    utc = value.astimezone(UTC)
    epoch_ms = int(utc.timestamp() * 1000)
    floored_ms = (epoch_ms // _INTERVAL_MS) * _INTERVAL_MS
    return datetime.fromtimestamp(floored_ms / 1000, tz=UTC)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("full-period replay timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value
