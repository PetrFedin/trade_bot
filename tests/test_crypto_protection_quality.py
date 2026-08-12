from app.strategy.crypto_protection_quality import (
    add_protection_quality_to_report,
    evaluate_protection_quality,
)


def test_gap_through_losses_are_separated_from_non_gap_protection() -> None:
    trades = [
        {
            "exit_reason": "PROFIT_PROTECTION",
            "net_pnl_usdt": -40.0,
            "gap_through": True,
        },
        {
            "exit_reason": "PROFIT_PROTECTION",
            "net_pnl_usdt": 5.0,
            "gap_through": False,
        },
        {
            "exit_reason": "BREAK_EVEN_STOP",
            "net_pnl_usdt": 0.0,
            "gap_through": False,
        },
        {
            "exit_reason": "HARD_STOP",
            "net_pnl_usdt": -10.0,
            "gap_through": True,
        },
    ]

    quality = evaluate_protection_quality(trades)

    assert quality.protective_exit_count == 3
    assert quality.protective_net_pnl_usdt == -35
    assert quality.gap_through_count == 1
    assert quality.gap_through_net_pnl_usdt == -40
    assert quality.non_gap_count == 2
    assert quality.non_gap_net_pnl_usdt == 5
    assert quality.profitable_protective_exit_count == 1
    assert quality.losing_protective_exit_count == 1
    assert quality.gap_through_loss_count == 1
    assert quality.worst_gap_loss_usdt == -40
    assert quality.gap_through_share == 1 / 3
    assert quality.gap_loss_share_of_protective_losses == 1


def test_report_annotation_covers_baseline_and_shadow_candidate() -> None:
    report = {
        "variants": {
            "TARGET_15_USD": {
                "closed_trades": [
                    {
                        "normalized_exit_reason": "PROFIT_PROTECTION",
                        "net_pnl_usdt": -25,
                        "gap_through": True,
                    }
                ]
            }
        },
        "notional_cap_shadow_candidates": {
            "MAX_NOTIONAL_3X_EQUITY": {
                "variants": {
                    "TARGET_20_USD": {
                        "closed_trades": [
                            {
                                "exit_reason": "BREAK_EVEN_STOP",
                                "net_pnl_usdt": 0,
                                "gap_through": False,
                            }
                        ]
                    }
                }
            }
        },
    }

    annotated = add_protection_quality_to_report(report)

    assert annotated["variants"]["TARGET_15_USD"]["protection_quality"][
        "gap_through_count"
    ] == 1
    assert annotated["notional_cap_shadow_candidates"]["MAX_NOTIONAL_3X_EQUITY"][
        "variants"
    ]["TARGET_20_USD"]["protection_quality"]["gap_through_count"] == 0
    assert annotated["protection_quality_contract"][
        "profit_protection_label_does_not_guarantee_positive_realized_pnl"
    ] is True
