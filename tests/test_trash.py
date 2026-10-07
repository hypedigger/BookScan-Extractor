"""Tests of the recycle bin guards.

``send2trash`` is replaced by a recorder for the whole suite (see ``conftest.py``),
so these tests check the decisions, never the deletion itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pdfextract.core.job import ExtractionPlan, ItemKind, JobState, PlanItem, ReviewPair
from pdfextract.core.sidecar import sidecar_path
from pdfextract.core.trash import check_guards, move_to_trash


@pytest.fixture
def scene(tmp_path):
    """A finished job: a source PDF, an output folder and two written files."""
    source = tmp_path / "sources"
    source.mkdir()
    pdf = source / "book.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    output = tmp_path / "out" / "book"
    output.mkdir(parents=True)
    written = []
    for name in ("book_0001.jpg", "book_0002-0003.jpg"):
        path = output / name
        path.write_bytes(b"image bytes")
        written.append(path)
    plan = ExtractionPlan(
        mode=3,
        items=[
            PlanItem(page_start=1),
            PlanItem(page_start=2, page_end=3, kind=ItemKind.MERGED),
        ],
    )
    return pdf, plan, written, output


def test_a_complete_job_may_be_trashed(scene, no_real_trash):
    pdf, plan, written, output = scene
    outcome = move_to_trash(pdf, plan, written, output, JobState.DONE)
    assert outcome.ok
    assert no_real_trash.calls == [pdf]


def test_the_sidecar_follows_the_pdf(scene, no_real_trash):
    pdf, plan, written, output = scene
    companion = sidecar_path(pdf)
    companion.write_text("{}", encoding="utf-8")
    outcome = move_to_trash(pdf, plan, written, output, JobState.DONE)
    assert outcome.moved == [pdf, companion]


def test_the_sidecar_can_be_kept(scene, no_real_trash):
    pdf, plan, written, output = scene
    sidecar_path(pdf).write_text("{}", encoding="utf-8")
    outcome = move_to_trash(pdf, plan, written, output, JobState.DONE, trash_sidecar=False)
    assert outcome.moved == [pdf]


def test_a_recorded_error_blocks_the_move(scene):
    pdf, plan, written, output = scene
    allowed, reason = check_guards(
        pdf, plan, written, output, JobState.DONE, errors=["could not read page 4"]
    )
    assert not allowed
    assert "error" in reason


def test_an_unfinished_validation_blocks_the_move(scene):
    pdf, plan, written, output = scene
    plan.review.append(
        ReviewPair(page_start=4, page_end=5, score=0.6, vertical_offset=0, proposed="merge")
    )
    allowed, reason = check_guards(pdf, plan, written, output, JobState.DONE)
    assert not allowed
    assert "awaiting validation" in reason


def test_a_missing_output_file_blocks_the_move(scene):
    pdf, plan, written, output = scene
    written[0].unlink()
    allowed, reason = check_guards(pdf, plan, written, output, JobState.DONE)
    assert not allowed
    assert "missing" in reason


def test_an_empty_output_file_blocks_the_move(scene):
    pdf, plan, written, output = scene
    written[1].write_bytes(b"")
    allowed, reason = check_guards(pdf, plan, written, output, JobState.DONE)
    assert not allowed
    assert "empty" in reason


def test_a_short_count_of_written_files_blocks_the_move(scene):
    pdf, plan, written, output = scene
    allowed, reason = check_guards(pdf, plan, written[:1], output, JobState.DONE)
    assert not allowed
    assert "expected" in reason


def test_an_output_folder_inside_the_source_folder_blocks_the_move(tmp_path):
    source = tmp_path / "sources"
    source.mkdir()
    pdf = source / "book.pdf"
    pdf.write_bytes(b"%PDF")
    output = source / "images"
    output.mkdir()
    written = [output / "book_0001.jpg"]
    written[0].write_bytes(b"x")
    plan = ExtractionPlan(mode=1, items=[PlanItem(page_start=1)])
    allowed, reason = check_guards(pdf, plan, written, output, JobState.DONE)
    assert not allowed
    assert "inside the source folder" in reason


def test_an_unfinished_job_blocks_the_move(scene):
    pdf, plan, written, output = scene
    allowed, reason = check_guards(pdf, plan, written, output, JobState.PARTIAL_EXPORT)
    assert not allowed
    assert "not done" in reason


def test_a_send2trash_failure_never_falls_back_on_a_real_delete(scene, no_real_trash):
    """A locked file or a network volume must leave the source exactly where it is."""
    pdf, plan, written, output = scene
    no_real_trash.failure = OSError("the file is in use")
    outcome = move_to_trash(pdf, plan, written, output, JobState.DONE)
    assert not outcome.ok
    assert outcome.skipped
    assert pdf.is_file()
    assert "send2trash failed" in outcome.reason


def test_a_blocked_move_is_reported_not_silent(scene):
    pdf, plan, written, output = scene
    outcome = move_to_trash(pdf, plan, written[:1], output, JobState.DONE)
    assert outcome.skipped
    assert outcome.reason
    assert pdf.is_file()


def test_the_move_is_written_to_the_application_log(scene, no_real_trash):
    """If there is ever a doubt about what was removed, the log has to answer it."""
    from pdfextract.core.applog import get_logger

    pdf, plan, written, output = scene
    logger = get_logger()
    move_to_trash(pdf, plan, written, output, JobState.DONE)

    # The logger configures its file once per process, so the handler itself says
    # where the log went rather than recomputing a path from the environment.
    targets = [
        Path(handler.baseFilename)
        for handler in logger.handlers
        if hasattr(handler, "baseFilename")
    ]
    assert targets
    for handler in logger.handlers:
        handler.flush()
    text = "".join(path.read_text(encoding="utf-8") for path in targets if path.is_file())
    assert "recycle bin" in text
    assert pdf.name in text
