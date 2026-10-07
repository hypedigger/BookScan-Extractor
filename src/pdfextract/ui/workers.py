"""Bridges between the Qt world and the engine.

The engine runs on plain threads and knows nothing about Qt. These adapters turn
its callbacks into signals, which Qt delivers to the interface thread on its own,
and supply the platform specific bits the engine deliberately does not carry.
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from pdfextract.core.document import iter_pdf_files
from pdfextract.core.review_queue import ReviewQueue
from pdfextract.core.runner import BatchRunner, PdfJob

# Windows thread priority constants, from processthreadsapi.h.
_THREAD_PRIORITY_BELOW_NORMAL = -1
_THREAD_PRIORITY_LOWEST = -2


def lower_current_thread_priority() -> None:
    """Drop the calling thread below normal priority.

    The extraction pool must never win the processor against the validation
    screen. The engine stays free of Qt and of platform calls, so the interface
    hands it this hook instead.
    """
    if sys.platform != "win32":
        return
    try:
        handle = ctypes.windll.kernel32.GetCurrentThread()
        ctypes.windll.kernel32.SetThreadPriority(handle, _THREAD_PRIORITY_BELOW_NORMAL)
    except Exception:
        pass  # a priority that cannot be set is not worth failing over


class RunnerBridge(QObject):
    """Re-emits engine callbacks as Qt signals, on the interface thread."""

    job_changed = Signal(object)  # PdfJob
    queue_changed = Signal(object)  # ReviewQueue

    def __init__(
        self, runner: BatchRunner, queue: ReviewQueue, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.runner = runner
        self.queue = queue
        runner.subscribe(self._on_job)
        queue.subscribe(self._on_queue)

    def _on_job(self, job: PdfJob) -> None:
        """Called from a worker thread: Qt queues the delivery to the main thread."""
        self.job_changed.emit(job)

    def _on_queue(self, queue: ReviewQueue) -> None:
        """Called from a worker thread when the validation queue changes."""
        self.queue_changed.emit(queue)


class FolderScanner(QThread):
    """Walks folders looking for PDF files, off the interface thread.

    A folder on a network share, or one holding a few thousand files, takes long
    enough that doing this on the interface thread freezes the window with no
    explanation. Here it reports as it goes instead.
    """

    counted = Signal(int)  # how many PDF files found so far
    found = Signal(list, object)  # the files, and the folder they came from

    def __init__(
        self, folders: list[Path], recursive: bool = True, parent: QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.folders = list(folders)
        self.recursive = recursive
        self._cancelled = False

    def cancel(self) -> None:
        """Ask the walk to stop; its result is then dropped."""
        self._cancelled = True

    def run(self) -> None:
        """Walk the folders, reporting the count every so often."""
        files: list[Path] = []
        for folder in self.folders:
            for path in iter_pdf_files(folder, recursive=self.recursive, sort=False):
                if self._cancelled:
                    return
                files.append(path)
                if len(files) % 25 == 0:
                    self.counted.emit(len(files))
        if self._cancelled:
            return
        root = self.folders[0] if len(self.folders) == 1 else None
        self.counted.emit(len(files))
        self.found.emit(sorted(files), root)
