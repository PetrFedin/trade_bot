from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.runtime.bybit_mainnet_readonly_probe import BybitMainnetReadOnlySnapshot
from app.strategy.crypto_live_evidence_ranking import CryptoLiveOpportunitySnapshot

_ZERO = Decimal("0")
_HEX = frozenset("0123456789abcdef")
_EQUITY_SOURCE = "BYBIT_MAINNET_READONLY_AVAILABLE_CAPITAL_USD_EQUIVALENT"


@dataclass(frozen=True)
class CryptoReadOnlyPositionExposure:
    symbol: str
    long_position_value_usd: Decimal
    short_position_value_usd: Decimal
    gross_position_value_usd: Decimal
    net_position_value_usd: Decimal
    position_count: int
    exposure_complete: bool
    missing_reasons: tuple[str, ...]

    def validate(self) -> None:
        _validate_symbol(self.symbol)
        for name, value in (
            ("long_position_value_usd", self.long_position_value_usd),
            ("short_position_value_usd", self.short_position_value_usd),
            ("gross_position_value_usd", self.gross_position_value_usd),
            ("net_position_value_usd", self.net_position_value_usd),
        ):
            if not value.is_finite():
                raise ValueError(f"read-only position exposure {name} must be finite")
        if self.long_position_value_usd < 0 or self.short_position_value_usd < 0:
            raise ValueError("read-only position exposure directional values cannot be negative")
        if self.gross_position_value_usd != (
            self.long_position_value_usd + self.short_position_value_usd
        ):
            raise ValueError("read-only position exposure gross value is inconsistent")
        if self.net_position_value_usd != (
            self.long_position_value_usd - self.short_position_value_usd
        ):
            raise ValueError("read-only position exposure net value is inconsistent")
        if self.position_count <= 0:
            raise ValueError("read-only position exposure count must be positive")
        if self.exposure_complete and self.missing_reasons:
            raise ValueError("complete read-only position exposure cannot have missing reasons")
        if not self.exposure_complete and not self.missing_reasons:
            raise ValueError("incomplete read-only position exposure requires missing reasons")

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "symbol": self.symbol,
            "long_position_value_usd": str(self.long_position_value_usd),
            "short_position_value_usd": str(self.short_position_value_usd),
            "gross_position_value_usd": str(self.gross_position_value_usd),
            "net_position_value_usd": str(self.net_position_value_usd),
            "position_count": self.position_count,
            "exposure_complete": self.exposure_complete,
            "missing_reasons": list(self.missing_reasons),
        }


