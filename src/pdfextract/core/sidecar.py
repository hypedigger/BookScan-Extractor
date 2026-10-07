"""Persistence of the user decisions, next to the source PDF.

A four hundred page book is not validated in one sitting. Every decision is written
straight away, atomically, so that closing the application, or losing it, never
costs more than nothing. Reopening the same PDF restores the decisions and picks the
validation up where it stopped.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pdfextract.core.job import DECISIONS

if TYPE_CHECKING:  # the runner imports this module, so the dependency only goes one way
    from pdfextract.core.job import ExtractionPlan

SIDECAR_VERSION = 1
SIDECAR_SUFFIX = ".extract.json"

# The registry is one file shared by every worker, so a read, change and write
# has to be serialised or two workers finishing together lose one another's entry.
_REGISTRY_LOCK = threading.RLock()


@dataclass
class Sidecar:
    """What is remembered about a PDF between two sessions."""

    schema_version: int = SIDECAR_VERSION
    pdf_hash: str = ""
    pdf_name: str = ""
    mode: int = 1
    state: str = "pending"
    decisions: dict[str, str] = field(default_factory=dict)  # "0006-0007" -> merge/split
    offsets: dict[str, int] = field(default_factory=dict)  # manual vertical offsets
    output_directory: str = ""
    written: list[str] = field(default_factory=list)
    updated_at: str = ""

    @property
    def decided_count(self) -> int:
        """Return how many pairs have already been decided."""
        return len(self.decisions)


def sidecar_path(pdf_path: Path) -> Path:
    """Return the sidecar path of a PDF: ``<name>.extract.json`` beside it."""
    return pdf_path.with_suffix(pdf_path.suffix + SIDECAR_SUFFIX)


def load(pdf_path: Path) -> Sidecar | None:
    """Load the sidecar of a PDF, or None when there is none or it is unusable."""
    path = sidecar_path(pdf_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("schema_version") != SIDECAR_VERSION:
        return None
    known = {key: raw[key] for key in raw if key in Sidecar.__dataclass_fields__}
    return Sidecar(**known)


def _atomic_write(path: Path, text: str) -> None:
    """Write a file through a uniquely named temporary, then rename it into place.

    The temporary name carries a random suffix: several threads can be writing at
    the same moment, and on Windows a shared temporary name makes one of them fail
    to rename over the other, which is enough to lose a job.
    """
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save(pdf_path: Path, sidecar: Sidecar) -> Path:
    """Write the sidecar atomically: a temporary file, then a rename.

    A brutal shutdown in the middle of a write must not be able to leave a half
    written file behind, because that file is the only record of the decisions.
    """
    sidecar.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    path = sidecar_path(pdf_path)
    _atomic_write(path, json.dumps(asdict(sidecar), indent=2, ensure_ascii=False))
    return path


def from_plan(
    pdf_path: Path,
    pdf_hash: str,
    plan: ExtractionPlan,
    state: str,
    output_directory: Path | None = None,
    written: list[Path] | None = None,
) -> Sidecar:
    """Build a sidecar describing the current state of a job."""
    return Sidecar(
        pdf_hash=pdf_hash,
        pdf_name=pdf_path.name,
        mode=plan.mode,
        state=state,
        decisions={
            pair.label: pair.decision for pair in plan.review if pair.decision is not None
        },
        offsets={pair.label: pair.vertical_offset for pair in plan.review},
        output_directory=str(output_directory) if output_directory else "",
        written=[str(path) for path in (written or [])],
    )


def apply_to_plan(sidecar: Sidecar, plan: ExtractionPlan, pdf_hash: str = "") -> bool:
    """Restore stored decisions onto a freshly built plan.

    Returns False and changes nothing when the sidecar belongs to a different file
    or a different mode: a stale decision applied to the wrong pages would be worse
    than asking the user again.
    """
    if pdf_hash and sidecar.pdf_hash and sidecar.pdf_hash != pdf_hash:
        return False
    if sidecar.mode != plan.mode:
        return False
    for pair in plan.review:
        decision = sidecar.decisions.get(pair.label)
        if decision in DECISIONS:
            pair.decision = decision
        offset = sidecar.offsets.get(pair.label)
        if offset is not None and pair.decision is not None:
            pair.vertical_offset = int(offset)
    return True


def record_decision(
    pdf_path: Path, sidecar: Sidecar, label: str, decision: str, offset: int | None = None
) -> Path:
    """Record one decision and flush it to disk immediately."""
    sidecar.decisions[label] = decision
    if offset is not None:
        sidecar.offsets[label] = offset
    return save(pdf_path, sidecar)


def forget_decision(pdf_path: Path, sidecar: Sidecar, label: str) -> Path:
    """Undo one decision, for the back step of the validation screen."""
    sidecar.decisions.pop(label, None)
    return save(pdf_path, sidecar)


def registry_path() -> Path:
    """Return the file listing the sources still awaiting validation."""
    from pdfextract.core.settings import app_data_dir

    return app_data_dir() / "pending.json"


def _read_registry() -> list[str]:
    """Return the raw registry contents, tolerating a missing or broken file."""
    try:
        raw = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in raw if isinstance(item, str)] if isinstance(raw, list) else []


def _write_registry(entries: list[str]) -> None:
    """Write the registry atomically."""
    _atomic_write(registry_path(), json.dumps(entries, indent=2))


def remember_pending(pdf_path: Path) -> None:
    """Record that a source is waiting for the user."""
    with _REGISTRY_LOCK:
        entries = _read_registry()
        if str(pdf_path) not in entries:
            entries.append(str(pdf_path))
            _write_registry(entries)


def forget_pending(pdf_path: Path) -> None:
    """Record that a source no longer needs the user."""
    with _REGISTRY_LOCK:
        entries = [item for item in _read_registry() if item != str(pdf_path)]
        if len(entries) != len(_read_registry()):
            _write_registry(entries)


def pending_sources() -> list[Path]:
    """Return the sources that were awaiting validation when the application closed.

    Only files that still exist, still have a sidecar and are still in that state are
    returned: the queue is rebuilt from the sidecars, the registry only says where
    to look.
    """
    restored: list[Path] = []
    for item in _read_registry():
        pdf_path = Path(item)
        if not pdf_path.is_file():
            continue
        record = load(pdf_path)
        if record is not None and record.state == "awaiting_validation":
            restored.append(pdf_path)
    return restored


def iter_sidecars(root: Path, recursive: bool = True) -> list[tuple[Path, Sidecar]]:
    """Find the PDF files under ``root`` that have a sidecar, with it.

    Used at start-up to rebuild the validation queue.
    """
    pattern = f"**/*{SIDECAR_SUFFIX}" if recursive else f"*{SIDECAR_SUFFIX}"
    found: list[tuple[Path, Sidecar]] = []
    for path in sorted(root.glob(pattern)):
        pdf_path = path.with_name(path.name[: -len(SIDECAR_SUFFIX)])
        if not pdf_path.is_file():
            continue
        sidecar = load(pdf_path)
        if sidecar is not None:
            found.append((pdf_path, sidecar))
    return found
