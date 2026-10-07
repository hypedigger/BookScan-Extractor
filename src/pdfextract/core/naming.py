"""Filename template engine.

Numbering is based on the source page number, never on the order in which files
happen to be written. Safe pages are exported immediately while ambiguous ones wait
for manual validation, so a sequential counter would number the output in an order
that has nothing to do with the order of the book. The source page number, on the
other hand, is known as soon as the document is analysed and never moves, so an
alphabetical sort of the output folder always matches the reading order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

PAGE_DIGITS = 4
MAX_PATH_LENGTH = 255

FORBIDDEN_CHARACTERS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
TOKEN_PATTERN = re.compile(r"\{(\w+)\}")

RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

KNOWN_TOKENS = frozenset(
    {"pdf", "pages", "page_start", "page_end", "type", "mode", "date", "index"}
)

DEFAULT_TEMPLATE = "{pdf}_{pages}"


class NamingError(ValueError):
    """The template contains an unknown token, or produces an empty name."""


@dataclass(frozen=True)
class NameContext:
    """Everything a template can refer to for one output file."""

    pdf_stem: str
    page_start: int
    page_end: int | None = None
    mode: int = 1
    index: int = 0
    processed_on: date | None = None

    @property
    def is_merged(self) -> bool:
        """Return True when the output covers two source pages."""
        return self.page_end is not None and self.page_end != self.page_start


def format_pages(start: int, end: int | None = None) -> str:
    """Return the ``{pages}`` token: ``0012`` for a single page, ``0012-0013`` merged."""
    if end is None or end == start:
        return f"{start:0{PAGE_DIGITS}d}"
    return f"{start:0{PAGE_DIGITS}d}-{end:0{PAGE_DIGITS}d}"


def token_values(context: NameContext) -> dict[str, str]:
    """Return the value of every token for a given context."""
    day = context.processed_on or date.today()
    return {
        "pdf": context.pdf_stem,
        "pages": format_pages(context.page_start, context.page_end),
        "page_start": f"{context.page_start:0{PAGE_DIGITS}d}",
        "page_end": (
            f"{context.page_end:0{PAGE_DIGITS}d}" if context.page_end is not None else ""
        ),
        "type": "merged" if context.is_merged else "single",
        "mode": str(context.mode),
        "date": day.isoformat(),
        "index": f"{context.index:0{PAGE_DIGITS}d}",
    }


def template_tokens(template: str) -> list[str]:
    """Return the tokens used by a template, in order of appearance."""
    return TOKEN_PATTERN.findall(template)


def validate_template(template: str) -> list[str]:
    """Return human readable warnings about a template.

    Unknown tokens raise; the returned list only holds advisory messages, which the
    interface displays next to the template field.
    """
    unknown = [name for name in template_tokens(template) if name not in KNOWN_TOKENS]
    if unknown:
        raise NamingError("unknown token(s): " + ", ".join(sorted(set(unknown))))
    warnings: list[str] = []
    used = set(template_tokens(template))
    if "index" in used:
        warnings.append(
            "The {index} token numbers files in the order they are written, which only "
            "matches the order of the book in mode 1. Prefer {pages}."
        )
    if not used & {"pages", "page_start", "index"}:
        warnings.append(
            "The template has no page token: every page of a PDF would collide on the "
            "same name and be suffixed."
        )
    return warnings


def sanitise_filename(name: str) -> str:
    """Make a name safe for Windows: forbidden characters, trailing dots, device names."""
    cleaned = FORBIDDEN_CHARACTERS.sub("_", name).strip()
    cleaned = cleaned.rstrip(". ")
    if not cleaned:
        cleaned = "untitled"
    if cleaned.split(".")[0].upper() in RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def render_template(template: str, context: NameContext) -> str:
    """Expand a template into a sanitised base name, without extension."""
    values = token_values(context)
    unknown = [name for name in template_tokens(template) if name not in values]
    if unknown:
        raise NamingError("unknown token(s): " + ", ".join(sorted(set(unknown))))

    def replace(match: re.Match[str]) -> str:
        return values[match.group(1)]

    rendered = TOKEN_PATTERN.sub(replace, template)
    rendered = re.sub(r"_{2,}", "_", rendered).strip("_ ")
    return sanitise_filename(rendered)


def _truncate_for_path(directory: Path, stem: str, suffix: str) -> str:
    """Shorten a stem so that the resulting full path fits within the path limit."""
    fixed = len(str(directory)) + 1 + len(suffix)
    room = MAX_PATH_LENGTH - fixed
    if room < 8:
        room = 8
    return stem if len(stem) <= room else stem[:room]


def build_output_path(
    directory: Path,
    stem: str,
    ext: str,
    taken: set[Path] | None = None,
    overwrite: bool = False,
) -> Path:
    """Return a free output path, handling truncation and collisions.

    ``taken`` holds paths already allocated during this run but possibly not yet
    written, so that two jobs cannot pick the same name.
    """
    suffix = f".{ext.lstrip('.')}"
    safe_stem = _truncate_for_path(directory, sanitise_filename(stem), suffix)
    candidate = directory / f"{safe_stem}{suffix}"
    if overwrite:
        return candidate
    counter = 2
    while candidate.exists() or (taken is not None and candidate in taken):
        marker = f"_{counter}"
        base = _truncate_for_path(directory, safe_stem, marker + suffix)
        candidate = directory / f"{base}{marker}{suffix}"
        counter += 1
    if taken is not None:
        taken.add(candidate)
    return candidate


def output_directory(
    root: Path, pdf_path: Path, source_root: Path | None, subfolder_per_pdf: bool,
    mirror_source_tree: bool,
) -> Path:
    """Return the directory where the images of ``pdf_path`` are written.

    When a folder was imported recursively, the relative tree of the sources is
    reproduced under the output root.
    """
    target = root
    if mirror_source_tree and source_root is not None:
        try:
            relative = pdf_path.parent.relative_to(source_root)
        except ValueError:
            relative = Path()
        target = target / relative
    if subfolder_per_pdf:
        target = target / sanitise_filename(pdf_path.stem)
    return target