@dataclass(frozen=True)
class CryptoReadOnlyAccountContext:
    observed_at: str
    api_host: str
    api_key_fingerprint_sha256: str
    read_only_verified: bool
    ip_binding_verified: bool
    margin_mode: str
    total_equity_usd: Decimal
    total_wallet_balance_usd: Decimal
    total_margin_balance_usd: Decimal
    total_available_balance_usd: Decimal
    total_perp_upl_usd: Decimal
    total_initial_margin_usd: Decimal
    total_maintenance_margin_usd: Decimal
    sizing_capital_usd_equivalent: Decimal | None
    gross_position_value_usd: Decimal
    long_position_value_usd: Decimal
    short_position_value_usd: Decimal
    net_position_value_usd: Decimal
    open_position_count: int
    position_exposure_complete: bool
    position_exposure_missing_reasons: tuple[str, ...]
    positions: tuple[CryptoReadOnlyPositionExposure, ...]
    equity_source: str = _EQUITY_SOURCE
    operator_review_required: bool = True
    trade_actionable: bool = False
    live_mainnet_order_routing_allowed: bool = False
    order_writes_supported: bool = False

    def validate(self) -> None:
        _parse_time(self.observed_at)
        if not self.api_host or self.api_host != self.api_host.strip().lower():
            raise ValueError("read-only account context API host is invalid")
        if len(self.api_key_fingerprint_sha256) != 64 or any(
            char not in _HEX for char in self.api_key_fingerprint_sha256
        ):
            raise ValueError("read-only account context key fingerprint is invalid")
        if not self.read_only_verified or not self.ip_binding_verified:
            raise ValueError("read-only account context requires verified key and IP binding")
        if not self.margin_mode:
            raise ValueError("read-only account context margin mode is required")
        for name, value in (
            ("total_equity_usd", self.total_equity_usd),
            ("total_wallet_balance_usd", self.total_wallet_balance_usd),
            ("total_margin_balance_usd", self.total_margin_balance_usd),
            ("total_available_balance_usd", self.total_available_balance_usd),
            ("total_perp_upl_usd", self.total_perp_upl_usd),
            ("total_initial_margin_usd", self.total_initial_margin_usd),
            ("total_maintenance_margin_usd", self.total_maintenance_margin_usd),
            ("gross_position_value_usd", self.gross_position_value_usd),
            ("long_position_value_usd", self.long_position_value_usd),
            ("short_position_value_usd", self.short_position_value_usd),
            ("net_position_value_usd", self.net_position_value_usd),
        ):
            if not value.is_finite():
                raise ValueError(f"read-only account context {name} must be finite")
        if self.total_equity_usd < 0:
            raise ValueError("read-only account context total equity cannot be negative")
        if self.total_initial_margin_usd < 0 or self.total_maintenance_margin_usd < 0:
            raise ValueError("read-only account context margin requirements cannot be negative")
        if self.long_position_value_usd < 0 or self.short_position_value_usd < 0:
            raise ValueError("read-only account context directional exposure cannot be negative")
        if self.gross_position_value_usd != (
            self.long_position_value_usd + self.short_position_value_usd
        ):
            raise ValueError("read-only account context gross exposure is inconsistent")
        if self.net_position_value_usd != (
            self.long_position_value_usd - self.short_position_value_usd
        ):
            raise ValueError("read-only account context net exposure is inconsistent")
        if self.open_position_count < 0:
            raise ValueError("read-only account context position count cannot be negative")
        if self.sizing_capital_usd_equivalent is not None:
            if (
                not self.sizing_capital_usd_equivalent.is_finite()
                or self.sizing_capital_usd_equivalent <= 0
            ):
                raise ValueError("read-only account sizing capital must be positive and finite")
            if self.sizing_capital_usd_equivalent > self.total_equity_usd:
                raise ValueError("read-only account sizing capital cannot exceed total equity")
            if self.sizing_capital_usd_equivalent > self.total_available_balance_usd:
                raise ValueError("read-only account sizing capital cannot exceed available balance")
        if self.position_exposure_complete and self.position_exposure_missing_reasons:
            raise ValueError("complete account exposure cannot carry missing reasons")
        if not self.position_exposure_complete and not self.position_exposure_missing_reasons:
            raise ValueError("incomplete account exposure requires missing reasons")
        if tuple(sorted(item.symbol for item in self.positions)) != tuple(
            item.symbol for item in self.positions
        ):
            raise ValueError("read-only account position exposures must be sorted by symbol")
        if len({item.symbol for item in self.positions}) != len(self.positions):
            raise ValueError("read-only account position exposures cannot duplicate symbols")
        if sum(item.position_count for item in self.positions) != self.open_position_count:
            raise ValueError("read-only account position count is inconsistent")
        for item in self.positions:
            item.validate()
        if self.equity_source != _EQUITY_SOURCE:
            raise ValueError("read-only account context equity source is invalid")
        if (
            not self.operator_review_required
            or self.trade_actionable
            or self.live_mainnet_order_routing_allowed
            or self.order_writes_supported
        ):
            raise ValueError("read-only account context cannot activate trading")

    @property
    def initial_margin_to_equity(self) -> Decimal | None:
        if self.total_equity_usd <= 0:
            return None
        return self.total_initial_margin_usd / self.total_equity_usd

    @property
    def maintenance_margin_to_equity(self) -> Decimal | None:
        if self.total_equity_usd <= 0:
            return None
        return self.total_maintenance_margin_usd / self.total_equity_usd

    @property
    def available_balance_to_equity(self) -> Decimal | None:
        if self.total_equity_usd <= 0:
            return None
        return self.total_available_balance_usd / self.total_equity_usd

    @property
    def gross_position_value_to_equity(self) -> Decimal | None:
        if self.total_equity_usd <= 0 or not self.position_exposure_complete:
            return None
        return self.gross_position_value_usd / self.total_equity_usd

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "schema": "BYBIT_MAINNET_READONLY_RANKING_CONTEXT_V115",
            "observed_at": self.observed_at,
            "api_host": self.api_host,
            "credential_safety": {
                "api_key_fingerprint_sha256": self.api_key_fingerprint_sha256,
                "read_only_verified": self.read_only_verified,
                "ip_binding_verified": self.ip_binding_verified,
            },
            "margin_mode": self.margin_mode,
            "total_equity_usd": str(self.total_equity_usd),
            "total_wallet_balance_usd": str(self.total_wallet_balance_usd),
            "total_margin_balance_usd": str(self.total_margin_balance_usd),
            "total_available_balance_usd": str(self.total_available_balance_usd),
            "total_perp_upl_usd": str(self.total_perp_upl_usd),
            "total_initial_margin_usd": str(self.total_initial_margin_usd),
            "total_maintenance_margin_usd": str(self.total_maintenance_margin_usd),
            "sizing_capital_usd_equivalent": _decimal_text(
                self.sizing_capital_usd_equivalent
            ),
            "equity_source": self.equity_source,
            "initial_margin_to_equity": _decimal_text(self.initial_margin_to_equity),
            "maintenance_margin_to_equity": _decimal_text(
                self.maintenance_margin_to_equity
            ),
            "available_balance_to_equity": _decimal_text(
                self.available_balance_to_equity
            ),
            "gross_position_value_usd": str(self.gross_position_value_usd),
            "long_position_value_usd": str(self.long_position_value_usd),
            "short_position_value_usd": str(self.short_position_value_usd),
            "net_position_value_usd": str(self.net_position_value_usd),
            "gross_position_value_to_equity": _decimal_text(
                self.gross_position_value_to_equity
            ),
            "open_position_count": self.open_position_count,
            "position_exposure_complete": self.position_exposure_complete,
            "position_exposure_missing_reasons": list(
                self.position_exposure_missing_reasons
            ),
            "positions": [item.to_payload() for item in self.positions],
            "operator_review_required": self.operator_review_required,
            "trade_actionable": self.trade_actionable,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
            "order_writes_supported": self.order_writes_supported,
        }


