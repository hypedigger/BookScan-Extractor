"""Global pairing decision for modes 2 and 3.

Mode 2 pairs every page two by two and only has to find where the pairing starts.
Mode 3 decides pair by pair, which cannot be done greedily: a page belongs to at
most one pair, so a local choice creates conflicts further along. A dynamic
programme over the sequence of pages gives the globally optimal split instead.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

from pdfextract.core.document import PdfDocument
from pdfextract.core.extractor import analysis_array, output_height
from pdfextract.core.job import ExtractionPlan, ItemKind, PlanItem, ReviewPair
from pdfextract.core.seam import UNCERTAIN, SeamScore, score_seam
from pdfextract.core.settings import AppSettings

ScoreCallback = Callable[[int, int, SeamScore], None]


def iter_pair_scores(
    document: PdfDocument, settings: AppSettings, progress: ScoreCallback | None = None
) -> Iterator[SeamScore]:
    """Score every adjacent pair of a document, in page order.

    Scoring runs on a reduced rendering of each page: the scorer normalises to its
    analysis height anyway, and rendering small is far cheaper than decoding a full
    resolution scan. The offset is converted back to the resolution the merger will
    actually work at, so that the two modules keep speaking the same units.
    """
    extraction = settings.extraction
    total = max(0, document.page_count - 1)
    previous = None
    previous_index = -1
    for index in range(document.page_count):
        page = document.load_page(index)
        current = analysis_array(page, extraction)
        if previous is not None:
            score = score_seam(previous, current, settings.seam)
            scale = output_height(page, extraction) / max(1, current.shape[0])
            score = SeamScore(
                value=score.value,
                vertical_offset=int(round(score.vertical_offset * scale)),
                metrics=score.metrics,
                confidence=score.confidence,
            )
            if progress is not None:
                progress(previous_index + 1, total, score)
            yield score
        previous = current
        previous_index = index


def detect_start_offset(scores: list[float]) -> tuple[int, float]:
    """Return the parity that pairs the pages best, and a confidence in ``[0, 1]``.

    Two parities are possible: pairing starts on the first page, or the first page
    stands alone because it is a fly-leaf or a half-title.
    """
    if not scores:
        return 0, 0.0
    means: dict[int, float] = {}
    for offset in (0, 1):
        selected = [scores[index] for index in range(offset, len(scores), 2)]
        means[offset] = sum(selected) / len(selected) if selected else 0.0
    best = max(means, key=lambda key: means[key])
    other = 1 - best
    total = means[best] + means[other]
    confidence = means[best] / total if total > 0 else 0.5
    return best, confidence


def _pair_every_page(
    page_count: int, scores: list[SeamScore], offset: int
) -> ExtractionPlan:
    """Pair pages two by two from a given starting parity, leaving orphans alone."""
    plan = ExtractionPlan(mode=2)
    if offset:
        plan.items.append(PlanItem(page_start=1))
    index = offset
    while index < page_count - 1:
        score = scores[index]
        plan.items.append(
            PlanItem(
                page_start=index + 1,
                page_end=index + 2,
                kind=ItemKind.MERGED,
                vertical_offset=score.vertical_offset,
                score=score.value,
            )
        )
        index += 2
    if index < page_count:  # a trailing page with no partner
        plan.items.append(PlanItem(page_start=index + 1))
    return plan


def _mode2_plan(
    document: PdfDocument, scores: list[SeamScore], settings: AppSettings
) -> ExtractionPlan:
    """Build the systematic pairing plan, with the starting offset auto-detected."""
    values = [score.value for score in scores]
    offset, confidence = detect_start_offset(values)
    plan = _pair_every_page(document.page_count, scores, offset)
    plan.offset_confidence = confidence

    if settings.pairing.mode2_report_confidence:
        if offset:
            plan.notes.append(
                f"Offset detected: page 1 is exported on its own "
                f"(confidence {confidence:.0%})."
            )
        else:
            plan.notes.append(
                f"Offset detected: pairing starts on page 1 (confidence {confidence:.0%})."
            )
    weak = [
        item.page_start
        for item in plan.items
        if item.kind is ItemKind.MERGED
        and item.score is not None
        and item.score <= settings.seam.threshold_split
    ]
    if weak:
        listed = ", ".join(str(page) for page in weak[:10])
        more = "..." if len(weak) > 10 else ""
        plan.notes.append(
            f"{len(weak)} pair(s) merged although they do not look continuous, "
            f"starting at page(s) {listed}{more}. Mode 3 decides those individually."
        )
    return plan


def plan_with_parity(
    page_count: int, scores: list[SeamScore], settings: AppSettings, offset: int
) -> ExtractionPlan:
    """Build a mode 2 plan for an imposed parity, for the one click override."""
    plan = _pair_every_page(page_count, scores, offset)
    plan.notes.append(
        "Pairing starts on page 1."
        if offset == 0
        else "Page 1 is exported on its own, pairing starts on page 2."
    )
    return plan


def _mode3_plan(
    document: PdfDocument, scores: list[SeamScore], settings: AppSettings
) -> ExtractionPlan:
    """Build the smart plan with a dynamic programme over the page sequence.

    ``best[i]`` is the value of the best arrangement of the first ``i`` pages. A
    page can either be exported on its own, for a constant prior, or merged with the
    page before it, for the value of their seam score. Back-tracking gives the
    globally optimal arrangement, which guarantees that no page ends up in two pairs.

    A pair accounts for two pages, so its seam score is counted twice before being
    weighed against the two single page priors it replaces. Comparing a score, which
    never exceeds one, with a single prior would make pairing impossible as soon as
    the prior went above one half. With this normalisation ``cost_single`` reads
    exactly as intended: a pair is merged when its score beats that value.
    """
    page_count = document.page_count
    cost_single = settings.pairing.cost_single

    best = [0.0] * (page_count + 1)
    merged_here = [False] * (page_count + 1)
    for count in range(1, page_count + 1):
        single = best[count - 1] + cost_single
        take_single = True
        value = single
        if count >= 2:
            pair = best[count - 2] + 2.0 * scores[count - 2].value
            if pair > single:
                value, take_single = pair, False
        best[count] = value
        merged_here[count] = not take_single

    arrangement: list[PlanItem] = []
    review: list[ReviewPair] = []
    cursor = page_count
    while cursor > 0:
        if merged_here[cursor]:
            score = scores[cursor - 2]
            arrangement.append(
                PlanItem(
                    page_start=cursor - 1,
                    page_end=cursor,
                    kind=ItemKind.MERGED,
                    vertical_offset=score.vertical_offset,
                    score=score.value,
                    uncertain=score.confidence == UNCERTAIN,
                )
            )
            if score.confidence == UNCERTAIN:
                review.append(_review_pair(cursor - 1, score, proposed="merge"))
            cursor -= 2
        else:
            arrangement.append(PlanItem(page_start=cursor))
            cursor -= 1
    arrangement.reverse()
    review.reverse()

    plan = ExtractionPlan(mode=3, items=arrangement, review=review)

    # Pairs the programme turned down by a small margin deserve a look too: the
    # user may well see a spread where the score was not quite convincing.
    retained = {(item.page_start, item.page_end) for item in arrangement}
    claimed = _pages_of(plan.review)
    for index, score in enumerate(scores):
        pages = (index + 1, index + 2)
        if pages in retained or score.confidence != UNCERTAIN:
            continue
        if claimed & set(pages):
            continue
        plan.review.append(_review_pair(pages[0], score, proposed="split"))
        claimed.update(pages)
    plan.review.sort(key=lambda pair: pair.page_start)
    _mark_uncertain_items(plan)
    return plan


def _pages_of(review: list[ReviewPair]) -> set[int]:
    """Return every page already involved in a pending decision."""
    return {page for pair in review for page in (pair.page_start, pair.page_end)}


def _review_pair(page_start: int, score: SeamScore, proposed: str) -> ReviewPair:
    """Wrap a score into a pending decision."""
    return ReviewPair(
        page_start=page_start,
        page_end=page_start + 1,
        score=score.value,
        vertical_offset=score.vertical_offset,
        proposed=proposed,
        metrics=dict(score.metrics),
    )


def _mark_uncertain_items(plan: ExtractionPlan) -> None:
    """Flag every item that touches a pending decision, so it is not exported yet."""
    pending = _pages_of(plan.review)
    plan.items = [
        PlanItem(
            page_start=item.page_start,
            page_end=item.page_end,
            kind=item.kind,
            vertical_offset=item.vertical_offset,
            score=item.score,
            uncertain=bool(set(item.pages) & pending),
        )
        for item in plan.items
    ]


def build_plan(
    document: PdfDocument,
    settings: AppSettings,
    progress: ScoreCallback | None = None,
    verbose: bool = False,
    force_parity: int | None = None,
) -> ExtractionPlan:
    """Return the extraction plan of a document for mode 2 or 3.

    ``force_parity`` overrides the parity detected in mode 2, which is what the
    one click override of the interface passes in.
    """
    if settings.mode not in (2, 3):
        raise ValueError(f"build_plan does not handle mode {settings.mode}")
    scores = list(iter_pair_scores(document, settings, progress))
    if verbose:
        for index, score in enumerate(scores):
            print(
                f"  pair {index + 1}-{index + 2}: {score.value:.3f} "
                f"{score.confidence} dy={score.vertical_offset} {score.metrics}"
            )
    if settings.mode == 2:
        if force_parity is not None:
            return plan_with_parity(document.page_count, scores, settings, force_parity)
        return _mode2_plan(document, scores, settings)
    return _mode3_plan(document, scores, settings)
