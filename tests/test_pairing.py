"""Tests of the global pairing decision and of the extraction plan."""

from __future__ import annotations

import pytest

from pdfextract.core.document import PdfDocument
from pdfextract.core.job import ExtractionPlan, ItemKind, PlanItem, ReviewPair
from pdfextract.core.pairing import build_plan, detect_start_offset, plan_with_parity
from pdfextract.core.settings import AppSettings

from fixture_builder import build_pdf, make_text_page, spread_pages


def _book(tmp_path, fly_leaves: int = 1, spreads: int = 3, name: str = "book.pdf"):
    """Build a book with a given number of lone pages before the first spread."""
    pages = [make_text_page(seed=100 + index) for index in range(fly_leaves)]
    for index in range(spreads):
        left, right = spread_pages(seed=2 + index, noise=3.0, dy=4 - 2 * index)
        pages += [left, right]
    return build_pdf(tmp_path / name, pages)


def _plan(path, mode: int):
    settings = AppSettings()
    settings.mode = mode
    with PdfDocument(path) as document:
        return build_plan(document, settings)


@pytest.mark.parametrize("fly_leaves", [0, 1, 2])
def test_mode2_detects_the_starting_offset(tmp_path, fly_leaves):
    """The parity that pairs the spreads must win, whatever precedes them."""
    plan = _plan(_book(tmp_path, fly_leaves=fly_leaves), mode=2)
    merged = [item for item in plan.items if item.kind is ItemKind.MERGED]
    # The three spreads start right after the fly-leaves, so the pairing has to
    # begin on the same parity as the first spread.
    assert len(merged) == 3 + (fly_leaves // 2)
    assert {item.pages for item in merged} >= {
        (fly_leaves + 1 + 2 * index, fly_leaves + 2 + 2 * index) for index in range(3)
    }


def test_mode2_reports_its_confidence(tmp_path):
    plan = _plan(_book(tmp_path, fly_leaves=1), mode=2)
    assert plan.offset_confidence is not None
    assert 0.5 <= plan.offset_confidence <= 1.0
    assert any("confidence" in note for note in plan.notes)


def test_detect_start_offset_prefers_the_parity_with_the_better_scores():
    scores = [0.1, 0.9, 0.1, 0.9, 0.1, 0.9]  # pairs starting at an odd index win
    offset, confidence = detect_start_offset(scores)
    assert offset == 1
    assert confidence > 0.8


def test_detect_start_offset_on_an_empty_document():
    assert detect_start_offset([]) == (0, 0.0)


def test_mode3_never_puts_a_page_in_two_pairs(tmp_path):
    """The point of the dynamic programme: no page may be consumed twice."""
    plan = _plan(_book(tmp_path, fly_leaves=1, spreads=4), mode=3)
    seen: list[int] = []
    for item in plan.items:
        seen.extend(item.pages)
    assert len(seen) == len(set(seen))
    assert seen == sorted(seen)


def test_mode3_covers_every_page_exactly_once(tmp_path):
    path = _book(tmp_path, fly_leaves=2, spreads=3)
    plan = _plan(path, mode=3)
    with PdfDocument(path) as document:
        expected = set(range(1, document.page_count + 1))
    covered = {page for item in plan.items for page in item.pages}
    assert covered == expected


def test_mode3_merges_the_spreads_and_leaves_the_fly_leaf_alone(tmp_path):
    plan = _plan(_book(tmp_path, fly_leaves=1, spreads=3), mode=3)
    merged = {item.pages for item in plan.items if item.kind is ItemKind.MERGED}
    assert merged == {(2, 3), (4, 5), (6, 7)}
    singles = {item.page_start for item in plan.items if item.kind is ItemKind.SINGLE}
    assert singles == {1}


def test_forcing_the_other_parity(tmp_path):
    """The one click override rebuilds the plan with the opposite parity."""
    from pdfextract.core.pairing import iter_pair_scores

    path = _book(tmp_path, fly_leaves=1, spreads=3)
    settings = AppSettings()
    settings.mode = 2
    with PdfDocument(path) as document:
        scores = list(iter_pair_scores(document, settings))
        detected = build_plan(document, settings)
        forced = plan_with_parity(document.page_count, scores, settings, offset=0)
    assert {item.pages for item in detected.items} != {item.pages for item in forced.items}
    assert forced.items[0].pages == (1, 2)


def test_uncertain_pairs_are_held_back_from_the_partial_export(tmp_path):
    """Pages awaiting a decision must not be exported yet."""
    plan = _plan(_book(tmp_path, fly_leaves=1, spreads=2), mode=3)
    plan.review.append(
        ReviewPair(page_start=2, page_end=3, score=0.6, vertical_offset=0, proposed="merge")
    )
    certain = {page for item in plan.certain_items for page in item.pages}
    assert not certain & {2, 3}
    assert plan.pending_pages == {2, 3}


def test_resolving_a_merge_consumes_both_pages():
    plan = ExtractionPlan(mode=3)
    plan.review = [
        ReviewPair(page_start=2, page_end=3, score=0.6, vertical_offset=4, proposed="merge",
                   decision="merge"),
        ReviewPair(page_start=3, page_end=4, score=0.55, vertical_offset=0, proposed="split",
                   decision="merge"),
    ]
    produced = plan.resolve_decisions()
    pages = [page for item in produced for page in item.pages]
    assert pages == sorted(set(pages))
    assert (2, 3) in {item.pages for item in produced}
    assert 4 in pages  # the conflicting pair is dropped, page 4 is exported alone


def test_resolving_a_split_produces_two_single_pages():
    plan = ExtractionPlan(mode=3)
    plan.review = [
        ReviewPair(page_start=6, page_end=7, score=0.5, vertical_offset=0, proposed="split",
                   decision="split")
    ]
    produced = plan.resolve_decisions()
    assert [item.pages for item in produced] == [(6,), (7,)]


def test_undecided_pairs_produce_nothing():
    plan = ExtractionPlan(mode=3)
    plan.review = [
        ReviewPair(page_start=6, page_end=7, score=0.5, vertical_offset=0, proposed="split")
    ]
    assert plan.resolve_decisions() == []


def test_dropping_the_left_page_exports_only_the_right_one():
    plan = ExtractionPlan(mode=3)
    plan.review = [
        ReviewPair(page_start=4, page_end=5, score=0.6, vertical_offset=0,
                   proposed="merge", decision="drop_left")
    ]
    assert [item.pages for item in plan.resolve_decisions()] == [(5,)]


def test_dropping_the_right_page_exports_only_the_left_one():
    plan = ExtractionPlan(mode=3)
    plan.review = [
        ReviewPair(page_start=4, page_end=5, score=0.6, vertical_offset=0,
                   proposed="merge", decision="drop_right")
    ]
    assert [item.pages for item in plan.resolve_decisions()] == [(4,)]


def test_dropping_both_pages_exports_nothing():
    plan = ExtractionPlan(mode=3)
    plan.review = [
        ReviewPair(page_start=4, page_end=5, score=0.6, vertical_offset=0,
                   proposed="merge", decision="drop_both")
    ]
    assert plan.resolve_decisions() == []


def test_a_dropped_page_is_not_claimed_by_the_next_pair():
    """A drop consumes its pages exactly as a merge does."""
    plan = ExtractionPlan(mode=3)
    plan.review = [
        ReviewPair(page_start=2, page_end=3, score=0.6, vertical_offset=0,
                   proposed="merge", decision="drop_both"),
        ReviewPair(page_start=3, page_end=4, score=0.5, vertical_offset=0,
                   proposed="split", decision="split"),
    ]
    pages = [page for item in plan.resolve_decisions() for page in item.pages]
    assert pages == [4]  # pages 2 and 3 are gone, page 4 stands alone


def test_the_expected_file_count_follows_the_drops():
    """The recycle bin guard counts files, so a drop has to lower the count."""
    plan = ExtractionPlan(mode=3, items=[PlanItem(page_start=1)])
    plan.review = [
        ReviewPair(page_start=2, page_end=3, score=0.6, vertical_offset=0,
                   proposed="merge", decision="drop_both")
    ]
    assert plan.expected_file_count() == 1  # page 1 only