@dataclass(frozen=True)
class CryptoReadOnlyCandidateAccountOverlay:
    evidence_rank: int
    symbol: str
    signal_side: str | None
    existing_long_position_value_usd: Decimal
    existing_short_position_value_usd: Decimal
    existing_gross_position_value_usd: Decimal
    existing_net_position_value_usd: Decimal
    existing_position_relation: str
    planned_notional_usdt: Decimal | None
    planned_notional_to_sizing_capital: Decimal | None
    gross_plus_planned_upper_bound_usd: Decimal | None
    account_exposure_complete: bool
    operator_review_required: bool = True
    trade_actionable: bool = False
    live_mainnet_order_routing_allowed: bool = False

    def validate(self) -> None:
        if not 1 <= self.evidence_rank <= 50:
            raise ValueError("read-only account overlay evidence rank must be within [1, 50]")
        _validate_symbol(self.symbol)
        if self.signal_side is not None and self.signal_side not in {"LONG", "SHORT"}:
            raise ValueError("read-only account overlay signal side is invalid")
        for name, value in (
            ("existing_long_position_value_usd", self.existing_long_position_value_usd),
            ("existing_short_position_value_usd", self.existing_short_position_value_usd),
            ("existing_gross_position_value_usd", self.existing_gross_position_value_usd),
            ("existing_net_position_value_usd", self.existing_net_position_value_usd),
        ):
            if not value.is_finite():
                raise ValueError(f"read-only account overlay {name} must be finite")
        if self.existing_long_position_value_usd < 0 or self.existing_short_position_value_usd < 0:
            raise ValueError("read-only account overlay directional exposure cannot be negative")
        if self.existing_gross_position_value_usd != (
            self.existing_long_position_value_usd + self.existing_short_position_value_usd
        ):
            raise ValueError("read-only account overlay gross exposure is inconsistent")
        if self.existing_net_position_value_usd != (
            self.existing_long_position_value_usd - self.existing_short_position_value_usd
        ):
            raise ValueError("read-only account overlay net exposure is inconsistent")
        allowed_relations = {
            "NO_SIGNAL",
            "NO_EXISTING_POSITION",
            "SAME_DIRECTION_EXISTING_POSITION",
            "OPPOSING_EXISTING_POSITION",
            "EXISTING_HEDGED_POSITION",
        }
        if self.existing_position_relation not in allowed_relations:
            raise ValueError("read-only account overlay relation is invalid")
        for name, value in (
            ("planned_notional_usdt", self.planned_notional_usdt),
            ("planned_notional_to_sizing_capital", self.planned_notional_to_sizing_capital),
            ("gross_plus_planned_upper_bound_usd", self.gross_plus_planned_upper_bound_usd),
        ):
            if value is not None and not value.is_finite():
                raise ValueError(f"read-only account overlay {name} must be finite")
        if (
            not self.operator_review_required
            or self.trade_actionable
            or self.live_mainnet_order_routing_allowed
        ):
            raise ValueError("read-only account overlay cannot activate trading")

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "evidence_rank": self.evidence_rank,
            "symbol": self.symbol,
            "signal_side": self.signal_side,
            "existing_long_position_value_usd": str(
                self.existing_long_position_value_usd
            ),
            "existing_short_position_value_usd": str(
                self.existing_short_position_value_usd
            ),
            "existing_gross_position_value_usd": str(
                self.existing_gross_position_value_usd
            ),
            "existing_net_position_value_usd": str(
                self.existing_net_position_value_usd
            ),
            "existing_position_relation": self.existing_position_relation,
            "planned_notional_usdt": _decimal_text(self.planned_notional_usdt),
            "planned_notional_to_sizing_capital": _decimal_text(
                self.planned_notional_to_sizing_capital
            ),
            "gross_plus_planned_upper_bound_usd": _decimal_text(
                self.gross_plus_planned_upper_bound_usd
            ),
            "account_exposure_complete": self.account_exposure_complete,
            "operator_review_required": self.operator_review_required,
            "trade_actionable": self.trade_actionable,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
        }


