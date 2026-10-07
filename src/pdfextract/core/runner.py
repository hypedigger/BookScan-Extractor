"""Execution of the job life cycle, and the batch runner that drives it.

The rule this module exists to enforce: manual validation never blocks processing.
A PDF that needs the user produces everything it can, hands its ambiguous pairs to
the validation queue, and releases its worker. Nothing waits for a human.

The specification lists ``job.py`` for the job model; the execution lives here so
that the model stays importable by ``sidecar.py`` without a circular import.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from os import cpu_count
from pathlib import Path

from pdfextract.core import naming, sidecar
from pdfextract.core.applog import get_logger
from pdfextract.core.document import PdfDocument, PdfDocumentError
from pdfextract.core.job import (
    ExtractionPlan,
    JobState,
    WrittenFile,
    build_single_plan,
    write_items,
)
from pdfextract.core.review_queue import ReviewBatch, ReviewQueue
from pdfextract.core.settings import AppSettings
from pdfextract.core.trash import TrashOutcome, move_to_trash

CancelCheck = Callable[[], bool]
JobListener = Callable[["PdfJob"], None]


@dataclass
class PdfJob:
    """One PDF making its way through the state machine."""

    path: Path
    mode: int | None = None  # overrides the global mode for this file only
    force_parity: int | None = None  # mode 2 only: imposes where the pairing starts
    state: JobState = JobState.PENDING
    plan: ExtractionPlan | None = None
    written: list[WrittenFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    output_directory: Path | None = None
    source_root: Path | None = None
    pdf_hash: str = ""
    page_count: int = 0
    size_bytes: int = 0
    analysed_pages: int = 0  # pages already scored, for the overall progress bar
    resolved_mode: int = 0  # filled once the job knows which mode applies to it
    progress: tuple[int, int] = (0, 0)
    trash_outcome: TrashOutcome | None = None
    restored_paths: list[Path] = field(default_factory=list)

    @property
    def pending_count(self) -> int:
        """Return how many pairs of this file are awaiting validation."""
        return len(self.plan.pending_pairs) if self.plan else 0

    @property
    def written_paths(self) -> list[Path]:
        """Return every path written for this file, this session or a previous one.

        A book validated over several sessions has part of its images already on
        disk, and the recycle bin guard counts files, so those have to be included.
        """
        current = [item.path for item in self.written]
        return current + [path for path in self.restored_paths if path not in current]

    @property
    def is_finished(self) -> bool:
        """Return True when nothing more will happen to this job."""
        return self.state in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


def _file_size(path: Path) -> int:
    """Return the size of a file, or zero when it cannot be read."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def resolve_mode(job: PdfJob, settings: AppSettings) -> int:
    """Return the mode to use for a file: its own override, or the global one."""
    return job.mode if job.mode in (1, 2, 3) else settings.mode


def build_plan_for(
    document: PdfDocument,
    mode: int,
    settings: AppSettings,
    force_parity: int | None = None,
    scored: Callable[[int, int], None] | None = None,
) -> ExtractionPlan:
    """Build the plan of a document for a given mode.

    ``scored`` is called as the pairs are scored, which is the slow half of modes
    2 and 3 and would otherwise look like the application doing nothing.
    """
    if mode == 1:
        return build_single_plan(document.page_count)
    from pdfextract.core.pairing import build_plan

    scoped = _with_mode(settings, mode)
    report = None
    if scored is not None:
        def report(done: int, total: int, score: object) -> None:
            scored(done, total)

    return build_plan(document, scoped, progress=report, force_parity=force_parity)


def _with_mode(settings: AppSettings, mode: int) -> AppSettings:
    """Return a shallow copy of the settings carrying a different mode."""
    import copy

    scoped = copy.copy(settings)
    scoped.mode = mode
    return scoped


