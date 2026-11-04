"""The screen shown while a batch runs.

Once a batch is started, the list of files has nothing left to offer: what one
wants to know is how far along it is, how long it will take and what comes out.
This page takes the whole window for that, around a large progress ring, and keeps
the only actions that still matter at hand: check the pairs already waiting,
pause, cancel.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QConicalGradient, QFont, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from pdfextract.ui.theme import FONT_SIZE, FONT_SIZE_LARGE, FONT_SIZE_SMALL, Palette

RING_SIZE = 230
RING_WIDTH = 14
FRAME_MS = 33  # about thirty frames a second, only while the page is on screen
COMET_DEGREES = 70.0


@dataclass
class WorkView:
    """Everything the page shows, worked out by the main window."""

    fraction: float = 0.0
    title: str = ""
    stage: str = ""
    tally: str = ""
    eta: str = ""
    files: str = ""
    file_lines: list[str] = field(default_factory=list)
    to_check: int = 0
    paused: bool = False
    active: bool = True  # False when only the user's decisions are left


class ProgressRing(QWidget):
    """A ring that fills with the progress, a bright spark running round it.

    The spark is what tells the user the application is alive even when the
    percentage does not move for a while, during the analysis of a heavy page.
    """

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self.fraction = 0.0
        self.active = True
        self._angle = 0.0
        self.setFixedSize(RING_SIZE, RING_SIZE)
        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_MS)
        self._timer.timeout.connect(self._tick)

    def set_fraction(self, fraction: float) -> None:
        """Fill the ring up to a fraction between 0 and 1."""
        self.fraction = max(0.0, min(1.0, fraction))
        self.update()

    def set_active(self, active: bool) -> None:
        """Run the spark while work is going on, stop it otherwise."""
        self.active = active
        self.update()

    def start(self) -> None:
        """Animate; called when the page shows."""
        self._timer.start()

    def stop(self) -> None:
        """Stop animating; nothing should draw on a hidden page."""
        self._timer.stop()

    @property
    def animating(self) -> bool:
        """Return True while the animation timer runs."""
        return self._timer.isActive()

    def _tick(self) -> None:
        if self.active:
            self._angle = (self._angle + 4.0) % 360.0
            self.update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt naming
        colours = self._palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        margin = RING_WIDTH / 2 + 2
        rect = QRectF(margin, margin, self.width() - 2 * margin, self.height() - 2 * margin)

        track = QPen(QColor(colours.surface_raised), RING_WIDTH)
        painter.setPen(track)
        painter.drawEllipse(rect)

        if self.fraction > 0:
            # A conical gradient runs counter-clockwise from its start angle, and the
            # arc runs clockwise from twelve o'clock: the start of the arc sits at
            # position 1.0 of the gradient, its end at 1.0 - fraction.
            gradient = QConicalGradient(rect.center(), 90.0)
            end = max(0.0, 1.0 - self.fraction)
            # The round cap at twelve o'clock pokes just past the seam, into the
            # positions near 0.0: those take the starting colour too.
            gradient.setColorAt(0.0, QColor(colours.accent))
            if end > 0.04:
                gradient.setColorAt(0.03, QColor(colours.accent_2))
            gradient.setColorAt(end, QColor(colours.accent_2))
            gradient.setColorAt(1.0, QColor(colours.accent))
            pen = QPen(gradient, RING_WIDTH)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            # Qt angles are in sixteenths of a degree, counter-clockwise from three
            # o'clock: start at twelve and run clockwise.
            painter.drawArc(rect, 90 * 16, -int(self.fraction * 360 * 16))

        if self.active:
            # A comet running clockwise: a bright head, a tail fading behind it.
            head = 90.0 - self._angle
            bright = QColor(colours.text)
            clear = QColor(colours.text)
            bright.setAlphaF(0.85)
            clear.setAlphaF(0.0)
            tail = QConicalGradient(rect.center(), head)
            tail.setColorAt(0.0, bright)
            tail.setColorAt(COMET_DEGREES / 360.0, clear)
            tail.setColorAt(1.0, clear)
            pen = QPen(tail, RING_WIDTH * 0.45)
            pen.setCapStyle(Qt.PenCapStyle.FlatCap)
            painter.setPen(pen)
            painter.drawArc(rect, int(head * 16), int(COMET_DEGREES * 16))

        painter.setPen(QColor(colours.text))
        font = QFont(self.font())
        font.setPixelSize(46)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"{math.floor(self.fraction * 100)}%")
        painter.end()


class WorkPage(QWidget):
    """Progress of the running batch, full window."""

    cancel_requested = Signal()
    pause_requested = Signal()
    review_requested = Signal()

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 24, 32, 32)
        outer.addStretch(1)

        column = QVBoxLayout()
        column.setSpacing(10)
        column.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        self.ring = ProgressRing(palette, self)
        column.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addSpacing(14)

        self.title = self._label(f"font-size: {FONT_SIZE_LARGE + 5}px; font-weight: 600;")
        self.stage = self._label(f"font-size: {FONT_SIZE_LARGE}px; color: {palette.text_muted};")
        self.tally = self._label(f"font-size: {FONT_SIZE}px; color: {palette.text};")
        self.eta = self._label(f"font-size: {FONT_SIZE}px; color: {palette.text_muted};")
        self.files = self._label(f"font-size: {FONT_SIZE_SMALL}px; color: {palette.text_faint};")
        self.file_list = self._label(
            f"font-size: {FONT_SIZE_SMALL}px; color: {palette.text_muted};"
        )
        self.file_list.setTextFormat(Qt.TextFormat.PlainText)
        for widget in (self.title, self.stage, self.tally, self.eta, self.files):
            column.addWidget(widget)
        column.addSpacing(6)
        column.addWidget(self.file_list)
        column.addSpacing(18)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        self.review_button = QPushButton("", self)
        self.review_button.setProperty("kind", "primary")
        self.review_button.clicked.connect(self.review_requested)
        self.pause_button = QPushButton("Pause", self)
        self.pause_button.clicked.connect(self.pause_requested)
        self.cancel_button = QPushButton("Cancel", self)
        self.cancel_button.setProperty("kind", "danger")
        self.cancel_button.clicked.connect(self.cancel_requested)
        for button in (self.review_button, self.pause_button, self.cancel_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        column.addLayout(buttons)

        outer.addLayout(column)
        outer.addStretch(1)

    def _label(self, style: str) -> QLabel:
        label = QLabel("", self)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        label.setStyleSheet(style + " background: transparent;")
        return label

    def show_view(self, view: WorkView) -> None:
        """Draw a state of the batch."""
        self.ring.set_fraction(view.fraction)
        self.ring.set_active(view.active and not view.paused)
        self.title.setText(view.title)
        for label, text in (
            (self.stage, view.stage),
            (self.tally, view.tally),
            (self.eta, view.eta),
            (self.files, view.files),
            (self.file_list, "\n".join(view.file_lines)),
        ):
            label.setText(text)
            label.setVisible(bool(text))
        if view.to_check:
            word = "pair" if view.to_check == 1 else "pairs"
            self.review_button.setText(f"Check {view.to_check} {word} now")
        self.review_button.setVisible(bool(view.to_check))
        self.pause_button.setText("Resume" if view.paused else "Pause")
        self.pause_button.setVisible(view.active)

    def showEvent(self, event: object) -> None:  # noqa: N802 - Qt naming
        """Animate only while on screen."""
        super().showEvent(event)  # type: ignore[arg-type]
        self.ring.start()

    def hideEvent(self, event: object) -> None:  # noqa: N802 - Qt naming
        """Leave the processor alone once the page is gone."""
        super().hideEvent(event)  # type: ignore[arg-type]
        self.ring.stop()