@dataclass(frozen=True)
class CryptoReadOnlyAccountAwareRegistrySnapshot:
    observed_at: str
    ranking_snapshot_id: str
    account: CryptoReadOnlyAccountContext
    candidate_overlays: tuple[CryptoReadOnlyCandidateAccountOverlay, ...]
    ranking_order_changed: bool = False
    operator_review_required: bool = True
    trade_actionable: bool = False
    live_mainnet_order_routing_allowed: bool = False
    order_writes_supported: bool = False

    def validate(self) -> None:
        _parse_time(self.observed_at)
        if len(self.ranking_snapshot_id) != 64 or any(
            char not in _HEX for char in self.ranking_snapshot_id
        ):
            raise ValueError("read-only account-aware ranking snapshot id is invalid")
        self.account.validate()
        for expected_rank, overlay in enumerate(self.candidate_overlays, start=1):
            overlay.validate()
            if overlay.evidence_rank != expected_rank:
                raise ValueError("read-only account overlays must follow evidence rank order")
        if (
            self.ranking_order_changed
            or not self.operator_review_required
            or self.trade_actionable
            or self.live_mainnet_order_routing_allowed
            or self.order_writes_supported
        ):
            raise ValueError("read-only account-aware registry cannot activate trading")

    @property
    def snapshot_id(self) -> str:
        canonical = json.dumps(
            self.to_payload(include_snapshot_id=False),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def to_payload(self, *, include_snapshot_id: bool = True) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "schema": "BYBIT_MAINNET_READONLY_ACCOUNT_AWARE_REGISTRY_V115",
            "observed_at": self.observed_at,
            "ranking_snapshot_id": self.ranking_snapshot_id,
            "account": self.account.to_payload(),
            "candidate_overlays": [item.to_payload() for item in self.candidate_overlays],
            "ranking_order_changed": self.ranking_order_changed,
            "operator_review_required": self.operator_review_required,
            "trade_actionable": self.trade_actionable,
            "live_mainnet_order_routing_allowed": self.live_mainnet_order_routing_allowed,
            "order_writes_supported": self.order_writes_supported,
        }
        if include_snapshot_id:
            payload["snapshot_id"] = self.snapshot_id
        return payload


