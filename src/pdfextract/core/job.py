"""Job model: extraction plan, states and synchronous runner.

A job is one PDF. Its plan is the list of output items to produce, each item
referring to its source page numbers, so that naming never depends on the order in
which files reach the disk. The state machine is the one described in the
specification: manual validation never blocks the processing of anything else.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import numpy as np

from pdfextract.core import naming
from pdfextract.core.document import PdfDocument
from pdfextract.core.extractor import PageOutput, encoded_output, load_page_array, page_output
from pdfextract.core.settings import AppSettings, PostProcessConfig

# What the user can decide about an ambiguous pair. The first two answer the
# question the scorer could not; the last three are for pages that should simply
# not be exported at all, such as a blank verso or a scan of the scanner lid.
MERGE = "merge"
SPLIT = "split"
DROP_LEFT = "drop_left"  # keep the right page only
DROP_RIGHT = "drop_right"  # keep the left page only
DROP_BOTH = "drop_both"

DECISIONS = (MERGE, SPLIT, DROP_LEFT, DROP_RIGHT, DROP_BOTH)


class JobState(Enum):
    """Life cycle of a PDF, as described in section 11.2 of the specification."""

    PENDING = "pending"
    ANALYSING = "analysing"
    PARTIAL_EXPORT = "partial_export"
    AWAITING_VALIDATION = "awaiting_validation"
    VALIDATED = "validated"
    FINALISING = "finalising"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ItemKind(Enum):
    """What an output item is made of."""

    SINGLE = "single"
    MERGED = "merged"


@dataclass(frozen=True)
class PlanItem:
    """One output image, described by the source pages it comes from."""

    page_start: int  # one based
    page_end: int | None = None
    kind: ItemKind = ItemKind.SINGLE
    vertical_offset: int = 0
    score: float | None = None
    uncertain: bool = False

    @property
    def pages(self) -> tuple[int, ...]:
        """Return every source page this item consumes."""
        if self.page_end is None or self.page_end == self.page_start:
            return (self.page_start,)
        return (self.page_start, self.page_end)

    @property
    def label(self) -> str:
        """Return the page label used in file names, e.g. ``0012-0013``."""
        return naming.format_pages(self.page_start, self.page_end)


@dataclass
class ReviewPair:
    """A pair the engine will not decide on its own.

    It carries the engine's own proposal, so the validation screen can pre-select
    nothing while still knowing what would have happened unattended.
    """

    page_start: int
    page_end: int
    score: float
    vertical_offset: int
    proposed: str  # "merge" or "split"
    metrics: dict[str, float] = field(default_factory=dict)
    decision: str | None = None  # "merge" or "split" once the user has chosen

    @property
    def pages(self) -> tuple[int, int]:
        """Return the two source pages involved."""
        return self.page_start, self.page_end

    @property
    def label(self) -> str:
        """Return the page label of the pair."""
        return naming.format_pages(self.page_start, self.page_end)


@dataclass
class ExtractionPlan:
    """The complete list of items to produce for one PDF.

    ``items`` is the arrangement the engine would apply unattended. Anything that
    touches a pending decision is held back: those pages are produced once, at the
    end, from the user decision, so no provisional image is ever written and then
    replaced.
    """

    mode: int
    items: list[PlanItem] = field(default_factory=list)
    review: list[ReviewPair] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    offset_confidence: float | None = None

    @property
    def review_pages(self) -> set[int]:
        """Return every page involved in a decision, taken or not."""
        return {page for pair in self.review for page in pair.pages}

    @property
    def pending_pages(self) -> set[int]:
        """Return every page still waiting for a decision."""
        return {
            page
            for pair in self.review
            if pair.decision is None
            for page in pair.pages
        }

    @property
    def pending_pairs(self) -> list[ReviewPair]:
        """Return the pairs still waiting for a decision."""
        return [pair for pair in self.review if pair.decision is None]

    @property
    def certain_items(self) -> list[PlanItem]:
        """Return the items that can be exported straight away."""
        pages = self.review_pages
        return [item for item in self.items if not set(item.pages) & pages]

    @property
    def uncertain_items(self) -> list[PlanItem]:
        """Return the provisional items held back until the user has decided."""
        pages = self.review_pages
        return [item for item in self.items if set(item.pages) & pages]

    def resolve_decisions(self) -> list[PlanItem]:
        """Turn the decisions taken so far into the items they produce.

        A page belongs to at most one output, so a decision that claims both of its
        pages consumes them and any other pair claiming one of them is dropped.
        Pages that were decided but claimed by nobody are exported on their own,
        which is what a split means.
        """
        consumed: set[int] = set()
        produced: list[PlanItem] = []
        ordered = sorted(self.review, key=lambda pair: pair.page_start)
        for pair in ordered:
            if pair.decision in (None, SPLIT) or consumed & set(pair.pages):
                continue
            if pair.decision == MERGE:
                produced.append(
                    PlanItem(
                        page_start=pair.page_start,
                        page_end=pair.page_end,
                        kind=ItemKind.MERGED,
                        vertical_offset=pair.vertical_offset,
                        score=pair.score,
                    )
                )
            elif pair.decision == DROP_LEFT:
                produced.append(PlanItem(page_start=pair.page_end))
            elif pair.decision == DROP_RIGHT:
                produced.append(PlanItem(page_start=pair.page_start))
            elif pair.decision != DROP_BOTH:
                continue  # an unknown decision changes nothing
            # A drop claims its pages just as firmly as a merge: the point is that
            # they produce no image of their own.
            consumed.update(pair.pages)
        settled = {
            page
            for pair in ordered
            if pair.decision is not None
            for page in pair.pages
        }
        for page in sorted(settled - consumed):
            if any(page in pair.pages and pair.decision is None for pair in ordered):
                continue  # another pair may still claim this page
            produced.append(PlanItem(page_start=page))
        return sorted(produced, key=lambda item: item.page_start)

    def expected_file_count(self) -> int:
        """Return how many files a complete run must write.

        Exact only once every pair has been decided, which is precisely when the
        recycle bin guard needs it.
        """
        return len(self.certain_items) + len(self.resolve_decisions())


@dataclass
class WrittenFile:
    """One file written to disk, kept for the trash guard conditions."""

    path: Path
    item: PlanItem
    passthrough: bool
    reason: str


@dataclass
class JobResult:
    """Outcome of running a job."""

    pdf_path: Path
    state: JobState
    written: list[WrittenFile] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    plan: ExtractionPlan | None = None


ProgressCallback = Callable[[str, int, int], None]


def build_single_plan(page_count: int) -> ExtractionPlan:
    """Return the mode 1 plan: one output per source page, no decision to take."""
    return ExtractionPlan(
        mode=1,
        items=[PlanItem(page_start=number) for number in range(1, page_count + 1)],
    )


def produce(document: PdfDocument, item: PlanItem, settings: AppSettings) -> PageOutput:
    """Render one plan item to the bytes that must be written.

    Merging or post-processing both destroy the original stream by construction, so
    those paths always re-encode; a plain single page with no post-processing keeps
    its stream untouched.
    """
    post = settings.resolved_postprocess()
    extraction = settings.extraction

    if item.kind is ItemKind.MERGED:
        from pdfextract.core.merger import merge_pages  # local: keeps core import graph flat

        assert item.page_end is not None
        left = load_page_array(document.load_page(item.page_start - 1), extraction)
        right = load_page_array(document.load_page(item.page_end - 1), extraction)
        merged = merge_pages(left, right, item.vertical_offset, settings.merge)
        merged = _post_process(merged, post, merged_image=True)
        return encoded_output(merged, extraction, "merged")

    page = document.load_page(item.page_start - 1)
    if post.any_enabled():
        array = load_page_array(page, extraction)
        array = _post_process(array, post, merged_image=False)
        return encoded_output(array, extraction, "post_processed")
    return page_output(page, extraction)


def _post_process(
    array: np.ndarray, post: PostProcessConfig, merged_image: bool
) -> np.ndarray:
    """Apply the post-processing chain when at least one step is enabled."""
    if not post.any_enabled():
        return array
    from pdfextract.core.postprocess import apply_chain  # local: keeps the import graph flat

    return apply_chain(array, post, merged=merged_image)


def item_path(
    directory: Path,
    pdf_stem: str,
    item: PlanItem,
    settings: AppSettings,
    taken: set[Path],
    index: int,
) -> Path:
    """Return the output path of an item, from the user template."""
    context = naming.NameContext(
        pdf_stem=pdf_stem,
        page_start=item.page_start,
        page_end=item.page_end,
        mode=settings.mode,
        index=index,
    )
    stem = naming.render_template(settings.output.template or naming.DEFAULT_TEMPLATE, context)
    return naming.build_output_path(
        directory,
        stem,
        "jpg",
        taken=taken,
        overwrite=settings.output.overwrite,
    )


def write_items(
    document: PdfDocument,
    items: Iterable[PlanItem],
    directory: Path,
    settings: AppSettings,
    taken: set[Path] | None = None,
    progress: ProgressCallback | None = None,
    start_index: int = 0,
) -> Iterator[WrittenFile]:
    """Produce and write items one by one, yielding each written file.

    Pages are handled in a stream: a 500 page book is never held in memory.
    """
    directory.mkdir(parents=True, exist_ok=True)
    allocated = taken if taken is not None else set()
    materialised = list(items)
    total = len(materialised)
    for position, item in enumerate(materialised):
        output = produce(document, item, settings)
        path = item_path(
            directory,
            document.path.stem,
            item,
            settings,
            allocated,
            start_index + position,
        )
        if path.suffix.lower() != f".{output.ext}":
            path = path.with_suffix(f".{output.ext}")
            allocated.add(path)
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(output.data)
        tmp.replace(path)
        if progress is not None:
            progress(item.label, position + 1, total)
        yield WrittenFile(
            path=path,
            item=item,
            passthrough=output.passthrough,
            reason=output.reason,
        )
