"""Overall progress of a batch, and how long it still has to run.

The unit is one page touched once. A book is read twice in modes 2 and 3, once to
score its pairs and once to write the images, so it counts for twice its page
number; in mode 1 there is nothing to score and it counts once. That keeps the bar
moving steadily instead of sitting at zero through the analysis and then jumping.

Files that have not been opened yet have no page count. Rather than leaving them
out, which would make the bar run backwards as they arrive, their pages are
estimated from their size: the pages of a scanned book weigh much the same.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pdfextract.core.job import DROP_BOTH, DROP_LEFT, DROP_RIGHT, ItemKind, JobState
from pdfextract.core.runner import PdfJob

STATE_LABELS = {
    JobState.PENDING: "waiting",
    JobState.ANALYSING: "analysing",
    JobState.PARTIAL_EXPORT: "writing images",
    JobState.AWAITING_VALIDATION: "waiting for you",
    JobState.VALIDATED: "validated",
    JobState.FINALISING: "writing the last images",
    JobState.DONE: "done",
    JobState.FAILED: "failed",
    JobState.CANCELLED: "cancelled",
}

FINISHED_STATES = (JobState.DONE, JobState.FAILED, JobState.CANCELLED)

# Below these, an estimate is more misleading than no estimate at all.
MINIMUM_SAMPLE_SECONDS = 2.0
MINIMUM_SAMPLE_FRACTION = 0.02


@dataclass(frozen=True)
class BatchProgress:
    """What to show at the bottom of the window."""

    fraction: float  # 0.0 to 1.0
    done_units: int
    total_units: int
    estimated: bool  # the total counts files whose page number is only guessed
    files_done: int
    files_total: int
    seconds_left: float | None
    label: str

    @property
    def percent(self) -> int:
        """Return the progress as a whole percentage."""
        return int(round(self.fraction * 100))

    @property
    def running(self) -> bool:
        """Return True while there is still something to do."""
        return self.files_done < self.files_total


def passes_for(mode: int) -> int:
    """Return how many times a book is read from end to end in a given mode."""
    return 1 if mode == 1 else 2


def bytes_per_page(jobs: Sequence[PdfJob]) -> float:
    """Return the average weight of a page, from the files already opened."""
    total_bytes = 0
    total_pages = 0
    for job in jobs:
        if job.page_count and job.size_bytes:
            total_bytes += job.size_bytes
            total_pages += job.page_count
    return total_bytes / total_pages if total_pages else 0.0


def _pages_of(job: PdfJob, weight: float) -> tuple[int, bool]:
    """Return the page count of a job and whether it had to be guessed."""
    if job.page_count:
        return job.page_count, False
    if weight > 0 and job.size_bytes:
        return max(1, int(round(job.size_bytes / weight))), True
    return 0, True


def units_of(job: PdfJob, weight: float = 0.0) -> tuple[int, int, bool]:
    """Return ``(done, total, estimated)`` units for one job."""
    pages, estimated = _pages_of(job, weight)
    passes = passes_for(job.resolved_mode) if job.resolved_mode else 2
    total = pages * passes
    if job.state in FINISHED_STATES:
        return total, total, estimated

    exported = sum(len(written.item.pages) for written in job.written)
    done = min(total, job.analysed_pages + exported) if total else 0
    return done, total, estimated


def format_duration(seconds: float) -> str:
    """Return a rounded, readable duration."""
    seconds = max(0.0, seconds)
    if seconds < 10:
        return "a few seconds"
    if seconds < 90:
        return f"{int(round(seconds / 5.0) * 5)} seconds"
    minutes = seconds / 60.0
    if minutes < 2:
        return "about a minute"
    if minutes < 60:
        return f"{int(round(minutes))} min"
    hours = int(minutes // 60)
    rest = int(round(minutes % 60))
    return f"{hours} h {rest:02d}"


def describe(jobs: Sequence[PdfJob]) -> str:
    """Return one line saying what the batch is doing right now."""
    running = [job for job in jobs if not job.is_finished]
    if not running:
        return "Finished"
    waiting = [job for job in running if job.state is JobState.AWAITING_VALIDATION]
    active = [job for job in running if job.state is not JobState.AWAITING_VALIDATION]
    if not active:
        return f"{len(waiting)} file(s) waiting for you to check them"

    job = active[0]
    state = STATE_LABELS.get(job.state, "working")
    done, total = job.progress
    detail = f" {done}/{total}" if total else ""
    others = len(active) - 1
    suffix = f"  (+{others} more)" if others > 0 else ""
    return f"{job.path.name} — {state}{detail}{suffix}"


def batch_progress(jobs: Sequence[PdfJob], elapsed: float) -> BatchProgress:
    """Return the progress of a whole batch, and an estimate of the time left."""
    if not jobs:
        return BatchProgress(0.0, 0, 0, False, 0, 0, None, "Nothing running")

    weight = bytes_per_page(jobs)
    done_units = 0
    total_units = 0
    estimated = False
    for job in jobs:
        job_done, job_total, job_estimated = units_of(job, weight)
        done_units += job_done
        total_units += job_total
        estimated = estimated or (job_estimated and job_total > 0)

    fraction = min(1.0, max(0.0, done_units / total_units)) if total_units else 0.0
    seconds_left: float | None = None
    if (
        elapsed >= MINIMUM_SAMPLE_SECONDS
        and MINIMUM_SAMPLE_FRACTION <= fraction < 1.0
        and done_units > 0
    ):
        rate = done_units / elapsed
        if rate > 0:
            seconds_left = (total_units - done_units) / rate

    return BatchProgress(
        fraction=fraction,
        done_units=done_units,
        total_units=total_units,
        estimated=estimated,
        files_done=sum(1 for job in jobs if job.is_finished),
        files_total=len(jobs),
        seconds_left=seconds_left,
        label=describe(jobs),
    )


@dataclass(frozen=True)
class PlanTally:
    """What a batch is going to produce, once its books have been analysed.

    Counted in output images rather than in source pages: that is what ends up in
    the folder, and a merged pair is one image, not two.
    """

    single: int  # images made of one page
    merged: int  # images made of two pages joined
    to_check: int  # pairs still waiting for a decision
    dropped: int  # pages the user asked not to export

    @property
    def worth_showing(self) -> bool:
        """Return True when the tally says more than the page count already does."""
        return bool(self.merged or self.to_check or self.dropped)

    @property
    def images(self) -> int:
        """Return how many images the batch will write in all."""
        return self.single + self.merged


def tally(jobs: Sequence[PdfJob]) -> PlanTally:
    """Count what the analysed books will produce, and what is still undecided."""
    single = merged = to_check = dropped = 0
    for job in jobs:
        plan = job.plan
        if plan is None:
            continue
        for item in plan.certain_items + plan.resolve_decisions():
            if item.kind is ItemKind.MERGED:
                merged += 1
            else:
                single += 1
        to_check += len(plan.pending_pairs)
        for pair in plan.review:
            if pair.decision in (DROP_LEFT, DROP_RIGHT):
                dropped += 1
            elif pair.decision == DROP_BOTH:
                dropped += 2
    return PlanTally(single=single, merged=merged, to_check=to_check, dropped=dropped)