def build_crypto_readonly_account_context(
    snapshot: BybitMainnetReadOnlySnapshot,
    *,
    observed_at: datetime,
) -> CryptoReadOnlyAccountContext:
    snapshot.validate()
    observed = _utc(observed_at)
    by_symbol: dict[str, dict[str, Any]] = {}
    account_missing: list[str] = []
    for position in snapshot.positions:
        position.validate()
        row = by_symbol.setdefault(
            position.symbol,
            {
                "long": _ZERO,
                "short": _ZERO,
                "count": 0,
                "complete": True,
                "reasons": [],
            },
        )
        row["count"] += 1
        value = position.position_value
        if value is None:
            row["complete"] = False
            reason = f"{position.symbol}:POSITION_VALUE_MISSING"
            row["reasons"].append(reason)
            account_missing.append(reason)
            continue
        if not value.is_finite() or value < 0:
            raise ValueError("read-only account position value must be non-negative and finite")
        if position.side == "Buy":
            row["long"] += value
        else:
            row["short"] += value

    exposures: list[CryptoReadOnlyPositionExposure] = []
    for symbol in sorted(by_symbol):
        row = by_symbol[symbol]
        exposure = CryptoReadOnlyPositionExposure(
            symbol=symbol,
            long_position_value_usd=row["long"],
            short_position_value_usd=row["short"],
            gross_position_value_usd=row["long"] + row["short"],
            net_position_value_usd=row["long"] - row["short"],
            position_count=row["count"],
            exposure_complete=row["complete"],
            missing_reasons=tuple(row["reasons"]),
        )
        exposure.validate()
        exposures.append(exposure)

    long_value = sum((item.long_position_value_usd for item in exposures), _ZERO)
    short_value = sum((item.short_position_value_usd for item in exposures), _ZERO)
    position_complete = not account_missing
    sizing_capital = _sizing_capital(snapshot)
    context = CryptoReadOnlyAccountContext(
        observed_at=observed.isoformat(),
        api_host=snapshot.api_host,
        api_key_fingerprint_sha256=snapshot.api_key.key_fingerprint_sha256,
        read_only_verified=snapshot.api_key.read_only,
        ip_binding_verified=bool(snapshot.api_key.ip_bindings),
        margin_mode=snapshot.account.margin_mode,
        total_equity_usd=snapshot.wallet.total_equity_usd,
        total_wallet_balance_usd=snapshot.wallet.total_wallet_balance_usd,
        total_margin_balance_usd=snapshot.wallet.total_margin_balance_usd,
        total_available_balance_usd=snapshot.wallet.total_available_balance_usd,
        total_perp_upl_usd=snapshot.wallet.total_perp_upl_usd,
        total_initial_margin_usd=snapshot.wallet.total_initial_margin_usd,
        total_maintenance_margin_usd=snapshot.wallet.total_maintenance_margin_usd,
        sizing_capital_usd_equivalent=sizing_capital,
        gross_position_value_usd=long_value + short_value,
        long_position_value_usd=long_value,
        short_position_value_usd=short_value,
        net_position_value_usd=long_value - short_value,
        open_position_count=len(snapshot.positions),
        position_exposure_complete=position_complete,
        position_exposure_missing_reasons=tuple(sorted(set(account_missing))),
        positions=tuple(exposures),
    )
    context.validate()
    return context


