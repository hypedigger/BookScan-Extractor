"""Tests of the overall progress report and of its estimate."""

from __future__ import annotations

from pathlib import Path

import pytest

from pdfextract.core.job import ItemKind, JobState, PlanItem, WrittenFile
from pdfextract.core.progress import (
    batch_progress,
    bytes_per_page,
    format_duration,
    passes_for,
    units_of,
)
from pdfextract.core.runner import PdfJob


def _job(name="book.pdf", pages=100, mode=3, size=1_000_000, **kwargs) -> PdfJob:
    job = PdfJob(path=Path(name))
    job.page_count = pages
    job.resolved_mode = mode
    job.size_bytes = size
    for key, value in kwargs.items():
        setattr(job, key, value)
    return job


def _written(page_start: int, page_end: int | None = None) -> WrittenFile:
    item = PlanItem(
        page_start=page_start,
        page_end=page_end,
        kind=ItemKind.MERGED if page_end else ItemKind.SINGLE,
    )
    return WrittenFile(path=Path("out.jpg"), item=item, passthrough=True, reason="native")


def test_a_book_is_read_twice_when_its_pairs_have_to_be_scored():
    assert passes_for(1) == 1
    assert passes_for(2) == 2
    assert passes_for(3) == 2


def test_progress_moves_during_the_analysis():
    """Otherwise the bar sits at zero through the slowest half of the work."""
    job = _job(pages=100, mode=3, analysed_pages=50)
    done, total, _ = units_of(job)
    assert total == 200
    assert done == 50


def test_progress_counts_the_pages_written():
    job = _job(pages=10, mode=3, analysed_pages=10)
    job.written = [_written(1, 2), _written(3, 4)]
    done, _total, _ = units_of(job)
    assert done == 10 + 4


def test_a_finished_job_is_complete_whatever_it_wrote():
    job = _job(pages=10, mode=3)
    job.state = JobState.DONE
    done, total, _ = units_of(job)
    assert done == total == 20


def test_a_failed_job_does_not_hold_the_bar_back():
    job = _job(pages=10, mode=1)
    job.state = JobState.FAILED
    done, total, _ = units_of(job)
    assert done == total == 10


def test_pages_of_an_unopened_file_are_estimated_from_its_size():
    opened = _job("a.pdf", pages=100, size=1_000_000)
    waiting = PdfJob(path=Path("b.pdf"))
    waiting.size_bytes = 500_000
    waiting.resolved_mode = 3

    weight = bytes_per_page([opened, waiting])
    assert weight == pytest.approx(10_000)
    _, total, estimated = units_of(waiting, weight)
    assert total == 100  # 50 estimated pages, read twice
    assert estimated


def test_the_report_marks_itself_as_an_estimate():
    opened = _job("a.pdf", pages=100, size=1_000_000, analysed_pages=100)
    waiting = PdfJob(path=Path("b.pdf"))
    waiting.size_bytes = 1_000_000
    waiting.resolved_mode = 3
    report = batch_progress([opened, waiting], elapsed=10.0)
    assert report.estimated
    assert 0.0 < report.fraction < 1.0


def test_the_time_left_falls_as_the_work_advances():
    job = _job(pages=100, mode=3, analysed_pages=50)
    early = batch_progress([job], elapsed=10.0)
    job.analysed_pages = 100
    late = batch_progress([job], elapsed=12.0)
    assert early.seconds_left is not None
    assert late.seconds_left is not None
    assert late.seconds_left < early.seconds_left


def test_no_estimate_is_given_too_early():
    """A guess made from two hundred milliseconds of work is worse than none."""
    job = _job(pages=1000, mode=3, analysed_pages=1)
    assert batch_progress([job], elapsed=0.5).seconds_left is None


def test_a_finished_batch_reports_no_time_left():
    job = _job(pages=10, mode=1)
    job.state = JobState.DONE
    report = batch_progress([job], elapsed=30.0)
    assert report.fraction == 1.0
    assert report.seconds_left is None
    assert not report.running
    assert report.label == "Finished"


