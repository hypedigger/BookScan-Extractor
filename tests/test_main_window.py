"""Tests of the main window: importing, ticking and starting a batch."""

from __future__ import annotations

from pathlib import Path

import pytest

from fixture_builder import build_pdf, make_text_page, spread_pages

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from pdfextract.core.job import JobState
from pdfextract.ui.main_window import MainWindow
from pdfextract.ui.queue_panel import NAME, STAGED_LABEL, STATE


def _book(path: Path) -> Path:
    left, right = spread_pages(seed=3)
    return build_pdf(path, [make_text_page(seed=700), left, right], dpi=120)


@pytest.fixture
def folder(tmp_path) -> Path:
    """A folder holding three PDF files."""
    source = tmp_path / "scans"
    source.mkdir()
    for name in ("alpha.pdf", "beta.pdf", "gamma.pdf"):
        _book(source / name)
    return source


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *args, **kwargs: None)
    widget = MainWindow()
    widget.settings.output.root = str(tmp_path / "out")
    widget.settings.mode = 1
    qtbot.addWidget(widget)
    yield widget
    widget.runner.stop(timeout=3.0)


def _rows(window) -> dict[str, str]:
    """Return the state shown for each row, by file name."""
    states = {}
    for row in range(window.panel.rowCount()):
        states[window.panel.item(row, NAME).text()] = window.panel.item(row, STATE).text()
    return states


def test_importing_a_folder_lists_its_pdf_files(window, folder):
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    assert set(_rows(window)) == {"alpha.pdf", "beta.pdf", "gamma.pdf"}


def test_imported_files_are_ticked_by_default(window, folder):
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    assert len(window.panel.checked_paths()) == 3
    assert len(window.panel.selected_staged_paths()) == 3


def test_importing_does_not_start_anything(window, folder):
    """Nothing runs until the user says so."""
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    assert window.runner.jobs == []
    assert set(_rows(window).values()) == {STAGED_LABEL}


def test_starting_only_submits_the_ticked_files(qtbot, window, folder):
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    unwanted = window.panel.item(1, NAME)
    unwanted.setCheckState(Qt.CheckState.Unchecked)
    skipped = unwanted.text()

    window.start_selected()
    assert {job.path.name for job in window.runner.jobs} == {"alpha.pdf", "gamma.pdf"}
    assert skipped not in {job.path.name for job in window.runner.jobs}


def test_the_unticked_file_stays_available(window, folder):
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    window.panel.item(1, NAME).setCheckState(Qt.CheckState.Unchecked)
    window.start_selected()

    remaining = window.panel.staged_paths()
    assert [path.name for path in remaining] == ["beta.pdf"]
    assert _rows(window)["beta.pdf"] == STAGED_LABEL


def test_starting_with_nothing_ticked_does_nothing(window, folder):
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    window.panel.set_all_checked(False)
    window.start_selected()
    assert window.runner.jobs == []


def test_tick_all_and_untick_all(window, folder):
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    window.panel.set_all_checked(False)
    assert window.panel.selected_staged_paths() == []
    window.panel.set_all_checked(True)
    assert len(window.panel.selected_staged_paths()) == 3


def test_the_start_button_counts_the_ticked_files(window, folder):
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    assert window.start_action.text() == "Start (3)"
    assert window.start_action.isEnabled()

    window.panel.set_all_checked(False)
    assert not window.start_action.isEnabled()
    assert "3 of 3" not in window.staged_label.text()


def test_a_file_already_running_cannot_be_unticked(qtbot, window, folder):
    """Its tick box no longer means anything once the runner has it."""
    window._stage(sorted(folder.glob("*.pdf"))[:1], source_root=folder)
    window.start_selected()
    item = window.panel.item(0, NAME)
    assert not (item.flags() & Qt.ItemFlag.ItemIsUserCheckable)


def test_importing_the_same_file_twice_adds_one_row(window, folder):
    files = sorted(folder.glob("*.pdf"))
    window._stage(files, source_root=folder)
    window._stage(files, source_root=folder)
    assert window.panel.rowCount() == 3


