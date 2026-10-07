"""The landing screen.

What the application opens on: choose the files, choose how to pair them, say where
the images go. Once files are in, the window switches to the queue and this screen
steps out of the way.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDropEvent,
    QIcon,
    QMouseEvent,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdfextract.ui.theme import (
    FONT_SIZE,
    FONT_SIZE_SMALL,
    RADIUS,
    Palette,
)

MODES = (
    (1, "One image per page", "No merging. Every page of the PDF becomes one image."),
    (2, "Pair every two pages", "Merge two by two. Where the pairing starts is detected."),
    (3, "Smart pairing", "Decide pair by pair. Ambiguous ones are shown to you."),
)


def split_drop(data: QMimeData) -> tuple[list[Path], list[Path]]:
    """Return the files and the folders carried by a drop, without walking them.

    Walking belongs on a worker: a dropped folder can hold thousands of files, and
    doing it here would freeze the window for as long as it takes.
    """
    files: list[Path] = []
    folders: list[Path] = []
    for url in data.urls():
        path = Path(url.toLocalFile())
        if not path.exists():
            continue
        if path.is_dir():
            folders.append(path)
        elif path.suffix.lower() == ".pdf":
            files.append(path)
    return files, folders


class ModeCard(QFrame):
    """One selectable extraction mode."""

    chosen = Signal(int)

    def __init__(self, mode: int, title: str, description: str, palette: Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mode = mode
        self._palette = palette
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(210)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        self.number = QLabel(f"Mode {mode}", self)
        self.number.setStyleSheet(
            f"color: {palette.text_faint}; font-size: {FONT_SIZE_SMALL}px; "
            "font-weight: 600; letter-spacing: 1px; background: transparent;"
        )
        self.title = QLabel(title, self)
        self.title.setStyleSheet(
            f"font-size: {FONT_SIZE + 1}px; font-weight: 600; background: transparent;"
        )
        self.description = QLabel(description, self)
        self.description.setWordWrap(True)
        self.description.setStyleSheet(
            f"color: {palette.text_muted}; font-size: {FONT_SIZE_SMALL}px; "
            "background: transparent;"
        )
        layout.addWidget(self.number)
        layout.addWidget(self.title)
        layout.addWidget(self.description)
        self.set_selected(False)

    def set_selected(self, selected: bool) -> None:
        """Highlight the card when it is the chosen mode."""
        self._selected = selected
        colours = self._palette
        border = colours.accent if selected else colours.border
        background = colours.surface_raised if selected else colours.surface
        self.setStyleSheet(
            f"ModeCard {{ background: {background}; border: 1px solid {border};"
            f" border-radius: {RADIUS}px; }}"
        )
        self.title.setStyleSheet(
            f"font-size: {FONT_SIZE + 1}px; font-weight: 600; background: transparent;"
            f" color: {colours.accent if selected else colours.text};"
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt naming
        """Choosing a mode is a click on the whole card, not on a small radio button."""
        self.chosen.emit(self.mode)
        super().mousePressEvent(event)


class DropZone(QFrame):
    """The big target that takes files and folders."""

    dropped = Signal(list, list)  # files, folders

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self.setAcceptDrops(True)
        self.setMinimumHeight(190)
        self._set_active(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.headline = QLabel("Drop your PDF files or a folder here", self)
        self.headline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.headline.setStyleSheet(
            f"font-size: {FONT_SIZE + 3}px; font-weight: 600; background: transparent;"
        )
        hint = QLabel("or pick them", self)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(
            f"color: {palette.text_muted}; background: transparent;"
        )

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.files_button = QPushButton("Choose files…", self)
        self.files_button.setProperty("kind", "primary")
        self.folder_button = QPushButton("Choose a folder…", self)
        buttons.addWidget(self.files_button)
        buttons.addWidget(self.folder_button)

        self.hint = hint
        self.busy_bar = QProgressBar(self)
        self.busy_bar.setRange(0, 0)  # the indeterminate, animated form
        self.busy_bar.setTextVisible(False)
        self.busy_bar.setFixedSize(220, 6)
        self.busy_bar.setVisible(False)
        self.buttons_row = QWidget(self)
        self.buttons_row.setLayout(buttons)

        layout.addWidget(self.headline)
        layout.addWidget(hint)
        layout.addWidget(self.buttons_row)
        layout.addWidget(self.busy_bar, 0, Qt.AlignmentFlag.AlignCenter)

    def set_busy(self, text: str) -> None:
        """Show that a folder is being walked."""
        self.headline.setText(text)
        self.hint.setVisible(False)
        self.buttons_row.setVisible(False)
        self.busy_bar.setVisible(True)

    def clear_busy(self) -> None:
        """Go back to inviting a drop."""
        self.headline.setText("Drop your PDF files or a folder here")
        self.hint.setVisible(True)
        self.buttons_row.setVisible(True)
        self.busy_bar.setVisible(False)

    def _set_active(self, active: bool) -> None:
        """Light the zone up while something is being dragged over it."""
        colours = self._palette
        border = colours.accent if active else colours.border_strong
        background = colours.surface_raised if active else colours.surface
        self.setStyleSheet(
            f"DropZone {{ background: {background}; border: 2px dashed {border};"
            f" border-radius: {RADIUS + 4}px; }}"
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt naming
        """Accept a drag that carries files."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._set_active(True)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:  # noqa: N802 - Qt naming
        """Return to the resting state."""
        self._set_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt naming
        """Take what was dropped; folders are walked elsewhere."""
        self._set_active(False)
        files, folders = split_drop(event.mimeData())
        if files or folders:
            event.acceptProposedAction()
            self.dropped.emit(files, folders)