def test_the_label_says_which_file_and_what_it_is_doing():
    job = _job("Flore des Alpes.pdf", pages=100)
    job.state = JobState.ANALYSING
    job.progress = (12, 99)
    report = batch_progress([job], elapsed=5.0)
    assert "Flore des Alpes.pdf" in report.label
    assert "analysing" in report.label
    assert "12/99" in report.label


def test_the_label_counts_the_other_running_files():
    first = _job("a.pdf")
    first.state = JobState.ANALYSING
    second = _job("b.pdf")
    second.state = JobState.PARTIAL_EXPORT
    assert "+1 more" in batch_progress([first, second], elapsed=1.0).label


def test_files_waiting_for_the_user_are_reported_as_such():
    job = _job("a.pdf")
    job.state = JobState.AWAITING_VALIDATION
    assert "waiting for you" in batch_progress([job], elapsed=1.0).label


def test_an_empty_batch_says_nothing_is_running():
    assert batch_progress([], elapsed=0.0).label == "Nothing running"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(3, "a few seconds"), (42, "40 seconds"), (100, "about a minute"),
     (200, "3 min"), (3700, "1 h 02")],
)
def test_durations_are_rounded_to_something_readable(seconds, expected):
    assert format_duration(seconds) == expected


def _plan_with(items, review=()):
    from pdfextract.core.job import ExtractionPlan

    plan = ExtractionPlan(mode=3)
    plan.items = list(items)
    plan.review = list(review)
    return plan


def test_the_tally_counts_single_and_merged_images():
    from pdfextract.core.job import PlanItem
    from pdfextract.core.progress import tally

    job = _job(pages=6, mode=3)
    job.plan = _plan_with([
        PlanItem(page_start=1),
        PlanItem(page_start=2, page_end=3, kind=ItemKind.MERGED),
        PlanItem(page_start=4, page_end=5, kind=ItemKind.MERGED),
        PlanItem(page_start=6),
    ])
    counts = tally([job])
    assert (counts.single, counts.merged, counts.to_check) == (2, 2, 0)
    assert counts.images == 4
    assert counts.worth_showing


def test_the_tally_counts_the_pairs_still_to_check():
    from pdfextract.core.job import PlanItem, ReviewPair
    from pdfextract.core.progress import tally

    job = _job(pages=4, mode=3)
    job.plan = _plan_with(
        [PlanItem(page_start=1)],
        [ReviewPair(page_start=2, page_end=3, score=0.6, vertical_offset=0,
                    proposed="merge")],
    )
    counts = tally([job])
    assert counts.to_check == 1
    assert counts.single == 1  # the undecided pair produces nothing yet


def test_the_tally_counts_dropped_pages():
    from pdfextract.core.job import ReviewPair
    from pdfextract.core.progress import tally

    job = _job(pages=4, mode=3)
    job.plan = _plan_with([], [
        ReviewPair(page_start=1, page_end=2, score=0.5, vertical_offset=0,
                   proposed="split", decision="drop_right"),
        ReviewPair(page_start=3, page_end=4, score=0.5, vertical_offset=0,
                   proposed="split", decision="drop_both"),
    ])
    counts = tally([job])
    assert counts.dropped == 3
    assert counts.single == 1  # only page 1 survives


def test_a_tally_with_nothing_merged_is_not_worth_showing():
    """In mode 1 it would only repeat the page count."""
    from pdfextract.core.job import PlanItem
    from pdfextract.core.progress import tally

    job = _job(pages=2, mode=1)
    job.plan = _plan_with([PlanItem(page_start=1), PlanItem(page_start=2)])
    assert not tally([job]).worth_showing


def test_a_book_that_has_not_been_analysed_adds_nothing_to_the_tally():
    from pdfextract.core.progress import tally

    assert tally([_job(pages=0)]) .images == 0
