import dataclasses
import time
from pathlib import Path

import pytest

from stonkfly.cli import wait_interval
from stonkfly.config import D, Settings
from stonkfly.ledger import Ledger
from stonkfly.market import Quote
from stonkfly.risk import Guard, Veto


def quote(**changes):
    q = Quote(
        "BTC-USDC",
        D("100"),
        D("100.1"),
        time.time(),
        D(".00000001"),
        D(".01"),
        D(".01"),
        D("1"),
        D(".00000001"),
    )
    return dataclasses.replace(q, **changes) if changes else q


def test_wide_spread_observation_does_not_halt(tmp_path):
    s = Settings()
    ledger = Ledger(tmp_path / "ledger.sqlite", s, "paper")
    try:
        g = Guard(s, ledger, tmp_path / "STOP")
        with pytest.raises(Veto, match="Spread"):
            g.check({"BTC-USDC": quote(ask=D("102"))}, time.time())
        assert ledger.get("halted") is None
        assert ledger.get("tick") == 0
    finally:
        ledger.close()


def test_wait_interval_fast_skips_sleep(monkeypatch):
    slept = []
    monkeypatch.setattr("stonkfly.cli.time.sleep", lambda s: slept.append(s))
    wait_interval(0, 60, Path("/nonexistent"), fast=True)
    assert slept == []
