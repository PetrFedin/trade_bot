from decimal import Decimal

from tools.historical_strategy_matrix import _summary


def test_summary_exposes_benchmark_relative_gap():
    payload = {
        "total_return": "0.05",
        "ending_equity": "10500",
        "max_drawdown_fraction": "0.03",
        "closed_trade_count": 10,
        "win_rate": "0.5",
        "profit_factor": "1.2",
        "turnover_fraction": "4.0",
        "fees_paid": "25",
        "funding_cost_lower_bound": None,
        "funding_cost_upper_bound": None,
        "cost_adjusted_return_lower_bound": None,
        "cost_adjusted_return_upper_bound": None,
        "maximum_gross_exposure_fraction": "0.6",
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "benchmarks": [
            {
                "benchmark_id": "btc_buy_hold",
                "total_return": "0.20",
            },
            {
                "benchmark_id": "equal_weight_capital_matched",
                "total_return": "0.12",
            },
        ],
        "verdict": "PROFITABILITY_NOT_PROVEN",
        "evidence_sha256": "a" * 64,
    }

    summary = _summary(payload)

    assert Decimal(summary["alpha_vs_capital_matched_equal_weight"]) == Decimal("-0.07")
    assert Decimal(summary["alpha_vs_btc_buy_hold"]) == Decimal("-0.15")
    assert summary["verdict"] == "PROFITABILITY_NOT_PROVEN"
