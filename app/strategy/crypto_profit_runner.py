from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.strategy.crypto_perp import CryptoPerpStrategyConfig, CryptoSide, CryptoTradePlan

_BPS = Decimal("10000")
_ONE = Decimal("1")


@dataclass(frozen=True)
class CryptoProfitRunnerPolicy:
    """Uncapped profit-runner objective after a cost-aware >=$20 entry admission.

    The activation and protected amounts are modeled net-USDT objectives under the configured
    fee/slippage assumptions. They are not guaranteed realized PnL because gaps, latency and
    adverse execution can cross a protective trigger.
    """

    activation_net_profit_usd: Decimal = Decimal("20")
    protected_net_profit_usd: Decimal = Decimal("15")

    def validate(self) -> None:
        if self.activation_net_profit_usd <= 0:
            raise ValueError("runner activation net profit must be positive")
        if self.protected_net_profit_usd <= 0:
            raise ValueError("runner protected net profit must be positive")
        if self.protected_net_profit_usd >= self.activation_net_profit_usd:
            raise ValueError("runner protected net profit must be below activation")


@dataclass(frozen=True)
class CryptoProfitRunnerLevels:
    activation_price: Decimal
    protected_price_at_activation: Decimal
    trailing_distance: Decimal
    activation_net_profit_usd: Decimal
    protected_net_profit_usd: Decimal
    profit_cap_net_profit_usd: Decimal | None = None

    def validate(self, *, side: CryptoSide, entry_price: Decimal) -> None:
        if entry_price <= 0:
            raise ValueError("runner entry price must be positive")
        if self.trailing_distance <= 0:
            raise ValueError("runner trailing distance must be positive")
        if side is CryptoSide.LONG:
            if not entry_price < self.protected_price_at_activation < self.activation_price:
                raise ValueError("long runner prices must be entry < protected < activation")
        elif not self.activation_price < self.protected_price_at_activation < entry_price:
            raise ValueError("short runner prices must be activation < protected < entry")
        if self.profit_cap_net_profit_usd is not None:
            raise ValueError("crypto profit runner must remain uncapped")


def modeled_raw_trigger_for_net_profit(
    *,
    side: CryptoSide,
    actual_average_entry_price: Decimal,
    actual_filled_quantity: Decimal,
    desired_net_profit_usd: Decimal,
    strategy_config: CryptoPerpStrategyConfig,
) -> Decimal:
    """Return the raw market trigger expected to net the desired amount after modeled costs.

    The average entry price is an already-executed fill, so entry slippage is not charged a
    second time. Entry and exit taker fees plus expected adverse exit slippage are included.
    """

    strategy_config.validate()
    if actual_average_entry_price <= 0:
        raise ValueError("runner actual average entry price must be positive")
    if actual_filled_quantity <= 0:
        raise ValueError("runner actual filled quantity must be positive")
    if desired_net_profit_usd < 0:
        raise ValueError("runner desired net profit cannot be negative")

    fee = strategy_config.taker_fee_rate
    slippage = strategy_config.slippage_bps_per_fill / _BPS
    quantity = actual_filled_quantity
    entry = actual_average_entry_price
    entry_fee = entry * quantity * fee

    if side is CryptoSide.LONG:
        exit_execution = (
            desired_net_profit_usd + quantity * entry + entry_fee
        ) / (quantity * (_ONE - fee))
        raw_trigger = exit_execution / (_ONE - slippage)
    else:
        exit_execution = (
            quantity * entry - entry_fee - desired_net_profit_usd
        ) / (quantity * (_ONE + fee))
        if exit_execution <= 0:
            raise ValueError("short runner objective would require non-positive exit price")
        raw_trigger = exit_execution / (_ONE + slippage)

    if raw_trigger <= 0:
        raise ValueError("runner raw trigger must be positive")
    return raw_trigger


def build_crypto_profit_runner_levels(
    trade_plan: CryptoTradePlan,
    *,
    actual_average_entry_price: Decimal,
    actual_filled_quantity: Decimal,
    strategy_config: CryptoPerpStrategyConfig,
    policy: CryptoProfitRunnerPolicy | None = None,
) -> CryptoProfitRunnerLevels:
    """Build an activation + giveback distance with no fixed upside target."""

    active_policy = CryptoProfitRunnerPolicy() if policy is None else policy
    active_policy.validate()
    if trade_plan.target_net_profit_usd < active_policy.activation_net_profit_usd:
        raise ValueError("runner requires a trade plan admitted for at least the activation net")

    activation = modeled_raw_trigger_for_net_profit(
        side=trade_plan.side,
        actual_average_entry_price=actual_average_entry_price,
        actual_filled_quantity=actual_filled_quantity,
        desired_net_profit_usd=active_policy.activation_net_profit_usd,
        strategy_config=strategy_config,
    )
    protected = modeled_raw_trigger_for_net_profit(
        side=trade_plan.side,
        actual_average_entry_price=actual_average_entry_price,
        actual_filled_quantity=actual_filled_quantity,
        desired_net_profit_usd=active_policy.protected_net_profit_usd,
        strategy_config=strategy_config,
    )
    levels = CryptoProfitRunnerLevels(
        activation_price=activation,
        protected_price_at_activation=protected,
        trailing_distance=abs(activation - protected),
        activation_net_profit_usd=active_policy.activation_net_profit_usd,
        protected_net_profit_usd=active_policy.protected_net_profit_usd,
        profit_cap_net_profit_usd=None,
    )
    levels.validate(side=trade_plan.side, entry_price=actual_average_entry_price)
    return levels
