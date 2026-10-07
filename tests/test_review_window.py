"""Tests of the validation screen."""

from __future__ import annotations

from pathlib import Path

import pytest

from fixture_builder import build_pdf, make_text_page, spread_pages

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QPushButton

from pdfextract.core.job import DROP_BOTH, DROP_LEFT, DROP_RIGHT
from pdfextract.core.review_queue import ReviewQueue
from pdfextract.core.runner import PdfJob, analyse_and_export, record_decision
from pdfextract.core.settings import AppSettings
from pdfextract.ui.review_window import MERGE, SPLIT, ReviewWindow


def _settings(tmp_path: Path) -> AppSettings:
    settings = AppSettings()
    settings.mode = 3
    settings.output.root = str(tmp_path / "out")
    settings.seam.threshold_merge = 0.99  # force every pair into the grey zone
    settings.seam.threshold_split = 0.01
    settings.review.preload_ahead = 2
    settings.review.cache_mb = 64
    return settings


def _book(path: Path, spreads: int = 2) -> Path:
    pages = [make_text_page(seed=301)]
    for index in range(spreads):
        left, right = spread_pages(seed=2 + index)
        pages += [left, right]
    return build_pdf(path, pages, dpi=200)


@pytest.fixture
def prepared(tmp_path):
    """A queue holding one book with pairs to decide."""
    settings = _settings(tmp_path)
    queue = ReviewQueue()
    analyse_and_export(PdfJob(path=_book(tmp_path / "book.pdf")), settings, queue=queue)
    return queue, settings


@pytest.fixture
def window(qtbot, prepared):
    queue, settings = prepared
    recorded: list[tuple[str, str | None]] = []
    finalised: list = []

    def record(batch, label, decision):
        recorded.append((label, decision))
        record_decision(batch, label, decision)

    widget = ReviewWindow(
        queue, settings, finalise=finalised.append, record=record, on_focus_changed=None
    )
    qtbot.addWidget(widget)
    widget.show()  # visibility assertions are meaningless on a window never shown
    widget.recorded = recorded
    widget.finalised = finalised
    yield widget
    widget.preloader.stop()


def test_the_first_pair_is_on_screen(window):
    assert window.current is not None
    assert not window.left_view.pixmap().isNull()
    assert not window.right_view.pixmap().isNull()
    assert str(window.current.page_start) in window.header.text()


def test_key_1_merges_and_moves_on(qtbot, window):
    first = window.current.label
    remaining = len(window.entries)
    qtbot.keyClick(window, Qt.Key.Key_1)
    assert window.recorded == [(first, MERGE)]
    assert len(window.entries) == remaining - 1
    assert window.decided[MERGE] == 1


def test_key_2_keeps_the_pages_separate(qtbot, window):
    first = window.current.label
    qtbot.keyClick(window, Qt.Key.Key_2)
    assert window.recorded == [(first, SPLIT)]
    assert window.decided[SPLIT] == 1


def test_a_decision_needs_no_confirmation(qtbot, window):
    """One key press, one decision, next pair. Nothing in between."""
    before = window.current.label
    qtbot.keyClick(window, Qt.Key.Key_1)
    after = window.current.label if window.current else None
    assert after != before


def test_backspace_undoes_the_last_decision(qtbot, window):
    first = window.current.label
    qtbot.keyClick(window, Qt.Key.Key_1)
    qtbot.keyClick(window, Qt.Key.Key_Backspace)
    assert window.current.label == first
    assert window.decided[MERGE] == 0
    assert window.recorded[-1] == (first, None)


def test_the_history_holds_more_than_one_step(qtbot, tmp_path, prepared):
    """A single step back would do, but the session keeps a full history."""
    queue, settings = prepared
    for batch in list(queue.batches()):
        queue.remove(batch.pdf_path)
    analyse_and_export(
        PdfJob(path=_book(tmp_path / "longer.pdf", spreads=4)), settings, queue=queue
    )
    widget = ReviewWindow(queue, settings, finalise=lambda batch: None, record=record_decision)
    qtbot.addWidget(widget)
    widget.show()
    try:
        before = [(entry.pdf_path.name, entry.label) for entry in widget.entries]
        qtbot.keyClick(widget, Qt.Key.Key_1)
        qtbot.keyClick(widget, Qt.Key.Key_2)
        qtbot.keyClick(widget, Qt.Key.Key_Backspace)
        qtbot.keyClick(widget, Qt.Key.Key_Backspace)
        assert [(entry.pdf_path.name, entry.label) for entry in widget.entries] == before
        assert not any(widget.decided.values())
    finally:
        widget.preloader.stop()


