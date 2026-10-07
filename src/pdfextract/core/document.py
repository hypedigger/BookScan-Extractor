"""PDF opening, page inventory and integrity checks.

Thin wrapper over PyMuPDF that keeps the rest of the engine free of direct
``pymupdf`` calls for the simple operations, and turns library errors into a small
set of explicit exceptions the job runner can report without stopping the queue.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

import pymupdf

PDF_SUFFIXES = (".pdf",)


class PdfDocumentError(Exception):
    """A PDF could not be opened or is unusable."""


class PdfPasswordRequiredError(PdfDocumentError):
    """The PDF is encrypted and no valid password was supplied."""


@dataclass(frozen=True)
class PageInfo:
    """Static description of a single source page."""

    index: int  # zero based, as used by PyMuPDF
    number: int  # one based, as shown to the user and used for naming
    width: float  # in points, as displayed (rotation applied)
    height: float
    rotation: int
    image_count: int

    @property
    def aspect_ratio(self) -> float:
        """Return width divided by height, guarding against a zero height."""
        return self.width / self.height if self.height else 0.0


class PdfDocument:
    """Open a PDF and expose its pages.

    Usable as a context manager::

        with PdfDocument(path) as document:
            for info in document.iter_page_info():
                ...
    """

    def __init__(self, path: Path | str, password: str | None = None) -> None:
        self.path = Path(path)
        self._password = password
        self._doc: pymupdf.Document | None = None
        self._hash: str | None = None

    def __enter__(self) -> PdfDocument:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def open(self) -> None:
        """Open the document, authenticating it when needed."""
        if self._doc is not None:
            return
        if not self.path.is_file():
            raise PdfDocumentError(f"file not found: {self.path}")
        try:
            doc = pymupdf.open(self.path)
        except Exception as exc:  # pymupdf raises a variety of low level errors
            raise PdfDocumentError(f"cannot open {self.path.name}: {exc}") from exc
        if doc.needs_pass and (
            self._password is None or not doc.authenticate(self._password)
        ):
            doc.close()
            raise PdfPasswordRequiredError(f"{self.path.name} is password protected")
        if doc.page_count == 0:
            doc.close()
            raise PdfDocumentError(f"{self.path.name} has no page")
        self._doc = doc

    def close(self) -> None:
        """Close the underlying document if it is open."""
        if self._doc is not None:
            self._doc.close()
            self._doc = None

    @property
    def doc(self) -> pymupdf.Document:
        """Return the underlying PyMuPDF document, opening it on first use."""
        if self._doc is None:
            self.open()
        assert self._doc is not None
        return self._doc

    @property
    def page_count(self) -> int:
        """Return the number of pages."""
        return int(self.doc.page_count)

    @property
    def metadata(self) -> dict[str, str]:
        """Return the document metadata, with None values dropped."""
        raw = self.doc.metadata or {}
        return {key: value for key, value in raw.items() if isinstance(value, str) and value}

    def load_page(self, index: int) -> pymupdf.Page:
        """Return the page at ``index`` (zero based)."""
        if not 0 <= index < self.page_count:
            raise IndexError(f"page index out of range: {index}")
        return self.doc.load_page(index)

    def page_info(self, index: int) -> PageInfo:
        """Return the static description of the page at ``index``."""
        page = self.load_page(index)
        rect = page.rect
        return PageInfo(
            index=index,
            number=index + 1,
            width=float(rect.width),
            height=float(rect.height),
            rotation=int(page.rotation),
            image_count=len(page.get_images(full=True)),
        )

    def iter_page_info(self) -> Iterator[PageInfo]:
        """Yield the description of every page, in document order."""
        for index in range(self.page_count):
            yield self.page_info(index)

    def file_hash(self, chunk_size: int = 1 << 20) -> str:
        """Return the SHA-256 of the file, computed once and cached.

        Used by the sidecar file to check that stored decisions still apply to the
        PDF they were recorded for.
        """
        if self._hash is None:
            digest = hashlib.sha256()
            with self.path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(chunk_size), b""):
                    digest.update(chunk)
            self._hash = digest.hexdigest()
        return self._hash


def iter_pdf_files(
    root: Path | str, recursive: bool = True, sort: bool = True
) -> Iterator[Path]:
    """Yield the PDF files found under ``root``, sorted for a reproducible order.

    ``root`` may be a single file, in which case it is yielded when it is a PDF.
    ``sort`` of False yields them as the walk finds them, without waiting for the
    whole tree to be listed: a scan of a large folder can then report its progress.
    """
    base = Path(root)
    if base.is_file():
        if base.suffix.lower() in PDF_SUFFIXES:
            yield base
        return
    pattern = "**/*" if recursive else "*"
    found = base.glob(pattern)
    for path in sorted(found) if sort else found:
        if path.is_file() and path.suffix.lower() in PDF_SUFFIXES:
            yield path


def probe(path: Path | str, password: str | None = None) -> tuple[bool, str, int]:
    """Check that a PDF can be opened.

    Returns ``(ok, message, page_count)``. Never raises, so the queue can record
    the failure and carry on with the next file.
    """
    try:
        with PdfDocument(path, password) as document:
            return True, "", document.page_count
    except PdfDocumentError as exc:
        return False, str(exc), 0