class HeroPage(QWidget):
    """The screen the application opens on."""

    files_chosen = Signal(list, object)  # paths, source root
    folders_chosen = Signal(list)  # folders to walk, on a worker
    mode_chosen = Signal(int)
    output_chosen = Signal(str)

    def __init__(self, palette: Palette, icon: Path | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self.mode = 3
        self.recursive = True
        self.start_directory = ""
        self.cards: list[ModeCard] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)

        centre = QWidget(self)
        centre.setMaximumWidth(760)
        column = QVBoxLayout(centre)
        column.setContentsMargins(32, 24, 32, 32)
        column.setSpacing(18)

        column.addLayout(self._build_header(icon))
        self.zone = DropZone(palette, self)
        self.zone.dropped.connect(self._on_dropped)
        self.zone.files_button.clicked.connect(self.choose_files)
        self.zone.folder_button.clicked.connect(self.choose_folder)
        column.addWidget(self.zone)
        column.addLayout(self._build_modes())
        column.addLayout(self._build_output())

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(centre)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)

        self.setAcceptDrops(True)

    # ------------------------------------------------------------------ building

    def _build_header(self, icon: Path | None) -> QVBoxLayout:
        """Application name and one line saying what it does."""
        header = QVBoxLayout()
        header.setSpacing(4)
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if icon is not None and icon.is_file():
            badge = QLabel(self)
            badge.setPixmap(
                QIcon(str(icon)).pixmap(72, 72)
                if icon.suffix == ".ico"
                else QPixmap(str(icon)).scaled(
                    72, 72,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            header.addWidget(badge)
        title = QLabel("BookScan Extractor", self)
        title.setProperty("role", "title")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle = QLabel(
            "Page images out of scanned books, double pages back together.", self
        )
        subtitle.setProperty("role", "subtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(title)
        header.addWidget(subtitle)
        return header

    def _build_modes(self) -> QVBoxLayout:
        """The three extraction modes, as cards."""
        block = QVBoxLayout()
        block.setSpacing(8)
        label = QLabel("How should the pages be paired?", self)
        label.setProperty("role", "section")
        block.addWidget(label)

        row = QHBoxLayout()
        row.setSpacing(10)
        for mode, title, description in MODES:
            card = ModeCard(mode, title, description, self._palette, self)
            card.chosen.connect(self.set_mode)
            self.cards.append(card)
            row.addWidget(card)
        block.addLayout(row)
        self.set_mode(self.mode)
        return block

    def _build_output(self) -> QHBoxLayout:
        """Where the images will be written."""
        row = QHBoxLayout()
        row.setSpacing(10)
        caption = QLabel("Output folder", self)
        caption.setProperty("role", "section")
        self.output_label = QLabel("not chosen yet", self)
        self.output_label.setProperty("role", "faint")
        self.output_label.setWordWrap(False)
        button = QPushButton("Change…", self)
        button.clicked.connect(self.choose_output)
        row.addWidget(caption)
        row.addWidget(self.output_label, 1)
        row.addWidget(button)
        return row

    # ------------------------------------------------------------------- actions

    def set_mode(self, mode: int) -> None:
        """Select an extraction mode and light up its card."""
        self.mode = mode
        for card in self.cards:
            card.set_selected(card.mode == mode)
        self.mode_chosen.emit(mode)

    def set_output(self, root: str) -> None:
        """Show the output folder."""
        self.output_label.setText(root or "not chosen yet")

    def set_recursive(self, recursive: bool) -> None:
        """Remember whether a dropped folder is walked to the bottom."""
        self.recursive = recursive

    def set_start_directory(self, directory: str) -> None:
        """Remember where the file choosers should open."""
        self.start_directory = directory or ""

    def choose_files(self) -> None:
        """Pick PDF files."""
        selected, _ = QFileDialog.getOpenFileNames(
            self, "Choose PDF files", self.start_directory, "PDF (*.pdf)"
        )
        if selected:
            self.files_chosen.emit([Path(path) for path in selected], None)

    def choose_folder(self) -> None:
        """Pick a folder; the walk itself happens on a worker."""
        directory = QFileDialog.getExistingDirectory(
            self, "Choose a folder", self.start_directory
        )
        if directory:
            self.folders_chosen.emit([Path(directory)])

    def choose_output(self) -> None:
        """Pick where the images go."""
        directory = QFileDialog.getExistingDirectory(
            self, "Output folder", self.start_directory
        )
        if directory:
            self.set_output(directory)
            self.output_chosen.emit(directory)

    def _on_dropped(self, files: list, folders: list) -> None:
        """Forward a drop on the zone."""
        if files:
            self.files_chosen.emit(files, None)
        if folders:
            self.folders_chosen.emit(folders)

    def set_busy(self, text: str) -> None:
        """Say that a folder is being walked, and keep the eye busy while it is."""
        self.zone.set_busy(text)

    def clear_busy(self) -> None:
        """Return the drop zone to its resting state."""
        self.zone.clear_busy()

    # ---------------------------------------------------------- window wide drop

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 - Qt naming
        """The whole page takes a drop, not only the dashed rectangle."""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.zone._set_active(True)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:  # noqa: N802 - Qt naming
        self.zone._set_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 - Qt naming
        self.zone._set_active(False)
        files, folders = split_drop(event.mimeData())
        if files or folders:
            event.acceptProposedAction()
            self._on_dropped(files, folders)