def test_a_started_batch_reaches_the_end(qtbot, window, folder):
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    window.start_selected()
    qtbot.waitUntil(
        lambda: bool(window.runner.jobs)
        and all(job.is_finished for job in window.runner.jobs),
        timeout=60000,
    )
    assert {job.state for job in window.runner.jobs} == {JobState.DONE}


def test_a_staged_file_can_be_removed_from_the_list(window, folder):
    """The last column removes a file that has not been started rather than cancelling."""
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    victim = Path(window.panel.item(1, NAME).data(Qt.ItemDataRole.UserRole))
    window.panel._on_action(victim)
    assert window.panel.rowCount() == 2
    assert victim not in window.panel.staged_paths()
    assert victim.name not in _rows(window)


def test_reordering_keeps_the_rows_consistent(window, folder):
    """A drop moves row data but not cell widgets, so the table is rebuilt."""
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    window.panel.item(0, NAME).setCheckState(Qt.CheckState.Unchecked)
    ordered = list(reversed(window.panel.ordered_paths()))

    window._on_order_changed(ordered)

    assert window.panel.ordered_paths() == ordered
    assert [path.name for path in window.panel.checked_paths()] == [
        path.name for path in ordered[:2]
    ]
    for row in range(window.panel.rowCount()):
        assert window.panel.cellWidget(row, 3) is not None  # the mode selector survived
        assert window.panel.cellWidget(row, 7) is not None  # and the action button
    assert set(_rows(window).values()) == {STAGED_LABEL}


def test_the_output_folder_is_shown_once_the_batch_is_over(qtbot, window, folder, no_file_manager):
    """A recurring option of the other applications: show the result when done."""
    window.settings.output.open_when_done = True
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    window.start_selected()
    qtbot.waitUntil(
        lambda: bool(window.runner.jobs)
        and all(job.is_finished for job in window.runner.jobs),
        timeout=60000,
    )
    qtbot.waitUntil(lambda: bool(no_file_manager), timeout=5000)
    assert len(no_file_manager) == 1  # once for the batch, not once per file


def test_the_output_folder_stays_shut_when_the_option_is_off(qtbot, window, folder,
                                                             no_file_manager):
    window.settings.output.open_when_done = False
    window._stage(sorted(folder.glob("*.pdf"))[:1], source_root=folder)
    window.start_selected()
    qtbot.waitUntil(
        lambda: bool(window.runner.jobs)
        and all(job.is_finished for job in window.runner.jobs),
        timeout=60000,
    )
    assert no_file_manager == []


def test_the_last_folder_is_remembered(window, folder):
    """Another recurring option: the next chooser opens where the user left off."""
    window._remember_directory(folder)
    assert window.settings.last_directory == str(folder)
    assert window.hero.start_directory == str(folder)


def test_unticked_files_are_hidden_while_the_batch_runs(window, folder):
    """A folder brings in more books than are wanted; the rest is noise once started."""
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    skipped = Path(window.panel.item(1, NAME).data(Qt.ItemDataRole.UserRole))
    window.panel.item(1, NAME).setCheckState(Qt.CheckState.Unchecked)

    window.start_selected()
    assert window.panel.hidden_paths == {skipped}
    assert window.panel.isRowHidden(1)


def test_the_hidden_files_come_back_when_the_run_is_over(qtbot, window, folder):
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    window.panel.item(1, NAME).setCheckState(Qt.CheckState.Unchecked)
    window.start_selected()

    qtbot.waitUntil(
        lambda: bool(window.runner.jobs)
        and all(job.is_finished for job in window.runner.jobs),
        timeout=60000,
    )
    window._refresh_progress()
    assert window.panel.hidden_paths == set()
    assert not any(window.panel.isRowHidden(row) for row in range(window.panel.rowCount()))


def test_the_progress_strip_is_hidden_until_something_runs(window, folder):
    assert not window.progress_strip.isVisible()
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    assert not window.progress_strip.isVisible()


def test_the_progress_strip_reports_the_batch(qtbot, window, folder):
    window.show()
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    window.start_selected()
    assert window.progress_strip.isVisible()
    assert "file(s)" in window.batch_label.text()

    qtbot.waitUntil(
        lambda: bool(window.runner.jobs)
        and all(job.is_finished for job in window.runner.jobs),
        timeout=60000,
    )
    window._refresh_progress()
    assert window.batch_bar.value() == window.batch_bar.maximum()
    assert window.eta_label.text() == "finished"


