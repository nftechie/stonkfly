"""Missing checkpoint files must fail closed as RuntimeError, not FileNotFoundError."""

from pathlib import Path

import pytest

from stonkfly.cli import restore_checkpoint


class FakeController:
    def restore(self, path):
        raise AssertionError(f"should not restore {path}")


def test_missing_checkpoint_file_is_runtime_error(tmp_path):
    with pytest.raises(RuntimeError, match="Checkpoint file missing"):
        restore_checkpoint(
            FakeController(),
            tmp_path,
            {"file": "brain-0.npz", "sha256": "deadbeef"},
        )


def test_checkpoint_integrity_mismatch(tmp_path):
    path = tmp_path / "brain-0.npz"
    path.write_bytes(b"not-a-real-checkpoint")
    with pytest.raises(RuntimeError, match="Checkpoint integrity mismatch"):
        restore_checkpoint(
            FakeController(),
            tmp_path,
            {"file": "brain-0.npz", "sha256": "deadbeef"},
        )
