"""Headless command line interface.

Used for testing, for batch scripting and, above all, to keep the engine honest:
everything the graphical interface can do must be reachable without Qt.
"""

from __future__ import annotations

import argparse
import sys
import time
from itertools import pairwise
from pathlib import Path

from pdfextract.core import naming
from pdfextract.core.document import PdfDocument, PdfDocumentError, iter_pdf_files
from pdfextract.core.job import JobState
from pdfextract.core.review_queue import ReviewQueue
from pdfextract.core.runner import BatchRunner, record_decision
from pdfextract.core.settings import AppSettings, load_settings, save_settings


def _apply_common_options(settings: AppSettings, args: argparse.Namespace) -> AppSettings:
    """Fold the command line options into a settings object."""
    settings.mode = getattr(args, "mode", settings.mode)
    if getattr(args, "dpi", None):
        settings.extraction.render_dpi = args.dpi
    if getattr(args, "template", None):
        settings.output.template = args.template
    if getattr(args, "out", None):
        settings.output.root = str(args.out)
    if getattr(args, "no_subfolder", False):
        settings.output.subfolder_per_pdf = False
    if getattr(args, "overwrite", False):
        settings.output.overwrite = True
    if getattr(args, "debug", False):
        settings.debug_metrics = True
    return settings


def _resolve_sources(paths: list[str], recursive: bool) -> tuple[list[Path], Path | None]:
    """Expand the given paths into a list of PDF files and a common source root."""
    files: list[Path] = []
    roots: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_dir():
            roots.append(path)
        files.extend(iter_pdf_files(path, recursive=recursive))
    source_root = roots[0] if len(roots) == 1 else None
    return files, source_root


def command_info(args: argparse.Namespace) -> int:
    """Print the page inventory of every PDF given."""
    files, _ = _resolve_sources(args.paths, args.recursive)
    if not files:
        print("no PDF found", file=sys.stderr)
        return 2
    for path in files:
        try:
            with PdfDocument(path, args.password) as document:
                print(f"{path}")
                print(f"  pages: {document.page_count}")
                print(f"  sha256: {document.file_hash()[:16]}...")
                rotations = {info.rotation for info in document.iter_page_info()}
                print(f"  rotations: {sorted(rotations)}")
        except PdfDocumentError as exc:
            print(f"{path}: {exc}", file=sys.stderr)
    return 0


def command_extract(args: argparse.Namespace) -> int:
    """Run an extraction over one or more PDF files.

    The runner is the same one the interface uses, so the command line exercises
    the real state machine: ambiguous pairs land in the validation queue instead of
    blocking the file that produced them.
    """
    settings = _apply_common_options(load_settings(args.settings), args)
    try:
        for warning in naming.validate_template(settings.output.template):
            print(f"warning: {warning}", file=sys.stderr)
    except naming.NamingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    files, source_root = _resolve_sources(args.paths, args.recursive)
    if not files:
        print("no PDF found", file=sys.stderr)
        return 2

    queue = ReviewQueue()
    runner = BatchRunner(settings, queue=queue)
    started = time.perf_counter()
    runner.submit(files, source_root=source_root)
    runner.start()
    runner.wait_until_idle(timeout=args.timeout)
    runner.stop()

    if args.auto_review != "none":
        _auto_review(runner, queue, args.auto_review)

    failures = 0
    for job in runner.jobs:
        if job.state is JobState.FAILED:
            failures += 1
            print(f"{job.path.name}: {'; '.join(job.errors)}", file=sys.stderr)
            continue
        native = sum(1 for item in job.written if item.passthrough)
        print(
            f"{job.path.name}: {len(job.written)} file(s), {native} native stream(s), "
            f"state {job.state.value} -> {job.output_directory}"
        )
        for note in (job.plan.notes if job.plan else []):
            print(f"  note: {note}")
        for pair in (job.plan.pending_pairs if job.plan else []):
            print(
                f"  pages {pair.label}: score {pair.score:.3f}, "
                f"engine would {pair.proposed} (awaiting validation)"
            )
        if job.trash_outcome is not None:
            if job.trash_outcome.ok:
                print(f"  source moved to the recycle bin ({len(job.trash_outcome.moved)} file(s))")
            else:
                print(f"  source kept: {job.trash_outcome.reason}")
    print(f"done in {time.perf_counter() - started:.1f}s; {queue.summary().lower()}")
    return 1 if failures else 0


def _auto_review(runner: BatchRunner, queue: ReviewQueue, policy: str) -> None:
    """Decide every pending pair without a user, for scripting and for tests."""
    for batch in queue.batches():
        for pair in list(batch.pending):
            decision = pair.proposed if policy == "engine" else policy
            record_decision(batch, pair.label, decision)
        runner.finalise_batch(batch)


def _progress(label: str, done: int, total: int) -> None:
    """Print a single line progress report."""
    print(f"  [{done}/{total}] {label}", end="\r", file=sys.stderr)
    if done == total:
        print(file=sys.stderr)


HISTOGRAM_BUCKETS = 20


