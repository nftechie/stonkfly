import pytest

from stonkfly import data
from stonkfly.neural import common


def test_verify_missing_prepared_inputs_is_runtime_error(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "DATA", tmp_path)
    monkeypatch.setattr(common, "GRAPH", tmp_path / "graph.npz")
    monkeypatch.setattr(data, "DATA", tmp_path)
    monkeypatch.setattr(data, "GRAPH", tmp_path / "graph.npz")
    with pytest.raises(RuntimeError, match="Annotation source missing"):
        data.verify()
    (tmp_path / "annotations.feather").write_bytes(b"not-the-released-file")
    with pytest.raises(RuntimeError, match="Prepared graph missing"):
        data.verify()
