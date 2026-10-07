"""The processing queue table.

One row per PDF: name, size, pages, mode, state, progress, pairs awaiting
validation and the actions. Rows can be reordered by dragging, and the extraction
mode can be overridden file by file.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QAction, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHeaderView,
    QMenu,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from pdfextract.core.job import JobState
from pdfextract.core.runner import PdfJob

# Column indices, named rather than written out at every call site: a table this
# wide is one inserted column away from a silent mix-up.
NAME, SIZE, PAGES, MODE, STATE, PROGRESS, REVIEW, ACTION = range(8)

COLUMNS = ("File", "Size", "Pages", "Mode", "State", "Progress", "To check", "")

STAGED_LABEL = "Ready"  # imported, ticked, but not handed to the runner yet

STATE_LABELS = {
    JobState.PENDING: "Waiting",
    JobState.ANALYSING: "Analysing",
    JobState.PARTIAL_EXPORT: "Exporting",
    JobState.AWAITING_VALIDATION: "Awaiting validation",
    JobState.VALIDATED: "Validated",
    JobState.FINALISING: "Finalising",
    JobState.DONE: "Done",
    JobState.FAILED: "Failed",
    JobState.CANCELLED: "Cancelled",
}

MODE_CHOICES = ("Global", "1 - single", "2 - systematic", "3 - smart")

# What each state actually means. Shown as a tooltip on the cell, because the one
# word in the column is not enough to tell whether the file still needs anything.
STATE_HELP = {
    JobState.PENDING: (
        "In the queue",
        "Waiting for a free worker. Nothing has been read from this file yet.",
    ),
    JobState.ANALYSING: (
        "Analysing",
        "Reading every page and scoring the join between each pair of consecutive "
        "pages, to work out which ones are two halves of the same picture.",
    ),
    JobState.PARTIAL_EXPORT: (
        "Writing images",
        "Writing every image that needs no decision from you. Pages belonging to an "
        "ambiguous pair are held back until you have decided.",
    ),
    JobState.AWAITING_VALIDATION: (
        "Waiting for you",
        "Everything that could be written has been written. The ambiguous pairs are "
        "waiting in the validation screen. This file holds nothing else up, and it "
        "can wait here until the next time you open the application.",
    ),
    JobState.VALIDATED: (
        "Validated",
        "You have decided every pair. The last images are about to be written.",
    ),
    JobState.FINALISING: (
        "Finishing",
        "Writing the images that came out of your decisions.",
    ),
    JobState.DONE: (
        "Done",
        "Every image has been written. If the recycle bin option is on and every "
        "check passed, the source PDF has been moved there.",
    ),
    JobState.FAILED: (
        "Failed",
        "This file could not be processed; the reason is in this tooltip when there "
        "is one, and always in the log. The rest of the queue carried on.",
    ),
    JobState.CANCELLED: (
        "Cancelled",
        "You stopped this file. What had already been written is still on disk.",
    ),
}

STAGED_HELP = (
    "Ready",
    "Imported and ticked, but nothing has been read yet. Press Start to process it; "
    "untick it to leave it out.",
)

COLUMN_HELP = {
    NAME: ("File", "The source PDF. Untick a file to leave it out of the next run."),
    SIZE: ("Size", "The size of the source PDF on disk."),
    PAGES: (
        "Pages",
        "How many pages the PDF holds. Only known once the file has been opened.",
    ),
    MODE: (
        "Mode",
        "How the pages of this file are paired. <i>Global</i> follows the mode "
        "chosen in the toolbar; the other three override it for this file only.",
    ),
    STATE: ("State", "Where this file is in its processing. Hover a cell for detail."),
    PROGRESS: ("Progress", "How far the current step has gone on this file."),
    REVIEW: (
        "To check",
        "Pairs of this file that the scorer could not decide. Click to open the "
        "validation screen on them.",
    ),
}


def rich_tooltip(title: str, text: str) -> str:
    """Return a tooltip with a bold title and an explanation underneath."""
    return f"<b>{title}</b><br>{text}"


def format_size(size: int) -> str:
    """Return a file size in the largest unit that keeps it readable."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024.0 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


