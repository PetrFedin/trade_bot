import json
from decimal import Decimal
from pathlib import Path

from app.strategy.entry_quality import EntryQualityPolicy

CONFIG = Path("research/entry_quality_filter_shadow_v1.json")


def test_entry_quality_shadow_config_is_fail_closed_for_promotion() -> None:
    document = json.loads(CONFIG.read_text(encoding="utf-8"))

    assert document["schema"] == "entry-quality-filter-shadow-v1"
    assert document["shadow_only"] is True
    assert document["strategy_promotion_allowed"] is False
    assert "NO_ENTRY_QUALITY_WALK_FORWARD_EVIDENCE" in document["promotion_blockers"]
    assert "NO_REAL_PAPER_ENTRY_QUALITY_EVIDENCE" in document["promotion_blockers"]
    assert "NO_PROFITABILITY_CLAIM" in document["promotion_blockers"]

    policy_document = document["policy"]
    policy = EntryQualityPolicy(
        lookback_bars=int(policy_document["lookback_bars"]),
        minimum_trend_efficiency=Decimal(
            policy_document["minimum_trend_efficiency"]
        ),
        maximum_price_extension_fraction=Decimal(
            policy_document["maximum_price_extension_fraction"]
        ),
        maximum_single_bar_return_fraction=Decimal(
            policy_document["maximum_single_bar_return_fraction"]
        ),
        minimum_average_dollar_volume=(
            None
            if policy_document["minimum_average_dollar_volume"] is None
            else Decimal(policy_document["minimum_average_dollar_volume"])
        ),
    )
    policy.validate()
