"""Queue of batches awaiting manual validation.

One batch is the set of ambiguous pairs of one PDF. The queue is filled by the
processing workers and drained by the validation screen, and the two never wait for
each other: a PDF sitting here for a week does not hold up anything.

The queue is plain Python and knows nothing about Qt. Listeners are ordinary
callables; the interface subscribes one that emits a signal.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pdfextract.core.job import ExtractionPlan, ReviewPair

Listener = Callable[["ReviewQueue"], None]


@dataclass
class ReviewBatch:
    """Everything the validation screen needs about one PDF."""

    pdf_path: Path
    plan: ExtractionPlan
    output_directory: Path
    pdf_hash: str = ""
    source_root: Path | None = None
    mode: int = 3
    notes: list[str] = field(default_factory=list)

    @property
    def pending(self) -> list[ReviewPair]:
        """Return the pairs still to decide, in page order."""
        return sorted(self.plan.pending_pairs, key=lambda pair: pair.page_start)

    @property
    def total(self) -> int:
        """Return how many pairs this batch holds in all."""
        return len(self.plan.review)

    @property
    def decided(self) -> int:
        """Return how many pairs have already been decided."""
        return self.total - len(self.pending)

    @property
    def finished(self) -> bool:
        """Return True once every pair of the batch has been decided."""
        return not self.pending


class ReviewQueue:
    """A thread safe FIFO of :class:`ReviewBatch`."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._batches: list[ReviewBatch] = []
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        """Register a callback fired whenever the queue changes."""
        with self._lock:
            self._listeners.append(listener)

    def _notify(self) -> None:
        """Fire the listeners outside the lock, so a slow one cannot block a worker."""
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            listener(self)

    def add(self, batch: ReviewBatch) -> None:
        """Append a batch. A batch arriving during a session is simply queued after."""
        with self._lock:
            existing = self._find(batch.pdf_path)
            if existing is not None:
                self._batches[self._batches.index(existing)] = batch
            else:
                self._batches.append(batch)
        self._notify()

    def _find(self, pdf_path: Path) -> ReviewBatch | None:
        """Return the batch of a given PDF, if it is queued."""
        for batch in self._batches:
            if batch.pdf_path == pdf_path:
                return batch
        return None

    def remove(self, pdf_path: Path) -> ReviewBatch | None:
        """Drop a batch from the queue and return it."""
        with self._lock:
            batch = self._find(pdf_path)
            if batch is not None:
                self._batches.remove(batch)
        if batch is not None:
            self._notify()
        return batch

    def batches(self) -> list[ReviewBatch]:
        """Return the queued batches, in arrival order."""
        with self._lock:
            return list(self._batches)

    def batch_count(self) -> int:
        """Return how many books are waiting."""
        with self._lock:
            return len([batch for batch in self._batches if not batch.finished])

    def pair_count(self) -> int:
        """Return how many pairs are waiting, all books together."""
        with self._lock:
            return sum(len(batch.pending) for batch in self._batches)

    def summary(self) -> str:
        """Return the line shown permanently in the main window."""
        books = self.batch_count()
        pairs = self.pair_count()
        if not pairs:
            return "Nothing to check"
        book_word = "book" if books == 1 else "books"
        pair_word = "pair" if pairs == 1 else "pairs"
        return f"{books} {book_word} to check — {pairs} {pair_word}"

    def is_empty(self) -> bool:
        """Return True when there is nothing left to validate."""
        return self.pair_count() == 0

    def first_pending(self) -> tuple[ReviewBatch, ReviewPair] | None:
        """Return the next pair to show, or None when the queue is drained."""
        with self._lock:
            for batch in self._batches:
                pending = batch.pending
                if pending:
                    return batch, pending[0]
        return None

    def batch_for(self, pdf_path: Path) -> ReviewBatch | None:
        """Return the batch of a given PDF, for the jump to book button."""
        with self._lock:
            return self._find(pdf_path)

    def notify(self) -> None:
        """Fire the listeners after an external change, such as a recorded decision."""
        self._notify()