def analyse_and_export(
    job: PdfJob,
    settings: AppSettings,
    queue: ReviewQueue | None = None,
    progress: Callable[[PdfJob], None] | None = None,
    cancelled: CancelCheck | None = None,
) -> PdfJob:
    """Analyse a PDF, export everything that needs no decision, then release.

    This is the part that runs on a worker. It returns as soon as the ambiguous
    pairs have been handed over, whatever remains to be decided.
    """
    logger = get_logger()
    mode = resolve_mode(job, settings)
    scoped = _with_mode(settings, mode)
    output_root = Path(settings.output.root or "out").expanduser()

    try:
        with PdfDocument(job.path) as document:
            job.state = JobState.ANALYSING
            job.page_count = document.page_count
            job.pdf_hash = document.file_hash()
            if progress is not None:
                progress(job)

            job.resolved_mode = mode
            job.size_bytes = job.size_bytes or _file_size(job.path)

            def scored(done: int, total: int) -> None:
                job.analysed_pages = min(job.page_count, done + 1)
                job.progress = (done, total)
                if progress is not None:
                    progress(job)

            plan = build_plan_for(document, mode, scoped, job.force_parity, scored)
            job.analysed_pages = job.page_count
            job.plan = plan

            stored = sidecar.load(job.path)
            if stored is not None and sidecar.apply_to_plan(stored, plan, job.pdf_hash):
                logger.info(
                    "restored %d decision(s) for %s", stored.decided_count, job.path.name
                )

            job.output_directory = naming.output_directory(
                output_root,
                job.path,
                job.source_root,
                settings.output.subfolder_per_pdf,
                settings.output.mirror_source_tree,
            )

            job.state = JobState.PARTIAL_EXPORT
            if progress is not None:
                progress(job)
            _write(job, document, plan.certain_items, scoped, cancelled, progress)
            if cancelled is not None and cancelled():
                job.state = JobState.CANCELLED
                return job

            if plan.pending_pairs:
                job.state = JobState.AWAITING_VALIDATION
                _save_sidecar(job)
                sidecar.remember_pending(job.path)
                if queue is not None:
                    queue.add(
                        ReviewBatch(
                            pdf_path=job.path,
                            plan=plan,
                            output_directory=job.output_directory,
                            pdf_hash=job.pdf_hash,
                            source_root=job.source_root,
                            mode=mode,
                            notes=list(plan.notes),
                        )
                    )
                if progress is not None:
                    progress(job)
                return job  # the worker is released here, nothing waits for a human

            finalise(job, settings, document=document, progress=progress)
    except PdfDocumentError as exc:
        job.errors.append(str(exc))
        job.state = JobState.FAILED
        logger.error("%s: %s", job.path.name, exc)
        if progress is not None:
            progress(job)
    return job


def finalise(
    job: PdfJob,
    settings: AppSettings,
    document: PdfDocument | None = None,
    progress: Callable[[PdfJob], None] | None = None,
) -> PdfJob:
    """Export the decided pairs, then close the job and apply the recycle bin option."""
    plan = job.plan
    if plan is None or job.output_directory is None:
        return job
    scoped = _with_mode(settings, resolve_mode(job, settings))
    job.state = JobState.FINALISING
    if progress is not None:
        progress(job)

    owned = document is None
    handle = document or PdfDocument(job.path)
    try:
        if owned:
            handle.open()
        _write(job, handle, plan.resolve_decisions(), scoped, None, progress)
    except PdfDocumentError as exc:
        job.errors.append(str(exc))
        job.state = JobState.FAILED
        _save_sidecar(job)
        return job
    finally:
        if owned:
            handle.close()

    job.state = JobState.DONE if not job.errors else JobState.FAILED
    _save_sidecar(job)
    sidecar.forget_pending(job.path)

    if settings.trash.enabled and job.state is JobState.DONE:
        job.trash_outcome = move_to_trash(
            job.path,
            plan,
            job.written_paths,
            job.output_directory,
            job.state,
            job.errors,
            trash_sidecar=settings.trash.trash_sidecar,
        )
    if progress is not None:
        progress(job)
    return job


