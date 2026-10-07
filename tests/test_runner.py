"""Tests of the job state machine and of the validation queue.

The property under test is the structural one: a file that needs the user must not
hold up anything, and no worker may ever sit waiting for a human.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pdfextract.core import sidecar
from pdfextract.core.job import JobState
from pdfextract.core.review_queue import ReviewQueue
from pdfextract.core.runner import (
    BatchRunner,
    PdfJob,
    analyse_and_export,
    record_decision,
)
from pdfextract.core.settings import AppSettings

from fixture_builder import build_pdf, make_text_page, spread_pages


def _book(path: Path, spreads: int = 2, fly_leaves: int = 1) -> Path:
    pages = [make_text_page(seed=200 + index) for index in range(fly_leaves)]
    for index in range(spreads):
        left, right = spread_pages(seed=2 + index, noise=3.0)
        pages += [left, right]
    return build_pdf(path, pages)


def _settings(tmp_path: Path, mode: int = 3, everything_uncertain: bool = False):
    settings = AppSettings()
    settings.mode = mode
    settings.output.root = str(tmp_path / "out")
    if everything_uncertain:
        # Force every pair into the grey zone, which is the situation the whole
        # partial export machinery exists for.
        settings.seam.threshold_merge = 0.99
        settings.seam.threshold_split = 0.01
    return settings


def test_ambiguous_pairs_go_to_the_queue_and_release_the_worker(tmp_path):
    path = _book(tmp_path / "book.pdf")
    queue = ReviewQueue()
    job = PdfJob(path=path)
    analyse_and_export(job, _settings(tmp_path, everything_uncertain=True), queue=queue)

    assert job.state is JobState.AWAITING_VALIDATION
    assert job.pending_count > 0
    assert queue.batch_count() == 1
    assert queue.pair_count() == job.pending_count


def test_pages_with_no_decision_to_take_are_exported_immediately(tmp_path):
    """Partial export: the safe pages must be on disk before any validation."""
    path = _book(tmp_path / "book.pdf", spreads=2, fly_leaves=3)
    settings = _settings(tmp_path, everything_uncertain=True)
    job = PdfJob(path=path)
    analyse_and_export(job, settings, queue=ReviewQueue())

    assert job.state is JobState.AWAITING_VALIDATION
    assert job.written, "no page was exported while the file waits for the user"
    pending = job.plan.pending_pages
    for written in job.written:
        assert not set(written.item.pages) & pending


def test_a_file_awaiting_validation_does_not_hold_up_the_next_ones(tmp_path):
    """The queue must drain even though the first file is still undecided."""
    first = _book(tmp_path / "1-ambiguous.pdf")
    second = build_pdf(tmp_path / "2-simple.pdf", [make_text_page(seed=7)])
    third = build_pdf(tmp_path / "3-simple.pdf", [make_text_page(seed=8)])

    queue = ReviewQueue()
    runner = BatchRunner(_settings(tmp_path, everything_uncertain=True), queue=queue,
                         max_workers=2)
    runner.submit([first, second, third])
    runner.start()
    assert runner.wait_until_idle(timeout=60.0)
    runner.stop()

    states = {job.path.name: job.state for job in runner.jobs}
    assert states["1-ambiguous.pdf"] is JobState.AWAITING_VALIDATION
    assert states["2-simple.pdf"] is JobState.DONE
    assert states["3-simple.pdf"] is JobState.DONE


def test_no_worker_stays_blocked_on_a_file_awaiting_validation(tmp_path):
    """A single worker must be free again once the ambiguous file is queued."""
    ambiguous = _book(tmp_path / "1-ambiguous.pdf")
    later = build_pdf(tmp_path / "2-simple.pdf", [make_text_page(seed=9)])

    runner = BatchRunner(_settings(tmp_path, everything_uncertain=True),
                         queue=ReviewQueue(), max_workers=1)
    runner.submit([ambiguous, later])
    runner.start()
    assert runner.wait_until_idle(timeout=60.0)
    runner.stop()

    assert runner.jobs[1].state is JobState.DONE


def test_finalising_a_batch_writes_the_decided_pages(tmp_path):
    path = _book(tmp_path / "book.pdf")
    settings = _settings(tmp_path, everything_uncertain=True)
    queue = ReviewQueue()
    runner = BatchRunner(settings, queue=queue, max_workers=1)
    runner.submit([path])
    runner.start()
    runner.wait_until_idle(timeout=60.0)
    runner.stop()

    batch = queue.batches()[0]
    before = len(runner.jobs[0].written)
    for pair in list(batch.pending):
        record_decision(batch, pair.label, "merge")
    job = runner.finalise_batch(batch)

    assert job.state is JobState.DONE
    assert len(job.written) > before
    assert queue.is_empty()
    assert all(item.path.is_file() for item in job.written)


def test_decisions_survive_a_restart(tmp_path):
    """Closing the application mid-validation must not cost a single decision."""
    path = _book(tmp_path / "book.pdf", spreads=3)
    settings = _settings(tmp_path, everything_uncertain=True)

    first_run = PdfJob(path=path)
    queue = ReviewQueue()
    analyse_and_export(first_run, settings, queue=queue)
    batch = queue.batches()[0]
    decided = batch.pending[0]
    record_decision(batch, decided.label, "split")

    # Second run: a brand new job, as after a restart.
    second_run = PdfJob(path=path)
    analyse_and_export(second_run, settings, queue=ReviewQueue())
    restored = {pair.label: pair.decision for pair in second_run.plan.review}
    assert restored[decided.label] == "split"


def test_the_queue_is_rebuilt_from_the_sidecars(tmp_path):
    path = _book(tmp_path / "book.pdf")
    settings = _settings(tmp_path, everything_uncertain=True)
    analyse_and_export(PdfJob(path=path), settings, queue=ReviewQueue())

    found = sidecar.iter_sidecars(tmp_path)
    assert [pdf for pdf, _ in found] == [path]
    assert found[0][1].state == JobState.AWAITING_VALIDATION.value


def test_a_sidecar_from_another_file_is_ignored(tmp_path):
    """A stale decision applied to the wrong pages is worse than asking again."""
    path = _book(tmp_path / "book.pdf")
    settings = _settings(tmp_path, everything_uncertain=True)
    job = PdfJob(path=path)
    analyse_and_export(job, settings, queue=ReviewQueue())

    stored = sidecar.load(path)
    stored.pdf_hash = "0" * 64
    stored.decisions = {label: "merge" for label in stored.offsets}
    sidecar.save(path, stored)

    again = PdfJob(path=path)
    analyse_and_export(again, settings, queue=ReviewQueue())
    assert all(pair.decision is None for pair in again.plan.review)


def test_a_broken_pdf_never_stops_the_queue(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")
    good = build_pdf(tmp_path / "good.pdf", [make_text_page(seed=11)])

    runner = BatchRunner(_settings(tmp_path, mode=1), queue=ReviewQueue(), max_workers=1)
    runner.submit([broken, good])
    runner.start()
    assert runner.wait_until_idle(timeout=60.0)
    runner.stop()

    assert runner.jobs[0].state is JobState.FAILED
    assert runner.jobs[0].errors
    assert runner.jobs[1].state is JobState.DONE


def test_mode_can_be_overridden_per_file(tmp_path):
    path = _book(tmp_path / "book.pdf")
    settings = _settings(tmp_path, mode=1)
    runner = BatchRunner(settings, queue=ReviewQueue(), max_workers=1)
    runner.submit([path], mode=3)
    runner.start()
    runner.wait_until_idle(timeout=60.0)
    runner.stop()
    assert runner.jobs[0].plan.mode == 3


def test_throttling_reduces_the_pool_to_one_worker(tmp_path):
    runner = BatchRunner(_settings(tmp_path), queue=ReviewQueue(), max_workers=4)
    runner.throttle(True)
    assert runner._allowed == 1
    runner.throttle(False)
    assert runner._allowed == 4


@pytest.mark.parametrize("policy", ["merge", "split"])
def test_every_page_ends_up_in_exactly_one_output(tmp_path, policy):
    """Whatever the user decides, no page is exported twice or lost."""
    path = _book(tmp_path / "book.pdf", spreads=3, fly_leaves=1)
    settings = _settings(tmp_path, everything_uncertain=True)
    queue = ReviewQueue()
    runner = BatchRunner(settings, queue=queue, max_workers=1)
    runner.submit([path])
    runner.start()
    runner.wait_until_idle(timeout=60.0)
    runner.stop()

    batch = queue.batches()[0]
    for pair in list(batch.pending):
        record_decision(batch, pair.label, policy)
    job = runner.finalise_batch(batch)

    pages = [page for item in job.written for page in item.item.pages]
    assert sorted(pages) == list(range(1, job.page_count + 1))


def test_the_mode_two_parity_can_be_forced_on_a_job(tmp_path):
    """The one click override of the interface goes through the job, not the settings."""
    path = _book(tmp_path / "book.pdf", spreads=2, fly_leaves=1)
    settings = _settings(tmp_path, mode=2)

    detected = PdfJob(path=path)
    analyse_and_export(detected, settings, queue=ReviewQueue())
    assert detected.plan.items[0].pages == (1,)  # the fly-leaf stands alone

    forced = PdfJob(path=path, force_parity=0)
    analyse_and_export(forced, settings, queue=ReviewQueue())
    assert forced.plan.items[0].pages == (1, 2)
