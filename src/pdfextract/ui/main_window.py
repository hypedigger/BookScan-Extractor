"""Main window: import, processing queue and the door to the validation screen."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from pdfextract.core.applog import log_path
from pdfextract.core.job import JobState
from pdfextract.core.progress import batch_progress, format_duration, tally
from pdfextract.core.review_queue import ReviewBatch, ReviewQueue
from pdfextract.core.runner import BatchRunner, PdfJob, record_decision
from pdfextract.core.settings import AppSettings, load_settings, save_settings
from pdfextract.ui.hero import HeroPage, split_drop
from pdfextract.ui.queue_panel import QueuePanel
from pdfextract.ui.review_window import ReviewWindow
from pdfextract.ui.settings_dialog import edit_settings
from pdfextract.ui.theme import palette_for, stylesheet
from pdfextract.ui.workers import (
    FolderScanner,
    RunnerBridge,
    lower_current_thread_priority,
)

MODE_LABELS = ("1 - one image per page", "2 - systematic pairing", "3 - smart pairing")

PROJECT_ROOT = Path(__file__).resolve().parents[3]
ICON_PATH = PROJECT_ROOT / "assets" / "app.png"

HERO_PAGE, QUEUE_PAGE = 0, 1

HELP = {
    "add_files": ("Add files", "Pick one or more PDF files."),
    "add_folder": (
        "Add a folder",
        "Take every PDF of a folder. They arrive ticked; untick the ones you do not "
        "want before starting.",
    ),
    "recursive": (
        "Include sub-folders",
        "Walk the whole tree under the folder you pick. The output reproduces the "
        "same tree.",
    ),
    "mode": (
        "Extraction mode",
        "<b>1 - single</b>: one image per page, nothing is merged.<br>"
        "<b>2 - systematic</b>: every page merged two by two; where the pairing "
        "starts is detected for you.<br>"
        "<b>3 - smart</b>: decided pair by pair from the join between the pages; the "
        "ambiguous ones are shown to you.",
    ),
    "output": ("Output folder", "Where the images are written."),
    "start": (
        "Start",
        "Process the ticked files, and only those. The others stay on the list and "
        "are hidden while the run lasts.",
    ),
    "pause": (
        "Pause",
        "Stop picking up new files. The file being processed finishes first.",
    ),
    "settings": (
        "Settings",
        "Thresholds, metric weights, naming, post-processing, theme. Every value the "
        "engine uses is here.",
    ),
    "review": (
        "Pairs to check",
        "Open the validation screen. Keys 1 to 5 decide each pair, Backspace steps "
        "back.",
    ),
    "trash": (
        "Recycle bin",
        "Move a source PDF to the Windows recycle bin once every one of its images "
        "has been written and checked. Never a permanent delete, and never before "
        "the output has been verified.",
    ),
    "open_when_done": (
        "Open when finished",
        "Show the output folder as soon as the whole batch is over.",
    ),
    "tick_all": ("Tick all", "Select every file that has not been started."),
    "untick_all": ("Untick all", "Leave every file out of the next run."),
    "progress": (
        "Progress of the batch",
        "The unit is one page read once. In modes 2 and 3 a book is read twice, once "
        "to score its pairs and once to write the images, so it counts double.",
    ),
    "tally": (
        "What the batch will produce",
        "Counted in output images: <b>single</b> comes from one page, <b>merged</b> "
        "from two joined, <b>to check</b> is still waiting for your decision.",
    ),
    "eta": (
        "Time left",
        "Worked out from the speed since the batch started. Files not yet opened "
        "have their page count estimated from their size, which is what <i>about</i> "
        "means.",
    ),
}


def tip(key: str) -> str:
    """Return the tooltip of a control, with a bold title and an explanation."""
    title, text = HELP[key]
    return f"<b>{title}</b><br>{text}"

# The application runs under pythonw.exe, which has no console. Any console
# program it starts would open one of its own and flash it in the user's face.
NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


class MainWindow(QMainWindow):
    """The window the application opens on."""

    def __init__(self) -> None:
        super().__init__()
        self.settings: AppSettings = load_settings()
        self.queue = ReviewQueue()
        self.runner = BatchRunner(
            self.settings, queue=self.queue, worker_hook=lower_current_thread_priority
        )
        self.bridge = RunnerBridge(self.runner, self.queue, self)
        self.bridge.job_changed.connect(self._on_job_changed)
        self.bridge.queue_changed.connect(self._on_queue_changed)
        self._review_window: ReviewWindow | None = None
        self._announced = False
        # Imported files wait here, ticked, until the user starts the batch.
        self._staged: dict[Path, PdfJob] = {}
        self._opened_output = False
        self._batch_started: float | None = None
        self._scanner: FolderScanner | None = None

        self.setWindowTitle("BookScan Extractor")
        self._palette = palette_for(self.settings.theme)
        self._apply_theme()
        self._build_ui()
        self.runner.start()
        # The estimate has to keep counting down even while nothing else moves.
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(1000)
        self._progress_timer.timeout.connect(self._refresh_progress)
        self.resize(1180, 720)
        self.setAcceptDrops(True)

    # ------------------------------------------------------------------ building

    def _build_ui(self) -> None:
        """Build the toolbar, the landing screen and the queue view."""
        toolbar = QToolBar("Main", self)
        toolbar.setMovable(False)
        self.toolbar = toolbar
        self.addToolBar(toolbar)

        add_files = QAction("Add files…", self)
        add_files.setToolTip(tip("add_files"))
        add_files.triggered.connect(self.add_files)
        toolbar.addAction(add_files)

        add_folder = QAction("Add a folder…", self)
        add_folder.setToolTip(tip("add_folder"))
        add_folder.triggered.connect(self.add_folder)
        toolbar.addAction(add_folder)

        self.recursive = QCheckBox("Include sub-folders", self)
        self.recursive.setToolTip(tip("recursive"))
        self.recursive.setChecked(True)
        toolbar.addWidget(self.recursive)
        toolbar.addSeparator()

        toolbar.addWidget(QLabel(" Mode ", self))
        self.mode_combo = QComboBox(self)
        self.mode_combo.setToolTip(tip("mode"))
        self.mode_combo.addItems(MODE_LABELS)
        self.mode_combo.setCurrentIndex(max(0, self.settings.mode - 1))
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        toolbar.addWidget(self.mode_combo)
        toolbar.addSeparator()

        output = QAction("Output folder…", self)
        output.setToolTip(tip("output"))
        output.triggered.connect(self.choose_output)
        toolbar.addAction(output)

        self.start_action = QAction("Start", self)
        self.start_action.setToolTip(tip("start"))
        self.start_action.triggered.connect(self.start_selected)
        toolbar.addAction(self.start_action)
        # The stylesheet reaches the button behind the action, not the action itself.
        started = toolbar.widgetForAction(self.start_action)
        if started is not None:
            started.setObjectName("primary")

        self.pause_action = QAction("Pause", self)
        self.pause_action.setToolTip(tip("pause"))
        self.pause_action.triggered.connect(self.toggle_pause)
        toolbar.addAction(self.pause_action)

        preferences = QAction("Settings…", self)
        preferences.setToolTip(tip("settings"))
        preferences.triggered.connect(self.open_settings)
        toolbar.addAction(preferences)

        self.stack = QStackedWidget(self)
        self.hero = HeroPage(self._palette, ICON_PATH, self)
        self.hero.files_chosen.connect(self._on_hero_files)
        self.hero.folders_chosen.connect(self.scan_folders)
        self.hero.mode_chosen.connect(self._on_hero_mode)
        self.hero.output_chosen.connect(self._on_hero_output)
        self.hero.set_output(self.settings.output.root)
        self.hero.set_mode(self.settings.mode)
        self.hero.set_start_directory(self.settings.last_directory)
        self.stack.addWidget(self.hero)
        self.stack.addWidget(self._build_queue_page())
        self.setCentralWidget(self.stack)

        self.setStatusBar(QStatusBar(self))
        self._refresh_output_label()
        self._refresh_start_action()
        self._build_menu()
        self._show_hero()

    def _build_queue_page(self) -> QWidget:
        """Build the list of files and everything around it."""
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        self.notice = QLabel("", central)
        self.notice.setStyleSheet(
            "background: #fff4ce; color: #4a3f1a; padding: 6px; border-radius: 3px;"
        )
        self.notice.setVisible(False)
        layout.addWidget(self.notice)

        busy = QHBoxLayout()
        busy.setContentsMargins(0, 0, 0, 0)
        self.busy_label = QLabel("", central)
        self.busy_label.setProperty("role", "muted")
        self.busy_bar = QProgressBar(central)
        self.busy_bar.setRange(0, 0)  # the indeterminate, animated form
        self.busy_bar.setTextVisible(False)
        self.busy_bar.setFixedSize(200, 6)
        busy.addWidget(self.busy_label)
        busy.addWidget(self.busy_bar)
        busy.addStretch(1)
        self.busy_row = QWidget(central)
        self.busy_row.setLayout(busy)
        self.busy_row.setVisible(False)
        layout.addWidget(self.busy_row)

        self.panel = QueuePanel(central)
        self.panel.mode_changed.connect(self._on_file_mode_changed)
        self.panel.cancel_requested.connect(self.runner.cancel)
        self.panel.review_requested.connect(self.open_review_at)
        self.panel.order_changed.connect(self._on_order_changed)
        self.panel.remove_requested.connect(self._remove_staged)
        self.panel.reprocess_requested.connect(self._reprocess)
        self.panel.selection_changed.connect(self._refresh_start_action)
        layout.addWidget(self.panel, 1)

        selection = QHBoxLayout()
        selection.setContentsMargins(0, 0, 0, 0)
        self.select_all_button = QPushButton("Tick all", central)
        self.select_all_button.setToolTip(tip("tick_all"))
        self.select_all_button.clicked.connect(lambda: self.panel.set_all_checked(True))
        self.select_none_button = QPushButton("Untick all", central)
        self.select_none_button.setToolTip(tip("untick_all"))
        self.select_none_button.clicked.connect(lambda: self.panel.set_all_checked(False))
        self.staged_label = QLabel("", central)
        selection.addWidget(self.select_all_button)
        selection.addWidget(self.select_none_button)
        selection.addWidget(self.staged_label)
        selection.addStretch(1)
        self.selection_row = QWidget(central)
        self.selection_row.setLayout(selection)
        layout.addWidget(self.selection_row)

        strip = QVBoxLayout()
        strip.setSpacing(4)
        heading = QHBoxLayout()
        self.batch_label = QLabel("", central)
        self.batch_label.setProperty("role", "muted")
        self.batch_label.setToolTip(tip("progress"))
        self.eta_label = QLabel("", central)
        self.eta_label.setProperty("role", "faint")
        self.eta_label.setToolTip(tip("eta"))
        self.tally_label = QLabel("", central)
        self.tally_label.setProperty("role", "muted")
        self.tally_label.setToolTip(tip("tally"))
        heading.addWidget(self.batch_label, 1)
        heading.addWidget(self.tally_label)
        heading.addWidget(self.eta_label)
        self.batch_bar = QProgressBar(central)
        self.batch_bar.setObjectName("batch")
        self.batch_bar.setRange(0, 1000)
        self.batch_bar.setValue(0)
        self.batch_bar.setTextVisible(False)
        self.batch_bar.setFixedHeight(8)
        self.batch_bar.setToolTip(tip("progress"))
        strip.addLayout(heading)
        strip.addWidget(self.batch_bar)
        self.progress_strip = QWidget(central)
        self.progress_strip.setLayout(strip)
        self.progress_strip.setVisible(False)
        layout.addWidget(self.progress_strip)

        bottom = QHBoxLayout()
        self.review_button = QPushButton("Nothing to check", central)
        self.review_button.setToolTip(tip("review"))
        self.review_button.setEnabled(False)
        self.review_button.clicked.connect(self.open_review)
        bottom.addWidget(self.review_button)

        self.trash_box = QCheckBox("Move processed PDF files to the recycle bin", central)
        self.trash_box.setToolTip(tip("trash"))
        self.trash_box.setChecked(self.settings.trash.enabled)
        self.trash_box.toggled.connect(self._on_trash_toggled)
        bottom.addWidget(self.trash_box)

        self.open_box = QCheckBox("Open the output folder when finished", central)
        self.open_box.setToolTip(tip("open_when_done"))
        self.open_box.setChecked(self.settings.output.open_when_done)
        self.open_box.toggled.connect(self._on_open_toggled)
        bottom.addWidget(self.open_box)
        bottom.addStretch(1)

        self.output_label = QLabel("", central)
        self.output_label.setProperty("role", "faint")
        bottom.addWidget(self.output_label)
        layout.addLayout(bottom)
        return central

    def _apply_theme(self) -> None:
        """Apply the palette to the whole application."""
        application = QApplication.instance()
        if isinstance(application, QApplication):
            application.setStyleSheet(stylesheet(self.settings.theme))

    # ------------------------------------------------------------------- pages

    def _show_hero(self) -> None:
        """Go back to the landing screen and put the toolbar away."""
        self.stack.setCurrentIndex(HERO_PAGE)
        self.toolbar.setVisible(False)
        self.hero.set_output(self.settings.output.root)
        self.hero.set_mode(self.settings.mode)

    def _show_queue(self) -> None:
        """Switch to the list of files."""
        self.stack.setCurrentIndex(QUEUE_PAGE)
        self.toolbar.setVisible(True)

    @property
    def on_hero(self) -> bool:
        """Return True while the landing screen is showing."""
        return self.stack.currentIndex() == HERO_PAGE

    def _on_hero_files(self, paths: list, source_root: object) -> None:
        """Take the files chosen on the landing screen into the queue."""
        root = source_root if isinstance(source_root, Path) else None
        if paths:
            self._remember_directory(root or Path(paths[0]).parent)
        if not paths:
            QMessageBox.information(self, "Nothing to do", "No PDF found there.")
            return
        self._stage(list(paths), source_root=root)

    def _on_hero_mode(self, mode: int) -> None:
        """Follow the mode chosen on the landing screen."""
        if mode == self.settings.mode:
            return
        self.settings.mode = mode
        self.mode_combo.setCurrentIndex(max(0, mode - 1))
        save_settings(self.settings)

    def _on_hero_output(self, directory: str) -> None:
        """Follow the output folder chosen on the landing screen."""
        self.settings.output.root = directory
        save_settings(self.settings)
        self._refresh_output_label()

    # ------------------------------------------------------------ window wide drop

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt naming
        """Files can be dropped on the window at any point, not only on the hero."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt naming
        """Import what was dropped. Folders are walked on a worker."""
        files, folders = split_drop(event.mimeData())
        if not files and not folders:
            return
        event.acceptProposedAction()
        if files:
            self._remember_directory(files[0].parent)
            self._stage(files, source_root=None)
        if folders:
            self.scan_folders(folders)

    def _build_menu(self) -> None:
        """Build the menu bar."""
        file_menu = self.menuBar().addMenu("&File")
        for title, slot in (
            ("Add files…", self.add_files),
            ("Add a folder…", self.add_folder),
            ("Output folder…", self.choose_output),
        ):
            action = QAction(title, self)
            action.triggered.connect(slot)
            file_menu.addAction(action)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("&Help")
        update_action = QAction("Check for updates", self)
        update_action.triggered.connect(self.check_for_updates)
        help_menu.addAction(update_action)
        log_action = QAction("Open the log folder", self)
        log_action.triggered.connect(self.open_log_folder)
        help_menu.addAction(log_action)

    # -------------------------------------------------------------------- import

    def add_files(self) -> None:
        """Pick one or more PDF files."""
        selected, _ = QFileDialog.getOpenFileNames(
            self, "Choose PDF files", self.settings.last_directory, "PDF (*.pdf)"
        )
        if selected:
            self._remember_directory(Path(selected[0]).parent)
            self._stage([Path(path) for path in selected], source_root=None)

    def add_folder(self) -> None:
        """Pick a folder; walking it happens on a worker."""
        directory = QFileDialog.getExistingDirectory(
            self, "Choose a folder", self.settings.last_directory
        )
        if directory:
            self.scan_folders([Path(directory)])

    def scan_folders(self, folders: list[Path]) -> None:
        """Look for PDF files under folders, without freezing the window.

        A folder on a network share, or one holding a few thousand files, takes
        long enough that the user needs to be told something is happening.
        """
        if not folders:
            return
        self._remember_directory(folders[0])
        if self._scanner is not None and self._scanner.isRunning():
            self._scanner.cancel()
        scanner = FolderScanner(folders, recursive=self.recursive.isChecked(), parent=self)
        scanner.counted.connect(self._on_scan_counted)
        scanner.found.connect(self._on_scan_found)
        self._scanner = scanner
        name = folders[0].name if len(folders) == 1 else f"{len(folders)} folders"
        self._set_busy(f"Looking for PDF files in {name}\u2026")
        scanner.start()

    def _on_scan_counted(self, count: int) -> None:
        """Report what the walk has found so far."""
        self._set_busy(f"Looking for PDF files\u2026 {count} found")

    def _on_scan_found(self, files: list, root: object) -> None:
        """Take the result of a walk into the queue."""
        self._clear_busy()
        source_root = root if isinstance(root, Path) else None
        if not files:
            QMessageBox.information(self, "Nothing to do", "No PDF found in that folder.")
            return
        self._stage([Path(path) for path in files], source_root=source_root)

    def _set_busy(self, text: str) -> None:
        """Show that something is being looked for, wherever the user is."""
        self.hero.set_busy(text)
        self.busy_label.setText(text)
        self.busy_row.setVisible(True)
        self.statusBar().showMessage(text)

    def _clear_busy(self) -> None:
        """Put the busy indicator away."""
        self.hero.clear_busy()
        self.busy_row.setVisible(False)
        self.statusBar().clearMessage()

    def _remember_directory(self, directory: Path) -> None:
        """Remember where the user was, so the next chooser opens there."""
        self.settings.last_directory = str(directory)
        self.hero.set_start_directory(self.settings.last_directory)
        save_settings(self.settings)

    def _stage(self, paths: list[Path], source_root: Path | None) -> None:
        """List imported files, ticked, without starting anything yet.

        Choosing a folder is how a whole shelf gets imported, and not every PDF in
        it is always wanted. The files are shown first and only the ticked ones are
        handed to the runner when the batch is started.
        """
        added = 0
        for path in paths:
            if path in self._staged or any(job.path == path for job in self.runner.jobs):
                continue
            job = PdfJob(path=path, source_root=source_root)
            self._staged[path] = job
            self.panel.upsert(job, staged=True)
            added += 1
        self._refresh_start_action()
        if added:
            self._show_queue()
        self.statusBar().showMessage(
            f"{added} file(s) added and ticked; press Start to process them", 6000
        )

    def start_selected(self) -> None:
        """Hand the ticked files to the runner, and only those."""
        paths = self.panel.selected_staged_paths()
        if not paths:
            QMessageBox.information(
                self, "Nothing selected", "Tick at least one file before starting."
            )
            return
        if not self.settings.output.root:
            self.choose_output()
            if not self.settings.output.root:
                return

        grouped: dict[Path | None, list[Path]] = {}
        for path in paths:
            staged = self._staged.get(path)
            grouped.setdefault(staged.source_root if staged else None, []).append(path)
        for source_root, items in grouped.items():
            for job in self.runner.submit(items, source_root=source_root):
                self.panel.upsert(job)
        # The files left out are not part of this run; showing them would only make
        # the list harder to read while it works.
        self.panel.hide_paths(set(self.panel.staged_paths()) - set(paths))
        for path in paths:
            self._staged.pop(path, None)
        self._opened_output = False  # a new batch deserves its own visit to the folder
        self._batch_started = time.monotonic()
        self._progress_timer.start()
        self._refresh_progress()
        self._refresh_start_action()
        self.statusBar().showMessage(f"{len(paths)} file(s) started", 5000)

    def _on_order_changed(self, ordered: list[Path]) -> None:
        """Follow a drag and drop, in the runner and in the table."""
        self.runner.reorder(ordered)
        known: dict[Path, PdfJob] = {job.path: job for job in self.runner.jobs}
        known.update(self._staged)
        checked = set(self.panel.checked_paths())
        rows = [known[path] for path in ordered if path in known]
        self.panel.rebuild(rows, set(self._staged), checked)

    def _remove_staged(self, path: Path) -> None:
        """Take a file that has not been started off the list."""
        self._staged.pop(path, None)
        self.panel.remove_path(path)
        self._refresh_start_action()
        if self.panel.rowCount() == 0:
            self._show_hero()  # an empty list has nothing to say, so offer the start again

    def _refresh_start_action(self) -> None:
        """Keep the start button in step with how many files are ticked."""
        hidden = self.panel.hidden_paths
        staged = [path for path in self.panel.staged_paths() if path not in hidden]
        selected = len([path for path in self.panel.selected_staged_paths()
                        if path not in hidden])
        waiting = len(staged)
        self.start_action.setText(f"Start ({selected})" if selected else "Start")
        self.start_action.setEnabled(bool(selected))
        for button in (self.select_all_button, self.select_none_button):
            button.setEnabled(bool(waiting))
        # Nothing left to choose from means the row is only in the way.
        self.selection_row.setVisible(bool(waiting))
        self.staged_label.setText(
            f"{selected} of {waiting} file(s) ticked" if waiting else ""
        )

    def _reprocess(self, path: Path, force_parity: object) -> None:
        """Run a file again, optionally imposing where the mode 2 pairing starts."""
        existing = next((job for job in self.runner.jobs if job.path == path), None)
        parity = force_parity if isinstance(force_parity, int) else None
        for job in self.runner.submit(
            [path],
            source_root=existing.source_root if existing else None,
            mode=existing.mode if existing else None,
            force_parity=parity,
        ):
            self.panel.upsert(job)
        if parity is None:
            message = f"{path.name} queued again."
        else:
            start = "page 1" if parity == 0 else "page 2"
            message = f"{path.name} queued again, pairing from {start}."
        self.statusBar().showMessage(message, 6000)

    def choose_output(self) -> None:
        """Pick the output root folder."""
        directory = QFileDialog.getExistingDirectory(self, "Output folder")
        if directory:
            self.settings.output.root = directory
            save_settings(self.settings)
            self._refresh_output_label()

    def _refresh_output_label(self) -> None:
        """Show where images are written."""
        root = self.settings.output.root or "(not chosen)"
        self.output_label.setText(f"Output: {root}")

    # ------------------------------------------------------------------- settings

    def _on_mode_changed(self, index: int) -> None:
        """Change the global extraction mode."""
        self.settings.mode = index + 1
        save_settings(self.settings)

    def _on_file_mode_changed(self, path: Path, index: int) -> None:
        """Override the mode for one file only."""
        for job in self.runner.jobs:
            if job.path == path:
                job.mode = None if index == 0 else index

    def _on_trash_toggled(self, checked: bool) -> None:
        """Remember the recycle bin choice between sessions."""
        self.settings.trash.enabled = checked
        save_settings(self.settings)

    def _on_open_toggled(self, checked: bool) -> None:
        """Remember whether the output folder is shown at the end."""
        self.settings.output.open_when_done = checked
        save_settings(self.settings)

    def _maybe_open_output(self) -> None:
        """Show the images once the whole batch is over, and only once.

        Files still awaiting validation do not count as finished: opening the
        folder then would show half of the book.
        """
        if not self.settings.output.open_when_done or self._opened_output:
            return
        jobs = self.runner.jobs
        if not jobs or not all(job.is_finished for job in jobs):
            return
        if not any(job.state is JobState.DONE for job in jobs):
            return
        self._opened_output = True
        root = Path(self.settings.output.root or "")
        if root.is_dir():
            self.open_folder(root)

    def open_folder(self, target: Path) -> None:
        """Open a folder in the file manager."""
        if sys.platform == "win32":
            os.startfile(target)
        else:
            subprocess.Popen(["xdg-open", str(target)])

    def open_settings(self) -> None:
        """Open the settings dialog."""
        edited = edit_settings(self.settings, self)
        if edited is None:
            return
        theme_changed = edited.theme != self.settings.theme
        self.settings = edited
        self.runner.settings = edited
        save_settings(edited)
        if theme_changed:
            self._palette = palette_for(edited.theme)
            self._apply_theme()
        self.mode_combo.setCurrentIndex(max(0, edited.mode - 1))
        self.trash_box.setChecked(edited.trash.enabled)
        self.open_box.setChecked(edited.output.open_when_done)
        self._refresh_output_label()

    def toggle_pause(self) -> None:
        """Pause or resume the processing queue."""
        if self.runner.paused:
            self.runner.resume()
            self.pause_action.setText("Pause")
        else:
            self.runner.pause()
            self.pause_action.setText("Resume")

    # --------------------------------------------------------------------- events

    def _on_job_changed(self, job: PdfJob) -> None:
        """Refresh the row of a job."""
        self.panel.upsert(job)
        self._refresh_start_action()
        self._refresh_progress()
        self._maybe_open_output()

    def _refresh_progress(self) -> None:
        """Update the bar at the bottom, its wording and the time left."""
        jobs = self.runner.jobs
        if not jobs or self._batch_started is None:
            self.progress_strip.setVisible(False)
            return
        elapsed = time.monotonic() - self._batch_started
        report = batch_progress(jobs, elapsed)
        self.progress_strip.setVisible(True)
        self.batch_bar.setValue(int(report.fraction * 1000))
        self.batch_label.setText(
            f"{report.label}    ·    {report.files_done}/{report.files_total} file(s)"
            f"    ·    {report.percent}%"
        )
        counts = tally(jobs)
        if counts.worth_showing:
            parts = [f"{counts.single} single", f"{counts.merged} merged"]
            if counts.to_check:
                parts.append(f"{counts.to_check} to check")
            if counts.dropped:
                parts.append(f"{counts.dropped} dropped")
            self.tally_label.setText("    ·    ".join(parts))
        else:
            self.tally_label.setText("")

        if report.seconds_left is not None:
            about = "about " if report.estimated else ""
            self.eta_label.setText(f"{about}{format_duration(report.seconds_left)} left")
        elif not report.running:
            self.eta_label.setText("finished")
        else:
            self.eta_label.setText("estimating…")

        if not report.running:
            # The run is over: stop counting and show the files left out again.
            self._progress_timer.stop()
            self.panel.show_all_rows()
            self._refresh_start_action()

    def _on_queue_changed(self, queue: ReviewQueue) -> None:
        """Refresh the validation indicator and announce the first batch."""
        summary = queue.summary()
        self.review_button.setText(summary)
        self.review_button.setEnabled(not queue.is_empty())
        if not queue.is_empty() and not self._announced:
            self._announced = True
            self._announce(f"{summary}. Click the button at the bottom left to start.")
        if self._review_window is not None:
            self._review_window.reload_entries()

    def _announce(self, message: str) -> None:
        """Show a discreet banner. Never a modal dialog: the user may be validating."""
        self.notice.setText(message)
        self.notice.setVisible(True)
        QTimer.singleShot(8000, lambda: self.notice.setVisible(False))

    # ------------------------------------------------------------------ validation

    def open_review(self) -> None:
        """Open the validation screen on the first pending pair."""
        if self.queue.is_empty():
            return
        if self._review_window is None:
            self._review_window = ReviewWindow(
                self.queue,
                self.settings,
                finalise=self._finalise_batch,
                record=record_decision,
                on_focus_changed=self._on_review_focus,
                parent=None,
            )
            self._review_window.finished.connect(self._on_review_closed)
        self._review_window.show()
        self._review_window.raise_()
        self._review_window.activateWindow()

    def open_review_at(self, path: Path) -> None:
        """Open the validation screen straight on the batch of a given file."""
        self.open_review()
        window = self._review_window
        if window is None:
            return
        for index, entry in enumerate(window.entries):
            if entry.pdf_path == path:
                window.position = index
                window.show_current()
                break

    def _finalise_batch(self, batch: ReviewBatch) -> None:
        """Called from the validation screen once a book is fully decided."""
        self.runner.finalise_batch(batch)

    def _on_review_focus(self, active: bool) -> None:
        """Give the processor to the interactive part while the user is validating."""
        if self.settings.review.throttle_background:
            self.runner.throttle(active)

    def _on_review_closed(self) -> None:
        """Forget the validation window once it is closed."""
        self._review_window = None

    # ---------------------------------------------------------------------- help

    def open_log_folder(self) -> None:
        """Open the folder holding the application log."""
        self.open_folder(log_path().parent)

    def check_for_updates(self) -> None:
        """Pull the latest version and sync the dependencies, then offer a restart."""
        if not (PROJECT_ROOT / ".git").exists():
            QMessageBox.information(
                self,
                "Updates",
                "This copy was not installed from the repository, so it cannot update "
                "itself. Run install.ps1 again to reinstall it.",
            )
            return
        self.statusBar().showMessage("Checking for updates…")
        try:
            pull = subprocess.run(
                ["git", "pull", "--ff-only"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )
            sync = subprocess.run(
                ["uv", "sync"],
                cwd=PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            QMessageBox.warning(self, "Updates", f"The update failed: {exc}")
            return
        finally:
            self.statusBar().clearMessage()

        report = (pull.stdout or pull.stderr).strip()
        if pull.returncode != 0:
            QMessageBox.warning(self, "Updates", f"git pull failed:\n{report}")
            return
        if "Already up to date" in report or "à jour" in report:
            QMessageBox.information(self, "Updates", "This is already the latest version.")
            return
        if sync.returncode != 0:
            QMessageBox.warning(
                self, "Updates", f"Dependencies could not be synchronised:\n{sync.stderr}"
            )
            return
        QMessageBox.information(
            self,
            "Updates",
            f"{report}\n\nThe update is installed. Restart the application to use it.",
        )

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        """Stop the workers cleanly."""
        if self._review_window is not None:
            self._review_window.close()
        if self._scanner is not None and self._scanner.isRunning():
            self._scanner.cancel()
            self._scanner.wait(1000)
        self.runner.stop(timeout=2.0)
        save_settings(self.settings)
        super().closeEvent(event)