def _write(
    job: PdfJob,
    document: PdfDocument,
    items: list,
    settings: AppSettings,
    cancelled: CancelCheck | None,
    progress: Callable[[PdfJob], None] | None,
) -> None:
    """Write a list of items, updating the job as it goes."""
    if not items or job.output_directory is None:
        return
    taken = set(job.written_paths)
    total = len(items)
    for position, written in enumerate(
        write_items(document, items, job.output_directory, settings, taken=taken)
    ):
        job.written.append(written)
        job.progress = (position + 1, total)
        if progress is not None:
            progress(job)
        if cancelled is not None and cancelled():
            return


def _save_sidecar(job: PdfJob) -> None:
    """Persist the state of a job next to its PDF."""
    if job.plan is None:
        return
    record = sidecar.from_plan(
        job.path,
        job.pdf_hash,
        job.plan,
        job.state.value,
        job.output_directory,
        job.written_paths,
    )
    sidecar.save(job.path, record)


def record_decision(
    batch: ReviewBatch, pair_label: str, decision: str | None, offset: int | None = None
) -> None:
    """Apply a user decision to a batch and flush it to disk straight away.

    ``decision`` of None forgets the decision, which is what the back step of the
    validation screen needs.
    """
    for pair in batch.plan.review:
        if pair.label == pair_label:
            pair.decision = decision
            if offset is not None:
                pair.vertical_offset = offset
            break
    record = sidecar.load(batch.pdf_path) or sidecar.from_plan(
        batch.pdf_path,
        batch.pdf_hash,
        batch.plan,
        JobState.AWAITING_VALIDATION.value,
        batch.output_directory,
    )
    record.decisions = {
        pair.label: pair.decision
        for pair in batch.plan.review
        if pair.decision is not None
    }
    record.offsets = {pair.label: pair.vertical_offset for pair in batch.plan.review}
    record.mode = batch.plan.mode
    sidecar.save(batch.pdf_path, record)