def test_the_tick_row_goes_away_while_the_batch_runs(window, folder):
    """It would otherwise count files that are no longer on screen."""
    window.show()
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    window.panel.item(1, NAME).setCheckState(Qt.CheckState.Unchecked)
    assert window.selection_row.isVisible()

    window.start_selected()
    assert not window.selection_row.isVisible()


def test_the_tick_row_comes_back_with_the_hidden_files(qtbot, window, folder):
    window.show()
    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    window.panel.item(1, NAME).setCheckState(Qt.CheckState.Unchecked)
    window.start_selected()
    qtbot.waitUntil(
        lambda: bool(window.runner.jobs)
        and all(job.is_finished for job in window.runner.jobs),
        timeout=60000,
    )
    window._refresh_progress()
    assert window.selection_row.isVisible()
    # It comes back the way the user left it: present, and still unticked.
    assert "0 of 1" in window.staged_label.text()
    assert window.panel.staged_paths()


def test_choosing_a_folder_scans_it_without_freezing_the_window(qtbot, window, folder):
    """A folder on a share can take seconds to walk; it must not block the window."""
    window.show()
    window.scan_folders([folder])
    assert "Looking for PDF files" in window.busy_label.text()

    qtbot.waitUntil(lambda: window.panel.rowCount() == 3, timeout=20000)
    assert not window.busy_row.isVisible()
    assert {path.name for path in window.panel.staged_paths()} == {
        "alpha.pdf", "beta.pdf", "gamma.pdf"
    }


def test_the_queue_page_shows_the_scan_too(qtbot, window, folder, tmp_path):
    """Adding a second folder from the toolbar has to say so as well."""
    window.show()
    other = tmp_path / "more"
    other.mkdir()
    _book(other / "delta.pdf")
    window._stage([next(folder.glob("*.pdf"))], source_root=folder)  # leaves the hero

    window.scan_folders([other])
    assert window.busy_row.isVisible()
    qtbot.waitUntil(lambda: window.panel.rowCount() == 2, timeout=20000)
    assert not window.busy_row.isVisible()


def test_the_landing_screen_says_it_is_looking(qtbot, window, folder):
    window.show()
    window.scan_folders([folder])
    assert "Looking for PDF files" in window.hero.zone.headline.text()
    assert window.hero.zone.busy_bar.isVisible()
    assert not window.hero.zone.buttons_row.isVisible()

    qtbot.waitUntil(lambda: window.panel.rowCount() == 3, timeout=20000)
    assert window.hero.zone.busy_bar.isVisible() is False
    assert "Drop your PDF files" in window.hero.zone.headline.text()


def test_an_empty_folder_says_so(qtbot, window, tmp_path, monkeypatch):
    told: list = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a))
    empty = tmp_path / "empty"
    empty.mkdir()
    window.scan_folders([empty])
    qtbot.waitUntil(lambda: bool(told), timeout=20000)
    assert window.panel.rowCount() == 0


def test_every_state_has_an_explanation():
    """The one word in the column is not enough to know what is happening."""
    from pdfextract.core.job import JobState
    from pdfextract.ui.queue_panel import STATE_HELP

    assert set(STATE_HELP) == set(JobState)
    for title, text in STATE_HELP.values():
        assert title and len(text) > 30


def test_the_state_cell_carries_its_explanation(window, folder):
    from pdfextract.ui.queue_panel import STATE

    window._stage(sorted(folder.glob("*.pdf")), source_root=folder)
    tooltip = window.panel.item(0, STATE).toolTip()
    assert "<b>Ready</b>" in tooltip
    assert "Press Start" in tooltip


def test_a_failed_file_shows_its_reason_in_the_tooltip(qtbot, window, tmp_path):
    from pdfextract.ui.queue_panel import STATE

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf")
    window._stage([broken], source_root=None)
    window.start_selected()
    qtbot.waitUntil(
        lambda: bool(window.runner.jobs) and window.runner.jobs[0].is_finished,
        timeout=30000,
    )
    tooltip = window.panel.item(0, STATE).toolTip()
    assert "<b>Failed</b>" in tooltip
    assert "cannot open" in tooltip
