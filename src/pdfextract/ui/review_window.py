"""Validation screen for the ambiguous pairs.

Fluidity first. The user has to be able to take dozens of decisions at the speed of
the keyboard, so a decision takes effect immediately, with no confirmation, and the
next pair is already decoded. Everything else about this screen follows from that.

The two pages are shown as they are, side by side, with a thin neutral rule to mark
the join. No preview of the merge: the eye has to go straight to the seam.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import (
    QCloseEvent,
    QImage,
    QKeyEvent,
    QPixmap,
    QResizeEvent,
    QShowEvent,
)
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdfextract.core.job import (
    DROP_BOTH,
    DROP_LEFT,
    DROP_RIGHT,
    MERGE,
    SPLIT,
)
from pdfextract.core.review_queue import ReviewBatch, ReviewQueue
from pdfextract.core.settings import AppSettings
from pdfextract.ui.preloader import PairKey, PreloadCache, Preloader, window_around
from pdfextract.ui.theme import (
    CHECK_ICON,
    FONT_SIZE,
    FONT_SIZE_LARGE,
    FONT_SIZE_SMALL,
    RADIUS_SMALL,
    palette_for,
)

# What each decision is called once it is done, for the end of session recap.
DECISION_NAMES = {
    MERGE: "merged",
    SPLIT: "kept separate",
    DROP_LEFT: "left page dropped",
    DROP_RIGHT: "right page dropped",
    DROP_BOTH: "both pages dropped",
}

# Which key takes which decision. The second entry of each line is the key that
# carries that digit unshifted on a French layout, so the top row works as printed.
KEY_DECISIONS: dict[int, str] = {
    int(Qt.Key.Key_1): MERGE,
    int(Qt.Key.Key_Ampersand): MERGE,
    int(Qt.Key.Key_2): SPLIT,
    int(Qt.Key.Key_Eacute): SPLIT,
    int(Qt.Key.Key_3): DROP_LEFT,
    int(Qt.Key.Key_QuoteDbl): DROP_LEFT,
    int(Qt.Key.Key_4): DROP_RIGHT,
    int(Qt.Key.Key_Apostrophe): DROP_RIGHT,
    int(Qt.Key.Key_5): DROP_BOTH,
    int(Qt.Key.Key_ParenLeft): DROP_BOTH,
}

SEPARATOR_WIDTH = 2
IMAGE_MARGIN = 12


@dataclass
class Entry:
    """One decision to take: a pair, and the batch it belongs to."""

    batch: ReviewBatch
    label: str
    page_start: int
    page_end: int
    score: float
    metrics: dict[str, float]

    @property
    def pdf_path(self) -> Path:
        """Return the PDF this pair comes from."""
        return self.batch.pdf_path


@dataclass
class HistoryStep:
    """One decision, kept so that the back step can undo it."""

    position: int
    entry: Entry
    decision: str


class ReviewWindow(QMainWindow):
    """The screen that shows one ambiguous pair at a time."""

    finished = Signal()

    def __init__(
        self,
        queue: ReviewQueue,
        settings: AppSettings,
        finalise: Callable[[ReviewBatch], None] | None = None,
        record: Callable[[ReviewBatch, str, str | None], None] | None = None,
        on_focus_changed: Callable[[bool], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.queue = queue
        self.settings = settings
        self._finalise = finalise
        self._record = record
        self._on_focus_changed = on_focus_changed

        self.entries: list[Entry] = []
        self.position = 0
        self.history: list[HistoryStep] = []
        self.decided = dict.fromkeys(DECISION_NAMES, 0)
        self._current_pdf: Path | None = None
        self._pixmaps: dict[PairKey, tuple[QPixmap, QPixmap]] = {}
        self._finalised: set[Path] = set()

        self.setWindowTitle("Validation")
        self._build_ui()

        self.preloader = Preloader(PreloadCache(settings.review.cache_mb))
        self.preloader.ready.connect(self._on_preloaded)
        self.preloader.start(Preloader.Priority.HighPriority)

        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(180)
        self._resize_timer.timeout.connect(self._on_resize_settled)

        self._banner_timer = QTimer(self)
        self._banner_timer.setSingleShot(True)
        self._banner_timer.timeout.connect(self._hide_banner)

        self.reload_entries()

    # ------------------------------------------------------------------ building

    def _build_ui(self) -> None:
        """Build the layout: a discreet header, the two pages, a row of buttons."""
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = QLabel("", central)
        self.header.setObjectName("header")
        self.header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.header.setContentsMargins(10, 6, 10, 6)
        layout.addWidget(self.header)

        self.metrics_label = QLabel("", central)
        self.metrics_label.setObjectName("metrics")
        self.metrics_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.metrics_label.setVisible(self.settings.debug_metrics)
        layout.addWidget(self.metrics_label)

        self.banner = QLabel("", central)
        self.banner.setObjectName("banner")
        self.banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.banner.setVisible(False)
        layout.addWidget(self.banner)

        pages = QWidget(central)
        pages_layout = QHBoxLayout(pages)
        pages_layout.setContentsMargins(IMAGE_MARGIN, 4, IMAGE_MARGIN, 4)
        pages_layout.setSpacing(0)
        self.left_view = QLabel(pages)
        self.left_view.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.right_view = QLabel(pages)
        self.right_view.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.separator = QWidget(pages)
        self.separator.setObjectName("separator")
        self.separator.setFixedWidth(SEPARATOR_WIDTH)
        pages_layout.addWidget(self.left_view, 1)
        pages_layout.addWidget(self.separator)
        pages_layout.addWidget(self.right_view, 1)
        layout.addWidget(pages, 1)

        buttons = QWidget(central)
        buttons_layout = QHBoxLayout(buttons)
        buttons_layout.setContentsMargins(10, 6, 10, 8)
        self.back_button = QPushButton("← Undo", buttons)
        self.apply_all = QCheckBox("Apply this choice to all remaining pairs", buttons)
        self.decision_buttons: dict[str, QPushButton] = {}
        for key, decision, title in (
            ("1", MERGE, "Merge"),
            ("2", SPLIT, "Keep separate"),
            ("3", DROP_LEFT, "Delete left"),
            ("4", DROP_RIGHT, "Delete right"),
            ("5", DROP_BOTH, "Delete both"),
        ):
            button = QPushButton(f"{key} — {title}", buttons)
            button.clicked.connect(lambda _=False, value=decision: self.decide(value))
            self.decision_buttons[decision] = button
        self.merge_button = self.decision_buttons[MERGE]
        self.split_button = self.decision_buttons[SPLIT]
        for widget in (self.back_button, self.apply_all, *self.decision_buttons.values()):
            # Without this, space or enter would replay the last clicked button.
            widget.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.back_button.clicked.connect(self.undo)
        buttons_layout.addWidget(self.back_button)
        buttons_layout.addStretch(1)
        buttons_layout.addWidget(self.apply_all)
        buttons_layout.addStretch(1)
        # In the order the keys are numbered, left to right.
        for decision in (MERGE, SPLIT, DROP_LEFT, DROP_RIGHT, DROP_BOTH):
            button = self.decision_buttons[decision]
            if decision in (DROP_LEFT, DROP_RIGHT, DROP_BOTH):
                button.setProperty("kind", "danger")
            buttons_layout.addWidget(button)
        layout.addWidget(buttons)

        self.setCentralWidget(central)
        # The screen is darker than the rest of the application on purpose: the two
        # pages have to be the only bright thing on it. The colours still come from
        # the shared theme, so the buttons follow it, including the red ones.
        colours = palette_for(self.settings.theme)
        self.setStyleSheet(
            f"""
            QWidget {{ background: {colours.window}; color: {colours.text}; }}
            QLabel#header {{ font-size: {FONT_SIZE}px; color: {colours.text_muted}; }}
            QLabel#metrics {{ font-size: {FONT_SIZE_SMALL}px; color: {colours.text_faint}; }}
            QLabel#banner {{
                font-size: {FONT_SIZE_LARGE}px;
                color: {colours.text};
                background: {colours.surface_raised};
                border-radius: {RADIUS_SMALL}px;
                padding: 6px;
            }}
            QWidget#separator {{ background: {colours.border_strong}; }}
            QCheckBox {{ spacing: 8px; background: transparent; }}
            QCheckBox::indicator {{
                width: 17px; height: 17px; border-radius: 5px;
                border: 1px solid {colours.border_strong};
                background: {colours.surface_raised};
            }}
            QCheckBox::indicator:checked {{
                background: {colours.accent};
                border-color: {colours.accent};
                image: url({CHECK_ICON});
            }}
            QPushButton {{
                padding: 7px 16px;
                background: {colours.surface};
                color: {colours.text};
                border: 1px solid {colours.border_strong};
                border-radius: {RADIUS_SMALL}px;
            }}
            QPushButton:hover {{ border-color: {colours.accent}; }}
            QPushButton[kind="danger"] {{ color: {colours.danger}; }}
            QPushButton[kind="danger"]:hover {{ border-color: {colours.danger}; }}
            """
        )
        self.resize(1400, 900)

    # ------------------------------------------------------------------- entries

    def reload_entries(self) -> None:
        """Rebuild the list of pending pairs from the queue, keeping the position.

        A batch that arrives while the user is working is simply appended: the
        validation session never stops.
        """
        entries: list[Entry] = []
        for batch in self.queue.batches():
            for pair in batch.pending:
                entries.append(
                    Entry(
                        batch=batch,
                        label=pair.label,
                        page_start=pair.page_start,
                        page_end=pair.page_end,
                        score=pair.score,
                        metrics=dict(pair.metrics),
                    )
                )
        self.entries = entries
        self.position = min(self.position, max(0, len(entries) - 1))
        self.show_current()

    @property
    def current(self) -> Entry | None:
        """Return the pair on screen, or None when the queue is drained."""
        if 0 <= self.position < len(self.entries):
            return self.entries[self.position]
        return None

    def _target_height(self) -> int:
        """Return the height the pages should be drawn at."""
        used = self.header.height() + self.banner.height() + 90
        if self.settings.debug_metrics:
            used += self.metrics_label.height()
        return max(200, self.height() - used)

    def _key_for(self, entry: Entry) -> PairKey:
        """Return the cache key of an entry at the current display size.

        The size asked for is in device pixels, not in logical ones. On a scaled
        display the two differ by the better part of a factor of two, and decoding
        at the logical size would leave Qt to enlarge the result: soft edges,
        exactly where the user is looking for the join.
        """
        height = int(round(self._target_height() * self.devicePixelRatioF()))
        return PairKey(entry.pdf_path, entry.page_start, entry.page_end, height)

    # ------------------------------------------------------------------- display

    def show_current(self) -> None:
        """Draw the current pair, pulling it from the cache."""
        entry = self.current
        if entry is None:
            self._show_summary()
            return

        if self._current_pdf is not None and entry.pdf_path != self._current_pdf:
            self._show_banner(entry.pdf_path.name)
        self._current_pdf = entry.pdf_path

        key = self._key_for(entry)
        pixmaps = self._pixmaps.get(key)
        if pixmaps is None:
            images = self.preloader.get(key) or self.preloader.load_now(key)
            ratio = self.devicePixelRatioF()
            pixmaps = (QPixmap.fromImage(images[0]), QPixmap.fromImage(images[1]))
            for pixmap in pixmaps:
                # The pixmap holds device pixels; this is what tells Qt to draw it
                # at the right logical size instead of twice too big.
                pixmap.setDevicePixelRatio(ratio)
            self._remember(key, pixmaps)
        self.left_view.setPixmap(pixmaps[0])
        self.right_view.setPixmap(pixmaps[1])

        remaining = len(self.entries)
        self.header.setText(
            f"{entry.pdf_path.name}    pages {entry.page_start}-{entry.page_end}"
            f"    {self.position + 1} / {remaining}    continuity {entry.score:.2f}"
        )
        if self.settings.debug_metrics:
            self.metrics_label.setText(
                "   ".join(f"{name} {value:.3f}" for name, value in sorted(entry.metrics.items()))
            )
        self._request_window()

    def _remember(self, key: PairKey, pixmaps: tuple[QPixmap, QPixmap]) -> None:
        """Keep a handful of converted pixmaps, enough for the back step."""
        self._pixmaps[key] = pixmaps
        if len(self._pixmaps) > 8:
            for stale in list(self._pixmaps)[:-8]:
                self._pixmaps.pop(stale, None)

    def _request_window(self) -> None:
        """Ask the preloader for the pairs around the current one.

        Near the end of a batch this naturally reaches into the next book of the
        queue, so moving from one book to the next is instant too.
        """
        height = self._target_height()
        pairs = [(entry.pdf_path, entry.page_start, entry.page_end) for entry in self.entries]
        keys = window_around(
            pairs,
            self.position,
            height,
            ahead=max(2, self.settings.review.preload_ahead),
            behind=max(1, self.settings.review.preload_behind),
        )
        self.preloader.request(keys)

    def _on_preloaded(self, key: PairKey) -> None:
        """A pair finished decoding: draw it if it is the one being waited for."""
        entry = self.current
        if entry is not None and self._key_for(entry) == key and self.left_view.pixmap().isNull():
            self.show_current()

    def _show_banner(self, name: str) -> None:
        """Announce a change of book, briefly and without blocking anything."""
        self.banner.setText(f"Now checking: {name}")
        self.banner.setVisible(True)
        self._banner_timer.start(max(200, self.settings.review.banner_ms))

    def _hide_banner(self) -> None:
        """Hide the transition banner."""
        self.banner.setVisible(False)

    def _show_summary(self) -> None:
        """Show the end of session recap once the queue is empty."""
        self.left_view.clear()
        self.right_view.clear()
        self.separator.setVisible(False)
        total = sum(self.decided.values())
        detail = ", ".join(
            f"{count} {DECISION_NAMES[decision]}"
            for decision, count in self.decided.items()
            if count
        )
        self.header.setText(
            f"Nothing left to check — {total} pair(s) decided"
            + (f": {detail}" if detail else "")
        )
        self.metrics_label.setText("")

    # ------------------------------------------------------------------ decisions

    def decide(self, decision: str) -> None:
        """Record a decision and move straight on to the next pair."""
        entry = self.current
        if entry is None:
            return
        if self.apply_all.isChecked():
            self._apply_to_all(decision)
            return

        self.history.append(
            HistoryStep(position=self.position, entry=entry, decision=decision)
        )
        self._apply(entry, decision)
        self.entries.pop(self.position)
        self.position = min(self.position, max(0, len(self.entries) - 1))
        self._check_batch(entry.batch)
        self.show_current()

    def _apply(self, entry: Entry, decision: str) -> None:
        """Apply one decision to the model and persist it."""
        self.decided[decision] += 1
        if self._record is not None:
            self._record(entry.batch, entry.label, decision)

    def _apply_to_all(self, decision: str) -> None:
        """Apply the same decision to every remaining pair of the queue."""
        for entry in list(self.entries):
            self._apply(entry, decision)
        batches = {entry.batch.pdf_path: entry.batch for entry in self.entries}
        self.entries.clear()
        self.position = 0
        self.apply_all.setChecked(False)
        for batch in batches.values():
            self._check_batch(batch)
        self.show_current()

    def _check_batch(self, batch: ReviewBatch) -> None:
        """Finalise a book once its last pair has been decided.

        Finalisation writes images, so it runs on its own low priority thread: the
        user must not feel it.
        """
        if not batch.finished or self._finalise is None:
            return
        self._finalised.add(batch.pdf_path)
        thread = threading.Thread(
            target=self._finalise, args=(batch,), name="pdfextract-finalise", daemon=True
        )
        thread.start()

    def undo(self) -> None:
        """Step back one decision, restore the pair and forget what was recorded.

        A book whose last pair has been decided is already being finalised on its
        own thread, and its images are being written; stepping back into it would
        mean racing that work, so the step is refused rather than done wrongly.
        """
        if not self.history:
            return
        if self.history[-1].entry.batch.pdf_path in self._finalised:
            return
        step = self.history.pop()
        self.decided[step.decision] = max(0, self.decided[step.decision] - 1)
        if self._record is not None:
            self._record(step.entry.batch, step.entry.label, None)
        self.entries.insert(min(step.position, len(self.entries)), step.entry)
        self.position = min(step.position, len(self.entries) - 1)
        self.show_current()

    # --------------------------------------------------------------------- events

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt naming
        """Keyboard is the primary interface here."""
        if self.banner.isVisible():
            self._hide_banner()
        decision = KEY_DECISIONS.get(int(event.key()))
        key = event.key()
        if decision is not None:
            self.decide(decision)
        elif key == Qt.Key.Key_Backspace:
            self.undo()
        elif key == Qt.Key.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt naming
        """A resize changes the target size, so the cache no longer applies."""
        super().resizeEvent(event)
        self._resize_timer.start()

    def _on_resize_settled(self) -> None:
        """Rebuild the cache once the user has stopped dragging the window edge."""
        self._pixmaps.clear()
        self.preloader.invalidate()
        self.show_current()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt naming
        """Tell the main window that background work should step aside."""
        super().showEvent(event)
        if self._on_focus_changed is not None:
            self._on_focus_changed(True)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        """Give the processing pool its workers back and stop the preloader."""
        if self._on_focus_changed is not None:
            self._on_focus_changed(False)
        self.preloader.stop()
        self.finished.emit()
        super().closeEvent(event)

    def current_images(self) -> tuple[QImage, QImage] | None:
        """Return the images of the current pair, for tests and screenshots."""
        entry = self.current
        if entry is None:
            return None
        return self.preloader.get(self._key_for(entry))