def file_size(path: Path) -> int:
    """Return the size of a file, or zero when it cannot be read."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


class QueuePanel(QTableWidget):
    """Table of the files to process."""

    mode_changed = Signal(object, int)  # path, mode
    cancel_requested = Signal(object)  # path
    review_requested = Signal(object)  # path
    order_changed = Signal(list)  # list[Path]
    reprocess_requested = Signal(object, object)  # path, forced parity or None
    selection_changed = Signal()  # a tick box was clicked
    remove_requested = Signal(object)  # path, for a file that has not started

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, len(COLUMNS), parent)
        self.setHorizontalHeaderLabels(COLUMNS)
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDragDropOverwriteMode(False)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.verticalHeader().setDefaultSectionSize(38)
        header = self.horizontalHeader()
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setSectionResizeMode(NAME, QHeaderView.ResizeMode.Stretch)
        for column, (title, text) in COLUMN_HELP.items():
            item = self.horizontalHeaderItem(column)
            if item is not None:
                item.setToolTip(rich_tooltip(title, text))
        for column in range(1, len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self._rows: dict[Path, int] = {}
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self._staged: set[Path] = set()
        self._hidden: set[Path] = set()
        self.itemChanged.connect(self._on_item_changed)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt naming
        """Report the new order so the runner, and the table, can follow it.

        The caller is expected to answer with :meth:`rebuild`. A drop moves the row
        data but leaves the cell widgets behind, and the row numbers this table
        remembers no longer match, so the rows are rebuilt rather than patched.
        """
        super().dropEvent(event)
        self.order_changed.emit(self.ordered_paths())

    def _show_menu(self, position: QPoint) -> None:
        """Offer the per file actions, including the mode 2 parity override."""
        item = self.item(self.rowAt(position.y()), NAME)
        if item is None:
            return
        path = Path(item.data(Qt.ItemDataRole.UserRole))
        menu = QMenu(self)
        again = QAction("Process again", menu)
        again.triggered.connect(lambda: self.reprocess_requested.emit(path, None))
        menu.addAction(again)
        for parity, title in (
            (0, "Redo pairing it from page 1"),
            (1, "Redo pairing it from page 2 (first page on its own)"),
        ):
            action = QAction(title, menu)
            action.triggered.connect(
                lambda _=False, value=parity: self.reprocess_requested.emit(path, value)
            )
            menu.addAction(action)
        menu.exec(self.viewport().mapToGlobal(position))

    def ordered_paths(self) -> list[Path]:
        """Return the paths in the order currently shown."""
        paths: list[Path] = []
        for row in range(self.rowCount()):
            item = self.item(row, NAME)
            if item is not None:
                paths.append(Path(item.data(Qt.ItemDataRole.UserRole)))
        return paths

    def rebuild(self, jobs: list[PdfJob], staged: set[Path], checked: set[Path]) -> None:
        """Rebuild every row, in the order given, keeping the tick boxes as they were."""
        blocked = self.blockSignals(True)
        try:
            self.setRowCount(0)
            self._rows.clear()
            self._staged = set(staged)
            for job in jobs:
                row = self.rowCount()
                self.insertRow(row)
                self._rows[job.path] = row
                self._build_row(row, job)
                item = self.item(row, NAME)
                if item is not None:
                    item.setCheckState(
                        Qt.CheckState.Checked
                        if job.path in checked
                        else Qt.CheckState.Unchecked
                    )
                self._refresh_row(row, job)
        finally:
            self.blockSignals(blocked)
        if self._hidden:
            self.hide_paths(self._hidden)
        self.selection_changed.emit()

    def remove_path(self, path: Path) -> None:
        """Drop a row, for a file the user no longer wants in the list."""
        row = self._rows.get(path)
        if row is None or row >= self.rowCount():
            return
        self.removeRow(row)
        self._staged.discard(path)
        self._rows = {}
        for index in range(self.rowCount()):
            item = self.item(index, NAME)
            if item is not None:
                self._rows[Path(item.data(Qt.ItemDataRole.UserRole))] = index
        self.selection_changed.emit()

    def upsert(self, job: PdfJob, staged: bool = False) -> None:
        """Create or refresh the row of a job.

        ``staged`` marks a file that has been imported and ticked but not yet
        handed to the runner.
        """
        blocked = self.blockSignals(True)
        try:
            row = self._rows.get(job.path)
            if row is None or row >= self.rowCount():
                row = self.rowCount()
                self.insertRow(row)
                self._rows[job.path] = row
                self._build_row(row, job)
            if staged:
                self._staged.add(job.path)
            else:
                self._staged.discard(job.path)
            self._refresh_row(row, job)
        finally:
            self.blockSignals(blocked)

    def _on_action(self, path: Path) -> None:
        """Route the last column to a removal or to a cancellation."""
        if path in self._staged:
            self.remove_requested.emit(path)
        else:
            self.cancel_requested.emit(path)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Report a tick so the start button can show how many files are selected."""
        if item.column() == NAME:
            self.selection_changed.emit()

    def checked_paths(self) -> list[Path]:
        """Return the ticked files, in the order shown."""
        paths: list[Path] = []
        for row in range(self.rowCount()):
            item = self.item(row, NAME)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                paths.append(Path(item.data(Qt.ItemDataRole.UserRole)))
        return paths

    def staged_paths(self) -> list[Path]:
        """Return the files waiting to be started, ticked or not."""
        return [path for path in self.ordered_paths() if path in self._staged]

    def selected_staged_paths(self) -> list[Path]:
        """Return the files that a start would actually process."""
        ticked = set(self.checked_paths())
        return [path for path in self.staged_paths() if path in ticked]

    def selected_size(self) -> int:
        """Return the total size of the files a start would process."""
        return sum(file_size(path) for path in self.selected_staged_paths())

    def hide_paths(self, paths: set[Path]) -> None:
        """Hide the rows of files that this run is not going to touch.

        A folder often brings in far more books than are wanted; once the batch has
        started, the ones left out are only noise.
        """
        for row in range(self.rowCount()):
            item = self.item(row, NAME)
            if item is None:
                continue
            path = Path(item.data(Qt.ItemDataRole.UserRole))
            self.setRowHidden(row, path in paths)
        self._hidden = set(paths)

    def show_all_rows(self) -> None:
        """Bring back every row, once the run is over."""
        for row in range(self.rowCount()):
            self.setRowHidden(row, False)
        self._hidden = set()

    @property
    def hidden_paths(self) -> set[Path]:
        """Return the files currently out of sight."""
        return set(self._hidden)

    def set_all_checked(self, checked: bool) -> None:
        """Tick or untick every file that has not been started yet."""
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        blocked = self.blockSignals(True)
        try:
            for row in range(self.rowCount()):
                item = self.item(row, NAME)
                if item is None:
                    continue
                path = Path(item.data(Qt.ItemDataRole.UserRole))
                if path in self._staged:
                    item.setCheckState(state)
        finally:
            self.blockSignals(blocked)
        self.selection_changed.emit()

    def _build_row(self, row: int, job: PdfJob) -> None:
        """Create the widgets of a row."""
        name = QTableWidgetItem(job.path.name)
        name.setData(Qt.ItemDataRole.UserRole, str(job.path))
        name.setToolTip(str(job.path))
        # Imported files arrive ticked: the common case is to run the lot, and
        # unticking the few that are not wanted is quicker than ticking the rest.
        name.setFlags(name.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        name.setCheckState(Qt.CheckState.Checked)
        self.setItem(row, NAME, name)

        size = QTableWidgetItem(format_size(file_size(job.path)))
        size.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setItem(row, SIZE, size)

        pages = QTableWidgetItem("")
        pages.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setItem(row, PAGES, pages)

        mode = QComboBox(self)
        mode.setToolTip(rich_tooltip(*COLUMN_HELP[MODE]))
        mode.addItems(list(MODE_CHOICES))
        mode.setCurrentIndex(job.mode if job.mode in (1, 2, 3) else 0)
        mode.currentIndexChanged.connect(
            lambda index, path=job.path: self.mode_changed.emit(path, index)
        )
        self.setCellWidget(row, MODE, mode)

        self.setItem(row, STATE, QTableWidgetItem(""))
        bar = QProgressBar(self)
        bar.setTextVisible(False)
        bar.setFixedSize(140, 6)
        self.setCellWidget(row, PROGRESS, bar)

        review = QPushButton("", self)
        review.setFlat(True)
        review.setProperty("kind", "link")
        review.setMinimumWidth(120)  # otherwise the count is clipped off the label
        review.setToolTip(rich_tooltip(*COLUMN_HELP[REVIEW]))
        review.clicked.connect(lambda _, path=job.path: self.review_requested.emit(path))
        self.setCellWidget(row, REVIEW, review)

        action = QPushButton("Cancel", self)
        action.setMinimumWidth(104)  # wide enough for the longest label, "Remove"
        action.clicked.connect(lambda _, path=job.path: self._on_action(path))
        self.setCellWidget(row, ACTION, action)

    def _refresh_row(self, row: int, job: PdfJob) -> None:
        """Update the changing cells of a row."""
        pages = self.item(row, PAGES)
        state = self.item(row, STATE)
        if pages is None or state is None:
            return  # the row went away while an update was in flight
        pages.setText(str(job.page_count or ""))
        staged = job.path in self._staged
        state.setText(STAGED_LABEL if staged else STATE_LABELS.get(job.state, job.state.value))
        title, text = STAGED_HELP if staged else STATE_HELP.get(
            job.state, (job.state.value, "")
        )
        if job.errors and not staged:
            text = f"{text}<br><br>{'<br>'.join(job.errors)}"
        state.setToolTip(rich_tooltip(title, text))

        name = self.item(row, NAME)
        if name is not None and not staged:
            # Once a file is running its tick box no longer means anything.
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
        bar = self.cellWidget(row, PROGRESS)
        done, total = job.progress
        if isinstance(bar, QProgressBar):
            bar.setMaximum(max(1, total))
            bar.setValue(done if total else 0)

        review = self.cellWidget(row, REVIEW)
        if isinstance(review, QPushButton):
            pending = job.pending_count
            review.setText(f"{pending} pair(s) →" if pending else "")
            review.setEnabled(bool(pending))

        action = self.cellWidget(row, ACTION)
        if isinstance(action, QPushButton):
            # Nothing to cancel on a file that has not been handed over yet: the
            # button takes it off the list instead.
            action.setText("Remove" if staged else "Cancel")
            action.setEnabled(staged or not job.is_finished)
            action.setToolTip(
                rich_tooltip("Remove", "Take this file off the list. Nothing is deleted.")
                if staged
                else rich_tooltip(
                    "Cancel",
                    "Stop processing this file. The images already written stay where "
                    "they are.",
                )
            )

    def bind(self, on_mode: Callable[[Path, int], None]) -> None:
        """Convenience wiring used by the main window."""
        self.mode_changed.connect(on_mode)