class BatchRunner:
    """Runs jobs on a bounded pool of workers, with pause, throttle and cancel.

    Interactivity wins: the pool runs at low priority in the interface, and the
    throttle drops it to a single worker while the user is validating.
    """

    def __init__(
        self,
        settings: AppSettings,
        queue: ReviewQueue | None = None,
        max_workers: int | None = None,
        worker_hook: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.queue = queue
        # Called once at the start of every worker thread. The interface uses it to
        # drop the thread to a low OS priority: the core stays free of Qt and of any
        # platform specific call.
        self.worker_hook = worker_hook
        configured = max_workers or settings.max_workers
        self.max_workers = configured if configured > 0 else max(1, (cpu_count() or 2) - 1)
        self.jobs: list[PdfJob] = []
        self._pending: deque[PdfJob] = deque()
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._resume = threading.Event()
        self._resume.set()
        self._stop = threading.Event()
        self._allowed = self.max_workers
        self._running = 0
        self._threads: list[threading.Thread] = []
        self._listeners: list[JobListener] = []
        self._cancelled: set[Path] = set()

    def subscribe(self, listener: JobListener) -> None:
        """Register a callback fired whenever a job changes."""
        self._listeners.append(listener)

    def _notify(self, job: PdfJob) -> None:
        for listener in list(self._listeners):
            listener(job)

    def submit(
        self,
        paths: list[Path],
        source_root: Path | None = None,
        mode: int | None = None,
        force_parity: int | None = None,
    ) -> list[PdfJob]:
        """Add files to the queue, in the order given."""
        created: list[PdfJob] = []
        with self._condition:
            for path in paths:
                job = PdfJob(
                    path=path, source_root=source_root, mode=mode, force_parity=force_parity
                )
                job.size_bytes = _file_size(path)
                job.resolved_mode = mode if mode in (1, 2, 3) else self.settings.mode
                self.jobs.append(job)
                self._pending.append(job)
                created.append(job)
            self._condition.notify_all()
        for job in created:
            self._notify(job)
        return created

    def reorder(self, ordered: list[Path]) -> None:
        """Reorder the waiting queue, for the drag and drop of the queue panel."""
        with self._condition:
            index = {path: position for position, path in enumerate(ordered)}
            items = sorted(self._pending, key=lambda job: index.get(job.path, 1 << 30))
            self._pending = deque(items)

    def cancel(self, path: Path) -> None:
        """Cancel one file, whether it is waiting or running."""
        with self._condition:
            self._cancelled.add(path)
            for job in list(self._pending):
                if job.path == path:
                    self._pending.remove(job)
                    job.state = JobState.CANCELLED
                    self._notify(job)

    def cancel_all(self) -> None:
        """Cancel everything still to do."""
        with self._condition:
            for job in list(self._pending):
                job.state = JobState.CANCELLED
                self._notify(job)
            self._pending.clear()
            self._cancelled.update(job.path for job in self.jobs if not job.is_finished)

    def pause(self) -> None:
        """Stop picking up new files. A running file finishes."""
        self._resume.clear()

    def resume(self) -> None:
        """Carry on."""
        self._resume.set()
        with self._condition:
            self._condition.notify_all()

    @property
    def paused(self) -> bool:
        """Return True when the runner is on hold."""
        return not self._resume.is_set()

    def throttle(self, active: bool) -> None:
        """Reduce the pool to a single worker while the user is validating."""
        with self._condition:
            self._allowed = 1 if active else self.max_workers
            self._condition.notify_all()

    def start(self) -> None:
        """Start the worker threads."""
        if self._threads:
            return
        self._stop.clear()
        for index in range(self.max_workers):
            thread = threading.Thread(
                target=self._worker, name=f"pdfextract-worker-{index}", daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the workers to finish and wait for them."""
        self._stop.set()
        self._resume.set()
        with self._condition:
            self._condition.notify_all()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    def wait_until_idle(self, timeout: float = 120.0) -> bool:
        """Block until nothing is queued or running. Used by the command line."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._condition:
                if not self._pending and self._running == 0:
                    return True
            time.sleep(0.02)
        return False

    def _next_job(self) -> PdfJob | None:
        """Take the next job, waiting for one if the queue is momentarily empty."""
        with self._condition:
            while not self._stop.is_set():
                if self._resume.is_set() and self._pending and self._running < self._allowed:
                    self._running += 1
                    return self._pending.popleft()
                self._condition.wait(timeout=0.1)
        return None

    def _cancel_check(self, job: PdfJob) -> CancelCheck:
        """Return the test a running job uses to notice it has been cancelled."""

        def cancelled() -> bool:
            return job.path in self._cancelled or self._stop.is_set()

        return cancelled

    def _worker(self) -> None:
        """Worker loop: take a job, run it, release the slot."""
        if self.worker_hook is not None:
            self.worker_hook()
        while not self._stop.is_set():
            job = self._next_job()
            if job is None:
                return
            try:
                analyse_and_export(
                    job,
                    self.settings,
                    queue=self.queue,
                    progress=self._notify,
                    cancelled=self._cancel_check(job),
                )
            except Exception as exc:  # a broken file must never stop the queue
                job.errors.append(str(exc))
                job.state = JobState.FAILED
                get_logger().exception("unexpected failure on %s", job.path)
                self._notify(job)
            finally:
                with self._condition:
                    self._running -= 1
                    self._condition.notify_all()

    def finalise_batch(self, batch: ReviewBatch) -> PdfJob | None:
        """Finalise the job of a batch once the user has decided everything."""
        job = next((item for item in self.jobs if item.path == batch.pdf_path), None)
        if job is None:
            job = PdfJob(path=batch.pdf_path, plan=batch.plan, mode=batch.mode)
            job.output_directory = batch.output_directory
            job.pdf_hash = batch.pdf_hash
            stored = sidecar.load(batch.pdf_path)
            if stored is not None:
                # The partial export happened in an earlier session: its files count
                # towards the guard that protects the source from the recycle bin.
                job.restored_paths = [Path(item) for item in stored.written]
            self.jobs.append(job)
        job.plan = batch.plan
        job.state = JobState.VALIDATED
        self._notify(job)
        finalise(job, self.settings, progress=self._notify)
        if self.queue is not None:
            self.queue.remove(batch.pdf_path)
        return job
