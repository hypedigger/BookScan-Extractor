"""Application start-up."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from pdfextract.core import sidecar
from pdfextract.core.applog import get_logger
from pdfextract.ui.main_window import MainWindow

ICON_PATH = Path(__file__).resolve().parents[3] / "assets" / "app.ico"


def restore_pending(window: MainWindow) -> int:
    """Put the books that were still awaiting validation back in the queue.

    Their decisions live in the sidecar files, so re-analysing them restores both
    what was already decided and what is still to decide.
    """
    sources = sidecar.pending_sources()
    if sources:
        window.runner.submit(sources)
        window.statusBar().showMessage(
            f"{len(sources)} book(s) still awaiting validation were reloaded", 8000
        )
    return len(sources)


def run_gui(argv: list[str] | None = None) -> int:
    """Start the graphical interface and return the exit code."""
    get_logger().info("application started")
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("BookScan Extractor")
    app.setOrganizationName("pdf-image-extractor")
    if ICON_PATH.is_file():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    window = MainWindow()
    window.show()
    restore_pending(window)
    return int(app.exec())
