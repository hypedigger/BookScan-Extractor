"""Generate sample PDF files for manual testing and calibration.

Usage::

    uv run python tools/make_fixtures.py --out tests/fixtures/generated
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from fixture_builder import (  # noqa: E402
    build_pdf,
    make_text_page,
    spread_pages,
    unrelated_pages,
)


def build_book(path: Path, rotation: int = 0) -> Path:
    """Build a small book: a fly-leaf, two spreads, two text pages and a last spread."""
    pages = [make_text_page(seed=1)]  # fly-leaf, exported on its own
    left, right = spread_pages(seed=2, noise=4.0, dy=6, shadow=0.35)
    pages += [left, right]
    left, right = spread_pages(seed=3, noise=4.0, dy=-4, shadow=0.4)
    pages += [left, right]
    pages += [make_text_page(seed=4), make_text_page(seed=5)]
    left, right = spread_pages(seed=6, noise=3.0, shadow=0.3)
    pages += [left, right]
    return build_pdf(path, pages, rotation=rotation)


def build_pairs(directory: Path) -> list[Path]:
    """Build one tiny PDF per expected outcome, for threshold calibration."""
    written: list[Path] = []
    left, right = spread_pages(seed=10)
    written.append(build_pdf(directory / "pair_continuous.pdf", [left, right]))
    left, right = spread_pages(seed=11, noise=8.0, dy=10, shadow=0.5, border=10)
    written.append(build_pdf(directory / "pair_continuous_noisy.pdf", [left, right]))
    left, right = unrelated_pages(seed=12)
    written.append(build_pdf(directory / "pair_unrelated.pdf", [left, right]))
    return written


def main(argv: list[str] | None = None) -> int:
    """Generate the fixture files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "tests" / "fixtures" / "generated")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)

    written = [build_book(args.out / "book.pdf")]
    written.append(build_book(args.out / "book_rotated.pdf", rotation=90))
    written += build_pairs(args.out)
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
