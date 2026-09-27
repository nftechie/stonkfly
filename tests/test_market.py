from datetime import datetime, timezone

import numpy as np
import pytest

from stonkfly.display import market_frame
from stonkfly.market import CoinbaseMarket, FixtureMarket, utc_timestamp


class PublicSDK:
    def get_public_candles(self, product, start, end, granularity, limit):
        return {
            "candles": [
                {"start": str(int(end) + 60), "close": "999999"},
                {"start": str(int(end) - 60), "close": "99"},
                {"start": str(int(end) - 120), "close": "101"},
            ]
        }

    def get_public_product(self, product):
        return {
            "product_id": product,
            "product_type": "SPOT",
            "quote_currency_id": "USDC",
            "base_increment": ".00000001",
            "quote_increment": ".01",
            "quote_min_size": "1",
            "base_min_size": ".00000001",
        }

    def get_public_product_book(self, product, limit):
        return {
            "pricebook": {
                "product_id": product,
                "time": datetime.now(timezone.utc).isoformat(),
                "bids": [{"price": "100"}],
                "asks": [{"price": "100.1"}],
            }
        }


def test_completed_history_and_observation_clock():
    m = CoinbaseMarket(("BTC-USDC",), PublicSDK())
    quotes = m.snapshot()
    assert m.history["BTC-USDC"] == [101.0, 99.0]
    m.record(quotes)
    assert m.history["BTC-USDC"] == [101.0, 99.0, 100.05]
    m.snapshot()  # Execution quote refresh must not add another neural observation.
    assert len(m.history["BTC-USDC"]) == 3
    q = quotes["BTC-USDC"]
    frame = market_frame(q.product, m.history[q.product], q.bid, q.ask)
    assert frame.shape == (180, 320, 3) and frame.dtype == np.uint8
    other = market_frame(q.product, list(reversed(m.history[q.product])), q.bid, q.ask)
    assert not np.array_equal(frame, other)


def test_explicit_timestamp_zone():
    with pytest.raises(ValueError):
        utc_timestamp("2026-01-01T00:00:00")
    assert utc_timestamp("2026-01-01T00:00:00Z") == 1767225600


def test_fixture_observation_clock_does_not_skip_on_execution_refresh():
    m = FixtureMarket(("BTC-USDC",))
    first = m.snapshot()
    assert m.tick == 0
    m.record(first)
    assert m.tick == 1
    m.snapshot()  # Execution quote refresh must not advance fixture phase.
    assert m.tick == 1
    m.record(m.snapshot())
    assert m.tick == 2


def test_public_client_cannot_inherit_credentials(monkeypatch):
    import coinbase.rest

    observed = {}

    def factory(**kwargs):
        observed.update(kwargs)
        return PublicSDK()

    monkeypatch.setattr(coinbase.rest, "RESTClient", factory)
    CoinbaseMarket(("BTC-USDC",)).snapshot()
    assert observed["api_key"] is None and observed["api_secret"] is None