def test_stepping_back_into_a_finished_book_is_refused(qtbot, window):
    """Its images are already being written; undoing would race that work."""
    while window.current is not None:
        qtbot.keyClick(window, Qt.Key.Key_1)
    decided = dict(window.decided)
    qtbot.keyClick(window, Qt.Key.Key_Backspace)
    assert window.decided == decided
    assert window.current is None


def test_buttons_do_the_same_thing_as_the_keys(window):
    first = window.current.label
    window.merge_button.click()
    assert window.recorded == [(first, MERGE)]


def test_buttons_never_take_the_focus(window):
    """Otherwise space or enter would replay the last button clicked."""
    widgets = window.findChildren(QPushButton) + window.findChildren(QCheckBox)
    assert widgets
    for widget in widgets:
        assert widget.focusPolicy() == Qt.FocusPolicy.NoFocus


def test_applying_the_choice_to_everything_left(window):
    total = len(window.entries)
    window.apply_all.setChecked(True)
    window.decide(SPLIT)
    assert window.entries == []
    assert window.decided[SPLIT] == total
    assert not window.apply_all.isChecked()


def test_the_batch_is_finalised_once_its_last_pair_is_decided(qtbot, window):
    while window.current is not None:
        qtbot.keyClick(window, Qt.Key.Key_1)
    qtbot.waitUntil(lambda: bool(window.finalised), timeout=5000)
    assert len(window.finalised) == 1


def test_the_summary_is_shown_when_the_queue_is_empty(qtbot, window):
    while window.current is not None:
        qtbot.keyClick(window, Qt.Key.Key_2)
    assert "Nothing left to check" in window.header.text()
    assert "kept separate" in window.header.text()


def test_moving_to_the_next_book_announces_it(qtbot, tmp_path, prepared):
    """Batches chain automatically, with a brief banner so the user knows."""
    queue, settings = prepared
    analyse_and_export(PdfJob(path=_book(tmp_path / "second.pdf")), settings, queue=queue)

    widget = ReviewWindow(queue, settings, finalise=lambda batch: None, record=record_decision)
    qtbot.addWidget(widget)
    widget.show()
    try:
        first_book = widget.current.pdf_path
        while widget.current is not None and widget.current.pdf_path == first_book:
            qtbot.keyClick(widget, Qt.Key.Key_2)
        assert widget.current is not None
        assert widget.current.pdf_path.name == "second.pdf"
        assert widget.banner.isVisible()
        assert "second.pdf" in widget.banner.text()
    finally:
        widget.preloader.stop()


def test_a_batch_arriving_during_the_session_is_appended(qtbot, tmp_path, window):
    """The session must never be interrupted by an arrival."""
    before = len(window.entries)
    settings = _settings(tmp_path)
    analyse_and_export(
        PdfJob(path=_book(tmp_path / "late.pdf")), settings, queue=window.queue
    )
    window.reload_entries()
    assert len(window.entries) > before
    assert window.current is not None


def test_the_metrics_are_hidden_unless_debugging(window):
    assert not window.metrics_label.isVisible()


def test_key_3_drops_the_left_page(qtbot, window):
    """The pages shown left to right, the keys numbered left to right."""
    first = window.current.label
    qtbot.keyClick(window, Qt.Key.Key_3)
    assert window.recorded == [(first, DROP_LEFT)]


def test_key_4_drops_the_right_page(qtbot, window):
    first = window.current.label
    qtbot.keyClick(window, Qt.Key.Key_4)
    assert window.recorded == [(first, DROP_RIGHT)]


def test_key_5_drops_both_pages(qtbot, window):
    first = window.current.label
    qtbot.keyClick(window, Qt.Key.Key_5)
    assert window.recorded == [(first, DROP_BOTH)]


def test_the_dropping_buttons_do_the_same_as_the_keys(window):
    first = window.current.label
    window.decision_buttons[DROP_BOTH].click()
    assert window.recorded == [(first, DROP_BOTH)]


def test_a_dropped_page_can_be_undone(qtbot, window):
    first = window.current.label
    qtbot.keyClick(window, Qt.Key.Key_4)
    qtbot.keyClick(window, Qt.Key.Key_Backspace)
    assert window.current.label == first
    assert not any(window.decided.values())


def test_the_recap_names_every_kind_of_decision(qtbot, window):
    qtbot.keyClick(window, Qt.Key.Key_3)
    while window.current is not None:
        qtbot.keyClick(window, Qt.Key.Key_1)
    text = window.header.text()
    assert "left page dropped" in text
    assert "merged" in text
