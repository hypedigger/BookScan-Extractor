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
from pdfextract.core.seam import SeamScore, score_seam
from pdfextract.core.settings import AppSettings, PairingConfig
from pdfextract.core.spread import IMPOSSIBLE, probability

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
                evidence=score.evidence,
                veto=score.veto,
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
        and item.score <= probability(settings.seam.split_evidence)
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


def book_parity(evidences: list[float], config: PairingConfig) -> int | None:
    """Return the parity of the pair indices that hold the spreads, if it is clear.

    A book is laid out so that a spread always sits on the same two facing pages:
    once the left page of a spread is an even page, it is so throughout the book,
    give or take a local shift. Witnesses are the pairs that look like spreads on
    their own; the parity is only trusted when enough of them agree and the other
    parity has clearly fewer.
    """
    witnesses = [0, 0]
    for index, value in enumerate(evidences):
        if value >= config.parity_evidence:
            witnesses[index % 2] += 1
    best = 0 if witnesses[0] >= witnesses[1] else 1
    if witnesses[best] >= config.parity_min_pairs and witnesses[best] >= 3 * witnesses[1 - best]:
        return best
    return None


def apply_parity(evidences: list[float], config: PairingConfig) -> tuple[list[float], int | None]:
    """Shift every evidence by the parity prior of the book, when there is one."""
    parity = book_parity(evidences, config)
    if parity is None:
        return list(evidences), None
    adjusted = [
        value
        if value <= IMPOSSIBLE
        else value + (config.parity_bonus if index % 2 == parity else -config.parity_penalty)
        for index, value in enumerate(evidences)
    ]
    return adjusted, parity


def best_arrangement(evidences: list[float]) -> set[int]:
    """Return the pair indices of the most likely arrangement of the whole book.

    ``best[n]`` is the total evidence of the best arrangement of the first ``n``
    pages, a lone page counting for nothing and a pair for its evidence. Taking a
    pair only when that beats leaving its pages alone, over the whole sequence,
    guarantees that no page ends up in two pairs.
    """
    page_count = len(evidences) + 1
    best = [0.0] * (page_count + 1)
    paired = [False] * (page_count + 1)
    for count in range(2, page_count + 1):
        candidate = best[count - 2] + evidences[count - 2]
        if candidate > best[count - 1]:
            best[count], paired[count] = candidate, True
        else:
            best[count] = best[count - 1]
    chosen: set[int] = set()
    cursor = page_count
    while cursor >= 2:
        if paired[cursor]:
            chosen.add(cursor - 2)
            cursor -= 2
        else:
            cursor -= 1
    return chosen


def _mode3_plan(
    document: PdfDocument, scores: list[SeamScore], settings: AppSettings
) -> ExtractionPlan:
    """Build the smart plan: merge what is sure, ask only about what is not.

    The evidence of every pair is first corrected by the parity of the book, then
    the best arrangement of the whole book is chosen. Pairs of that arrangement at
    or above the merge threshold are merged without asking; their pages are then
    settled, so no question is asked about a pair that would reuse one of them.
    Everything between the two thresholds becomes a question, with the arrangement
    as the proposed answer.
    """
    page_count = document.page_count
    merge_at = settings.seam.merge_evidence
    split_at = settings.seam.split_evidence
    evidences, parity = apply_parity([score.evidence for score in scores], settings.pairing)
    chosen = best_arrangement(evidences)

    sure = {index for index in chosen if evidences[index] >= merge_at}
    settled = {page for index in sure for page in (index + 1, index + 2)}

    plan = ExtractionPlan(mode=3)
    cursor = 1
    while cursor <= page_count:
        index = cursor - 1
        if index in chosen:
            score = scores[index]
            plan.items.append(
                PlanItem(
                    page_start=cursor,
                    page_end=cursor + 1,
                    kind=ItemKind.MERGED,
                    vertical_offset=score.vertical_offset,
                    score=probability(evidences[index]),
                    uncertain=index not in sure,
                )
            )
            if index not in sure:
                plan.review.append(
                    _review_pair(cursor, score, evidences[index], proposed="merge")
                )
            cursor += 2
        else:
            plan.items.append(PlanItem(page_start=cursor))
            cursor += 1

    claimed = settled | _pages_of(plan.review)
    for index, score in enumerate(scores):
        pages = (index + 1, index + 2)
        if index in chosen or not split_at < evidences[index] < merge_at:
            continue
        if claimed & set(pages):
            continue  # a page already settled or already asked about
        plan.review.append(_review_pair(pages[0], score, evidences[index], proposed="split"))
        claimed.update(pages)
    plan.review.sort(key=lambda pair: pair.page_start)
    _mark_uncertain_items(plan)

    asked = len(plan.review)
    vetoed = sum(1 for score in scores if score.veto is not None)
    if parity is not None:
        plan.notes.append(
            f"Spreads start on {'odd' if parity == 0 else 'even'} pages in this book."
        )
    plan.notes.append(
        f"{len(sure)} spread(s) merged, {asked} question(s), "
        f"{vetoed} pair(s) ruled out at once (frame, margin or blank page)."
    )
    return plan


def _pages_of(review: list[ReviewPair]) -> set[int]:
    """Return every page already involved in a pending decision."""
    return {page for pair in review for page in (pair.page_start, pair.page_end)}


def _review_pair(
    page_start: int, score: SeamScore, evidence: float, proposed: str
) -> ReviewPair:
    """Wrap a score into a pending decision."""
    return ReviewPair(
        page_start=page_start,
        page_end=page_start + 1,
        score=probability(evidence),
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
