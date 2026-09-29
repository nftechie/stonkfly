"""CLI status must not open Coinbase; missing ledgers fail closed."""

import json
from pathlib import Path

import pytest

from stonkfly.cli import main
from stonkfly.config import Settings
from stonkfly.ledger import Ledger


def test_status_missing_ledger(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv", ["stonkfly", "status", "--out", str(tmp_path / "runs" / "paper")]
    )
    with pytest.raises(SystemExit, match="No ledger"):
        main()
    captured = capsys.readouterr()
    assert "No ledger" in captured.err or captured.out == ""


def test_status_reads_existing_ledger(tmp_path, monkeypatch, capsys):
    out = tmp_path / "runs" / "paper"
    ledger = Ledger(out / "ledger.sqlite", Settings(), "paper")
    ledger.close()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["stonkfly", "status", "--out", str(out)])
    main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "paper"
    assert payload["halted"] is None
    assert payload["cash"] == "100"
