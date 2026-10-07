"""Look ahead image loader for the validation screen.

The whole design of the validation screen follows from one requirement: the user
must be able to take decisions at the speed of the keyboard. So the pairs around
the current one are decoded and scaled ahead of time, on a thread that runs at a
higher priority than the extraction pool, and the screen only ever pulls an image
that is already in the cache.

Qt forbids building a ``QPixmap`` outside the GUI thread, so the cache holds
``QImage`` objects, already scaled to the size the screen will draw them at. The
conversion left to the GUI thread is then a cheap one.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtGui import QImage

from pdfextract.core.document import PdfDocument
from pdfextract.core.extractor import render_for_height

MINIMUM_HEIGHT = 120


@dataclass(frozen=True)
class PairKey:
    """Identifies one pair of pages at one display size."""

    pdf_path: Path
    left_page: int  # one based
    right_page: int
    height: int  # target height in pixels

    def pages(self) -> tuple[int, int]:
        """Return the two source pages."""
        return self.left_page, self.right_page


def to_qimage(array: np.ndarray) -> QImage:
    """Convert a BGR array to a QImage that owns its buffer.

    ``copy()`` is not optional: the numpy buffer is released as soon as the worker
    moves on, and a QImage pointing at freed memory crashes later, somewhere else.
    """
    rgb = np.ascontiguousarray(array[:, :, ::-1])
    height, width, _ = rgb.shape
    image = QImage(rgb.data, width, height, 3 * width, QImage.Format.Format_RGB888)
    return image.copy()


class PreloadCache:
    """A bounded least recently used cache of decoded pairs."""

    def __init__(self, limit_mb: int = 512) -> None:
        self._limit = max(32, limit_mb) * 1024 * 1024
        self._entries: OrderedDict[PairKey, tuple[QImage, QImage]] = OrderedDict()
        self._bytes = 0
        self._lock = threading.RLock()

    @staticmethod
    def _size_of(pair: tuple[QImage, QImage]) -> int:
        """Return the memory a cached pair occupies."""
        return int(pair[0].sizeInBytes()) + int(pair[1].sizeInBytes())

    def get(self, key: PairKey) -> tuple[QImage, QImage] | None:
        """Return a cached pair and mark it as recently used."""
        with self._lock:
            pair = self._entries.get(key)
            if pair is not None:
                self._entries.move_to_end(key)
            return pair

    def put(self, key: PairKey, pair: tuple[QImage, QImage]) -> None:
        """Store a pair, evicting the least recently used ones if needed."""
        with self._lock:
            if key in self._entries:
                self._bytes -= self._size_of(self._entries[key])
            self._entries[key] = pair
            self._entries.move_to_end(key)
            self._bytes += self._size_of(pair)
            while self._bytes > self._limit and len(self._entries) > 1:
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= self._size_of(evicted)

    def clear(self) -> None:
        """Empty the cache, after a window resize for instance."""
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._entries

    @property
    def used_bytes(self) -> int:
        """Return how much memory the cache currently holds."""
        with self._lock:
            return self._bytes

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class Preloader(QThread):
    """Decodes the pairs around the current one, ahead of the user.

    The thread runs at high priority on purpose: without it the display stutters
    exactly when the batch is working, which is to say all the time.
    """

    ready = Signal(object)  # PairKey

    def __init__(self, cache: PreloadCache | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.cache = cache or PreloadCache()
        self._wanted: list[PairKey] = []
        self._condition = threading.Condition()
        self._stopping = False
        self._documents: OrderedDict[Path, PdfDocument] = OrderedDict()
        self._document_limit = 4

    def request(self, keys: list[PairKey]) -> None:
        """Set the look ahead window, in priority order. Replaces the previous one."""
        with self._condition:
            self._wanted = [key for key in keys if key not in self.cache]
            self._condition.notify_all()

    def get(self, key: PairKey) -> tuple[QImage, QImage] | None:
        """Return a pair if it is already decoded."""
        return self.cache.get(key)

    def load_now(self, key: PairKey) -> tuple[QImage, QImage]:
        """Decode a pair on the calling thread, for a cache miss on screen."""
        pair = self.cache.get(key)
        if pair is None:
            pair = self._decode(key)
            self.cache.put(key, pair)
        return pair

    def invalidate(self) -> None:
        """Drop everything: the display size changed, the cache no longer applies."""
        self.cache.clear()

    def stop(self) -> None:
        """Ask the thread to finish and wait for it."""
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self.wait(3000)
        self._close_documents()

    def _close_documents(self) -> None:
        """Close the documents kept open for decoding."""
        for document in self._documents.values():
            document.close()
        self._documents.clear()

    def _document(self, path: Path) -> PdfDocument:
        """Return an open document, keeping a few of them to avoid reopening."""
        document = self._documents.get(path)
        if document is None:
            document = PdfDocument(path)
            document.open()
            self._documents[path] = document
            while len(self._documents) > self._document_limit:
                _, evicted = self._documents.popitem(last=False)
                evicted.close()
        else:
            self._documents.move_to_end(path)
        return document

    def _decode(self, key: PairKey) -> tuple[QImage, QImage]:
        """Render and scale one pair."""
        document = self._document(key.pdf_path)
        height = max(MINIMUM_HEIGHT, key.height)
        left = render_for_height(document.load_page(key.left_page - 1), height)
        right = render_for_height(document.load_page(key.right_page - 1), height)
        return to_qimage(left), to_qimage(right)

    def run(self) -> None:
        while True:
            with self._condition:
                while not self._stopping and not self._wanted:
                    self._condition.wait(0.2)
                if self._stopping:
                    break
                key = self._wanted.pop(0)
            if key in self.cache:
                continue
            try:
                pair = self._decode(key)
            except Exception:
                continue  # a page that cannot be drawn must not kill the preloader
            self.cache.put(key, pair)
            self.ready.emit(key)
        self._close_documents()


def window_around(
    pairs: list[tuple[Path, int, int]], position: int, height: int, ahead: int, behind: int
) -> list[PairKey]:
    """Return the keys to keep decoded around a position, nearest first.

    Looking ahead is what makes the next decision instant; keeping a couple of
    pairs behind is what makes the back step instant too.
    """
    keys: list[PairKey] = []
    for distance in range(0, max(ahead, behind) + 1):
        for step in ((0,) if distance == 0 else (distance, -distance)):
            if step > 0 and step > ahead:
                continue
            if step < 0 and -step > behind:
                continue
            index = position + step
            if 0 <= index < len(pairs):
                path, left, right = pairs[index]
                keys.append(PairKey(path, left, right, height))
    return keys