def command_calibrate(args: argparse.Namespace) -> int:
    """Show the distribution of the continuity scores of whole books.

    The default thresholds are a starting point, nothing more. On real files the
    two populations, spreads and unrelated pages, separate into two clusters with a
    gap between them; this prints that distribution and proposes thresholds placed
    inside the gap.
    """
    from pdfextract.core.pairing import iter_pair_scores

    settings = load_settings(args.settings)
    settings.mode = 3
    scores: list[tuple[str, int, float, str]] = []
    for raw in args.paths:
        for path in iter_pdf_files(Path(raw).expanduser(), recursive=args.recursive):
            try:
                with PdfDocument(path, args.password) as document:
                    for index, score in enumerate(iter_pair_scores(document, settings)):
                        scores.append((path.name, index + 1, score.value, score.confidence))
            except PdfDocumentError as exc:
                print(f"{path.name}: {exc}", file=sys.stderr)
    if not scores:
        print("no score to report", file=sys.stderr)
        return 2

    values = sorted(value for _, _, value, _ in scores)
    print(f"\n{len(values)} pair(s) scored\n")
    counts = [0] * HISTOGRAM_BUCKETS
    for value in values:
        counts[min(HISTOGRAM_BUCKETS - 1, int(value * HISTOGRAM_BUCKETS))] += 1
    widest = max(counts) or 1
    for bucket, count in enumerate(counts):
        low = bucket / HISTOGRAM_BUCKETS
        bar = "#" * int(round(40 * count / widest))
        print(f"  {low:.2f}-{low + 1 / HISTOGRAM_BUCKETS:.2f} {count:>4} {bar}")

    gap_start, gap_end = _widest_gap(values)
    print(f"\nWidest gap in the distribution: {gap_start:.3f} to {gap_end:.3f}")
    if gap_end - gap_start > 0.05:
        merge = round(gap_start + 0.75 * (gap_end - gap_start), 2)
        split = round(gap_start + 0.25 * (gap_end - gap_start), 2)
        print(f"Suggested thresholds: threshold_merge = {merge}, threshold_split = {split}")
        print(
            f"Current settings:     threshold_merge = {settings.seam.threshold_merge}, "
            f"threshold_split = {settings.seam.threshold_split}"
        )
    else:
        print(
            "The two populations do not separate cleanly. Look at the pairs around the "
            "middle of the range with tools/score_report.py --plate before changing the "
            "weights."
        )

    if args.list_uncertain:
        print("\nPairs in the grey zone with the current thresholds:")
        for name, index, value, confidence in scores:
            if confidence == "uncertain":
                print(f"  {name} pages {index}-{index + 1}: {value:.3f}")
    return 0


def _widest_gap(values: list[float]) -> tuple[float, float]:
    """Return the widest empty interval between two consecutive scores."""
    if len(values) < 2:
        return 0.0, 0.0
    best = (0.0, 0.0)
    for lower, upper in pairwise(values):
        if upper - lower > best[1] - best[0]:
            best = (lower, upper)
    return best


def command_settings(args: argparse.Namespace) -> int:
    """Show or initialise the settings file."""
    path = args.settings
    settings = load_settings(path)
    if args.init:
        written = save_settings(settings, path)
        print(f"settings written to {written}")
        return 0
    import dataclasses
    import json

    print(json.dumps(dataclasses.asdict(settings), indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="pdfextract",
        description="Extract page images from scanned book PDFs.",
    )
    parser.add_argument(
        "--settings",
        type=Path,
        default=None,
        help="path to a settings file (default: the per-user settings)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract", help="extract images from one or more PDF")
    extract.add_argument("paths", nargs="+", help="PDF files or folders")
    extract.add_argument(
        "--mode", type=int, choices=(1, 2, 3), default=3,
        help="1 single, 2 systematic pairing, 3 smart pairing",
    )
    extract.add_argument("--out", type=Path, default=None, help="output root folder")
    extract.add_argument("--dpi", type=int, default=None, help="render dpi for the fallback")
    extract.add_argument("--template", default=None, help="file name template")
    extract.add_argument("--password", default=None, help="password of an encrypted PDF")
    extract.add_argument("--recursive", action="store_true", help="walk sub-folders")
    extract.add_argument(
        "--no-subfolder", action="store_true", help="do not create one folder per PDF"
    )
    extract.add_argument("--overwrite", action="store_true", help="overwrite existing files")
    extract.add_argument("--verbose", action="store_true", help="per page progress")
    extract.add_argument("--debug", action="store_true", help="report individual seam metrics")
    extract.add_argument(
        "--auto-review", choices=("none", "engine", "merge", "split"), default="none",
        help="decide the ambiguous pairs without a user: keep the engine proposal, "
             "or force merge or split (default: leave them in the validation queue)",
    )
    extract.add_argument("--timeout", type=float, default=3600.0, help="give up after N seconds")
    extract.set_defaults(func=command_extract)

    info = subparsers.add_parser("info", help="print the page inventory of a PDF")
    info.add_argument("paths", nargs="+", help="PDF files or folders")
    info.add_argument("--password", default=None)
    info.add_argument("--recursive", action="store_true")
    info.set_defaults(func=command_info)

    calibrate = subparsers.add_parser(
        "calibrate", help="show the distribution of the continuity scores of a book"
    )
    calibrate.add_argument("paths", nargs="+", help="PDF files or folders")
    calibrate.add_argument("--password", default=None)
    calibrate.add_argument("--recursive", action="store_true")
    calibrate.add_argument(
        "--list-uncertain", action="store_true", help="list the pairs in the grey zone"
    )
    calibrate.set_defaults(func=command_calibrate)

    config = subparsers.add_parser("settings", help="show or initialise the settings file")
    config.add_argument("--init", action="store_true", help="write the default settings file")
    config.set_defaults(func=command_settings)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point of the command line interface."""
    parser = build_parser()
    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
