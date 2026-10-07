"""Shared pytest configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

# Qt has to be able to start without a desktop, both in CI and in a plain terminal.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class TrashRecorder:
    """Stands in for ``send2trash`` and records what would have been binned."""

    def __init__(self) -> None:
        self.calls: list[Path] = []
        self.failure: Exception | None = None

    def __call__(self, target: str) -> None:
        if self.failure is not None:
            raise self.failure
        self.calls.append(Path(target))


@pytest.fixture(autouse=True)
def no_real_trash(monkeypatch) -> TrashRecorder:
    """Never let the test suite touch the real recycle bin.

    Applied to every test, not only the ones about trashing: a guard that regresses
    must fail a test, not move one of the developer's files.
    """
    recorder = TrashRecorder()
    monkeypatch.setattr("pdfextract.core.trash.send2trash", recorder)
    return recorder


@pytest.fixture(autouse=True)
def no_file_manager(monkeypatch) -> list:
    """Never let the test suite open a file manager window.

    The interface shows the output folder when a batch finishes, which is fine for
    a user and intolerable in a test run.
    """
    opened: list = []
    monkeypatch.setattr("os.startfile", opened.append, raising=False)
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: opened.append(a))
    return opened


@pytest.fixture(autouse=True)
def isolated_app_data(monkeypatch, tmp_path_factory) -> Path:
    """Keep settings and logs written by the tests out of the real user profile."""
    directory = tmp_path_factory.mktemp("appdata")
    monkeypatch.setenv("APPDATA", str(directory))
    return directory
