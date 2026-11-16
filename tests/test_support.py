"""Tests of the Ko-fi link and of the invitation to support the project."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QDesktopServices

from pdfextract.core.settings import AppSettings, default_settings_path, load_settings
from pdfextract.ui import app
from pdfextract.ui.main_window import MainWindow
from pdfextract.ui.support import KOFI_URL, SupportDialog, support_due
from pdfextract.ui.support import open_kofi as real_open_kofi  # bound before any patch


@pytest.fixture
def window(qtbot):
    widget = MainWindow()
    qtbot.addWidget(widget)
    yield widget
    widget.runner.stop(timeout=3.0)


@pytest.fixture
def scheduled(monkeypatch) -> list:
    """Record the invitations scheduled instead of waiting for the timer."""
    calls: list = []
    monkeypatch.setattr(
        QTimer, "singleShot", lambda delay, context, slot: calls.append((delay, slot))
    )
    return calls


def _answer(monkeypatch, button: str, optout: bool = False) -> None:
    """Close the invitation on a given button instead of waiting for the user."""

    def fake_exec(dialog: SupportDialog) -> int:
        dialog.optout_box.setChecked(optout)
        getattr(dialog, button).click()
        return 0

    monkeypatch.setattr(SupportDialog, "exec", fake_exec)


# ----------------------------------------------------------------- when to ask


@pytest.mark.parametrize(
    ("count", "due"),
    [
        (1, False),
        (9, False),
        (10, True),
        (11, False),
        (20, False),
        (99, False),
        (100, True),
        (101, False),
        (110, False),
        (199, False),
        (200, True),
        (1000, True),
    ],
)
def test_the_invitation_comes_at_the_tenth_launch_then_every_hundredth(count, due):
    assert support_due(count, optout=False) is due


@pytest.mark.parametrize("count", [10, 100, 200])
def test_opting_out_silences_the_invitation(count):
    assert support_due(count, optout=True) is False


@pytest.mark.parametrize(
    ("before", "due"), [(9, True), (10, False), (99, True), (199, True)]
)
def test_a_launch_is_counted_and_asks_when_due(window, scheduled, before, due):
    window.settings.usage_count = before
    assert app.count_launch(window) is due
    assert window.settings.usage_count == before + 1
    assert len(scheduled) == (1 if due else 0)
    if due:
        delay, slot = scheduled[0]
        assert 2000 <= delay <= 3000
        assert slot == window.show_support_prompt


@pytest.mark.parametrize("before", [9, 99, 199])
def test_a_launch_is_counted_but_does_not_ask_once_opted_out(window, scheduled, before):
    window.settings.usage_count = before
    window.settings.support_optout = True
    assert app.count_launch(window) is False
    assert window.settings.usage_count == before + 1
    assert scheduled == []


def test_the_count_is_kept_in_the_settings_file(window, scheduled, isolated_app_data):
    """It lives with the other settings, and nowhere near the real user profile."""
    app.count_launch(window)
    app.count_launch(window)
    path = default_settings_path()
    assert isolated_app_data in path.parents
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["usage_count"] == 2
    assert stored["support_optout"] is False
    assert load_settings().usage_count == 2


def test_settings_written_before_the_counter_still_load(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"mode": 2, "theme": "light"}), encoding="utf-8")
    settings = load_settings(path)
    assert (settings.mode, settings.usage_count, settings.support_optout) == (2, 0, False)
    assert AppSettings().usage_count == 0


# ------------------------------------------------------------------ the dialog


def test_the_invitation_says_what_it_is_for(window):
    dialog = SupportDialog(window)
    assert "BookScan Extractor" in dialog.text()
    assert "free and open source" in dialog.informativeText()
    assert "Ko-fi" in dialog.kofi_button.text()
    assert dialog.later_button.text() == "Later"
    assert dialog.optout_box.text() == "Don't ask me again"
    assert not dialog.optout_box.isChecked()
    assert dialog.escapeButton() is dialog.later_button
    assert dialog.kofi_button.property("kind") == "primary"


def test_supporting_opens_the_kofi_page(window, monkeypatch, no_browser):
    _answer(monkeypatch, "kofi_button")
    window.show_support_prompt()
    assert no_browser == ["kofi"]
    assert window.settings.support_optout is False


def test_later_opens_nothing_and_asks_again(window, monkeypatch, no_browser):
    _answer(monkeypatch, "later_button")
    window.show_support_prompt()
    assert no_browser == []
    assert window.settings.support_optout is False


def test_do_not_ask_again_is_remembered(window, monkeypatch, no_browser):
    _answer(monkeypatch, "later_button", optout=True)
    window.show_support_prompt()
    assert no_browser == []
    assert window.settings.support_optout is True
    assert load_settings().support_optout is True
    assert support_due(100, window.settings.support_optout) is False


def test_the_real_dialog_runs_and_closes_in_one_click(qtbot, window, no_browser):
    """The dialog as the user gets it: shown by the timer, sent away by one click."""
    seen: list[str] = []

    def click_later() -> None:
        dialog = next(w for w in window.findChildren(SupportDialog) if w.isVisible())
        seen.append(dialog.text())
        dialog.later_button.click()

    QTimer.singleShot(0, window, window.show_support_prompt)
    QTimer.singleShot(200, click_later)
    qtbot.waitUntil(lambda: bool(seen), timeout=5000)
    assert "Enjoying BookScan Extractor?" in seen[0]
    assert no_browser == []


# -------------------------------------------------------------------- the link


def test_the_help_menu_leads_to_kofi(window, no_browser):
    actions = [action for menu in window.menuBar().actions() for action in menu.menu().actions()]
    support_action = next(action for action in actions if "Ko-fi" in action.text())
    support_action.trigger()
    assert no_browser == ["kofi"]


def test_the_link_points_at_the_kofi_page(monkeypatch):
    """The real function, with only the browser itself stood in for."""
    opened: list[str] = []
    monkeypatch.setattr(
        QDesktopServices, "openUrl", lambda url: opened.append(QUrl(url).toString())
    )
    real_open_kofi()
    assert opened == [KOFI_URL] == ["https://ko-fi.com/hypedigger"]
