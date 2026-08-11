from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import pytest

from app.marketdata.alpaca_historical import (
    AlpacaHistoricalBarsClient,
    AlpacaHistoricalBarsRequest,
    HttpJsonPage,
)


def request() -> AlpacaHistoricalBarsRequest:
    return AlpacaHistoricalBarsRequest(
        symbols=("AAPL", "MSFT"),
        start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2025, 2, 1, tzinfo=UTC),
        timeframe="1Day",
        feed="iex",
        adjustment="all",
        limit=2,
    )


def row(timestamp: str, close: float) -> dict[str, object]:
    return {
        "t": timestamp,
        "o": close - 1,
        "h": close + 1,
        "l": close - 2,
        "c": close,
        "v": 1000,
        "n": 50,
        "vw": close - 0.25,
    }


def test_multisymbol_client_paginates_and_preserves_request_ids() -> None:
    calls: list[str] = []

    def transport(url: str, _headers: dict[str, str]) -> HttpJsonPage:
        calls.append(url)
        query = parse_qs(urlparse(url).query)
        if "page_token" not in query:
            return HttpJsonPage(
                200,
                {"X-Request-ID": "req-1"},
                {
                    "bars": {
                        "AAPL": [row("2025-01-02T05:00:00Z", 101.0)],
                    },
                    "next_page_token": "next-1",
                },
            )
        assert query["page_token"] == ["next-1"]
        return HttpJsonPage(
            200,
            {"x-request-id": "req-2"},
            {
                "bars": {
                    "MSFT": [row("2025-01-02T05:00:00Z", 201.0)],
                },
                "next_page_token": None,
            },
        )

    result = AlpacaHistoricalBarsClient(
        key_id="key",
        secret_key="secret",
        transport=transport,
    ).fetch(request())
    result.validate(minimum_bars_per_symbol=1)
    assert len(calls) == 2
    first_query = parse_qs(urlparse(calls[0]).query)
    assert first_query["symbols"] == ["AAPL,MSFT"]
    assert first_query["feed"] == ["iex"]
    assert first_query["adjustment"] == ["all"]
    assert result.dataset.request_ids == ("req-1", "req-2")
    assert result.dataset.symbols == ("AAPL", "MSFT")
    assert result.page_count == 2
    assert result.missing_symbols == ()


def test_multisymbol_client_reports_missing_symbol() -> None:
    def transport(_url: str, _headers: dict[str, str]) -> HttpJsonPage:
        return HttpJsonPage(
            200,
            {},
            {
                "bars": {"AAPL": [row("2025-01-02T05:00:00Z", 101.0)]},
                "next_page_token": None,
            },
        )

    result = AlpacaHistoricalBarsClient(
        key_id="key",
        secret_key="secret",
        transport=transport,
    ).fetch(request())
    assert result.missing_symbols == ("MSFT",)
    with pytest.raises(ValueError, match="missing symbols"):
        result.validate()


def test_multisymbol_client_rejects_repeated_pagination_token() -> None:
    def transport(_url: str, _headers: dict[str, str]) -> HttpJsonPage:
        return HttpJsonPage(
            200,
            {},
            {"bars": {}, "next_page_token": "loop"},
        )

    client = AlpacaHistoricalBarsClient(
        key_id="key",
        secret_key="secret",
        transport=transport,
    )
    with pytest.raises(ValueError, match="token repeated"):
        client.fetch(request())


def test_request_rejects_duplicate_symbols() -> None:
    invalid = AlpacaHistoricalBarsRequest(
        symbols=("AAPL", "AAPL"),
        start=datetime(2025, 1, 1, tzinfo=UTC),
        end=datetime(2025, 2, 1, tzinfo=UTC),
    )
    with pytest.raises(ValueError, match="unique"):
        invalid.validate()