def build_crypto_account_aware_registry_snapshot(
    ranking: CryptoLiveOpportunitySnapshot,
    account: CryptoReadOnlyAccountContext,
    *,
    observed_at: datetime,
) -> CryptoReadOnlyAccountAwareRegistrySnapshot:
    ranking.validate()
    account.validate()
    observed = _utc(observed_at)
    sizing = account.sizing_capital_usd_equivalent
    if sizing is None:
        raise ValueError("read-only account has no positive available sizing capital")
    if ranking.equity_source != account.equity_source or ranking.equity_usdt != sizing:
        raise ValueError("live ranking was not sized from the supplied read-only account context")
    positions = {item.symbol: item for item in account.positions}
    overlays: list[CryptoReadOnlyCandidateAccountOverlay] = []
    for opportunity in ranking.opportunities:
        existing = positions.get(opportunity.symbol)
        long_value = _ZERO if existing is None else existing.long_position_value_usd
        short_value = _ZERO if existing is None else existing.short_position_value_usd
        gross = long_value + short_value
        planned = opportunity.planned_notional_usdt
        planned_ratio = None if planned is None else planned / sizing
        gross_upper = None if planned is None else account.gross_position_value_usd + planned
        overlay = CryptoReadOnlyCandidateAccountOverlay(
            evidence_rank=opportunity.evidence_rank,
            symbol=opportunity.symbol,
            signal_side=opportunity.signal_side,
            existing_long_position_value_usd=long_value,
            existing_short_position_value_usd=short_value,
            existing_gross_position_value_usd=gross,
            existing_net_position_value_usd=long_value - short_value,
            existing_position_relation=_position_relation(
                signal_side=opportunity.signal_side,
                long_value=long_value,
                short_value=short_value,
            ),
            planned_notional_usdt=planned,
            planned_notional_to_sizing_capital=planned_ratio,
            gross_plus_planned_upper_bound_usd=gross_upper,
            account_exposure_complete=(
                account.position_exposure_complete
                and (existing is None or existing.exposure_complete)
            ),
        )
        overlay.validate()
        overlays.append(overlay)
    result = CryptoReadOnlyAccountAwareRegistrySnapshot(
        observed_at=observed.isoformat(),
        ranking_snapshot_id=ranking.snapshot_id,
        account=account,
        candidate_overlays=tuple(overlays),
    )
    result.validate()
    return result


def _sizing_capital(snapshot: BybitMainnetReadOnlySnapshot) -> Decimal | None:
    equity = snapshot.wallet.total_equity_usd
    available = snapshot.wallet.total_available_balance_usd
    if not equity.is_finite() or not available.is_finite():
        raise ValueError("read-only account sizing inputs must be finite")
    if equity <= 0 or available <= 0:
        return None
    return min(equity, available)


def _position_relation(
    *,
    signal_side: str | None,
    long_value: Decimal,
    short_value: Decimal,
) -> str:
    if signal_side is None:
        return "NO_SIGNAL"
    if long_value == 0 and short_value == 0:
        return "NO_EXISTING_POSITION"
    if long_value > 0 and short_value > 0:
        return "EXISTING_HEDGED_POSITION"
    if signal_side == "LONG":
        return (
            "SAME_DIRECTION_EXISTING_POSITION"
            if long_value > 0
            else "OPPOSING_EXISTING_POSITION"
        )
    return (
        "SAME_DIRECTION_EXISTING_POSITION"
        if short_value > 0
        else "OPPOSING_EXISTING_POSITION"
    )


def _validate_symbol(symbol: str) -> None:
    if (
        not symbol
        or symbol != symbol.strip().upper()
        or not symbol.endswith("USDT")
        or not symbol.isalnum()
    ):
        raise ValueError("read-only account symbol must be normalized USDT")


def _parse_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("read-only account context timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
