from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.strategy.crypto_perp import CryptoTradePlan
from app.strategy.crypto_profit_runner import CryptoProfitRunnerPolicy


@dataclass(frozen=True)
class CryptoRunnerAdmissionPolicy:
    """Require material upside beyond the $20 activation before giving up fixed $20 TP.

    The threshold is intentionally expressed as a multiple of the runner activation amount so
    the decision stays cost-aware through ``CryptoTradePlan.expected_net_edge_usd``. This gate
    does not claim that the extra edge will be realized; it only decides whether an uncapped
    runner is worth researching instead of the fixed target.
    """

    minimum_expected_edge_multiple: Decimal = Decimal("1.50")

    def validate(self) -> None:
        if self.minimum_expected_edge_multiple <= Decimal("1"):
            raise ValueError("runner excess-edge multiple must be greater than 1")


@dataclass(frozen=True)
class CryptoRunnerAdmissionDecision:
    eligible: bool
    expected_net_edge_usd: Decimal
    required_expected_net_edge_usd: Decimal
    reasons: tuple[str, ...]
    strategy_promotion_allowed: bool = False
    live_activation_allowed: bool = False


def evaluate_crypto_runner_admission(
    trade_plan: CryptoTradePlan,
    *,
    runner_policy: CryptoProfitRunnerPolicy | None = None,
    admission_policy: CryptoRunnerAdmissionPolicy | None = None,
) -> CryptoRunnerAdmissionDecision:
    active_runner = CryptoProfitRunnerPolicy() if runner_policy is None else runner_policy
    active_admission = (
        CryptoRunnerAdmissionPolicy() if admission_policy is None else admission_policy
    )
    active_runner.validate()
    active_admission.validate()

    required = (
        active_runner.activation_net_profit_usd
        * active_admission.minimum_expected_edge_multiple
    )
    reasons: list[str] = []
    if trade_plan.target_net_profit_usd < active_runner.activation_net_profit_usd:
        reasons.append("RUNNER_REQUIRES_MINIMUM_20_USD_ENTRY_EDGE")
    if trade_plan.expected_net_edge_usd < required:
        reasons.append("RUNNER_EXCESS_EXPECTED_EDGE_TOO_THIN")

    return CryptoRunnerAdmissionDecision(
        eligible=not reasons,
        expected_net_edge_usd=trade_plan.expected_net_edge_usd,
        required_expected_net_edge_usd=required,
        reasons=tuple(reasons),
    )
