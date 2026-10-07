"""Moving processed sources to the recycle bin.

Never a permanent delete. ``send2trash`` is used so the user can always restore a
file from the Windows recycle bin, and every guard below has to pass first. If
``send2trash`` itself fails, the failure is reported and the file stays where it is:
falling back on a real delete would be exactly the wrong thing to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from send2trash import send2trash

from pdfextract.core.applog import get_logger
from pdfextract.core.job import ExtractionPlan, JobState
from pdfextract.core.sidecar import sidecar_path


@dataclass(frozen=True)
class TrashOutcome:
    """What happened when a source was considered for the recycle bin."""

    moved: list[Path]
    skipped: bool
    reason: str

    @property
    def ok(self) -> bool:
        """Return True when the source did reach the recycle bin."""
        return bool(self.moved) and not self.skipped


def _is_inside(child: Path, parent: Path) -> bool:
    """Return True when ``child`` is the same folder as ``parent`` or below it."""
    try:
        child.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


def check_guards(
    pdf_path: Path,
    plan: ExtractionPlan,
    written: list[Path],
    output_directory: Path,
    state: JobState,
    errors: list[str] | None = None,
) -> tuple[bool, str]:
    """Check every condition that must hold before a source may be thrown away.

    The conditions are checked in the order given by the specification, and the
    first failure is reported: an export that is not provably complete must leave
    the source alone.
    """
    if errors:
        return False, f"{len(errors)} error(s) recorded for this file"
    if state is not JobState.DONE:
        return False, f"the job is in state {state.value}, not done"
    if plan.pending_pairs:
        return False, f"{len(plan.pending_pairs)} pair(s) still awaiting validation"
    expected = plan.expected_file_count()
    if len(written) != expected:
        return False, f"{len(written)} file(s) written, {expected} expected"
    for path in written:
        if not path.is_file():
            return False, f"missing output file: {path.name}"
        if path.stat().st_size == 0:
            return False, f"empty output file: {path.name}"
    if _is_inside(output_directory, pdf_path.parent):
        return False, "the output folder is inside the source folder"
    return True, ""


def move_to_trash(
    pdf_path: Path,
    plan: ExtractionPlan,
    written: list[Path],
    output_directory: Path,
    state: JobState,
    errors: list[str] | None = None,
    trash_sidecar: bool = True,
) -> TrashOutcome:
    """Move a fully processed source, and optionally its sidecar, to the recycle bin."""
    logger = get_logger()
    allowed, reason = check_guards(pdf_path, plan, written, output_directory, state, errors)
    if not allowed:
        logger.info("trash skipped for %s: %s", pdf_path, reason)
        return TrashOutcome(moved=[], skipped=True, reason=reason)

    targets = [pdf_path]
    if trash_sidecar:
        companion = sidecar_path(pdf_path)
        if companion.is_file():
            targets.append(companion)

    moved: list[Path] = []
    for target in targets:
        try:
            send2trash(str(target))
        except Exception as exc:  # locked file, network volume with no recycle bin...
            logger.error("could not move %s to the recycle bin: %s", target, exc)
            return TrashOutcome(
                moved=moved, skipped=True, reason=f"send2trash failed on {target.name}: {exc}"
            )
        moved.append(target)
        logger.info(
            "moved to the recycle bin: %s (%d image(s) produced in %s)",
            target,
            len(written),
            output_directory,
        )
    return TrashOutcome(moved=moved, skipped=False, reason="")
