from pathlib import Path

import pytest

import stonkfly.data as data


def test_reuse_missing_source_is_runtime_error(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "DATA", tmp_path / "data")
    monkeypatch.setattr(data, "GRAPH", tmp_path / "data" / "graph.npz")
    empty = tmp_path / "doomfly"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="Missing reuse source"):
        data.prepare(empty)
    assert not (tmp_path / "data" / "graph.npz").exists()
