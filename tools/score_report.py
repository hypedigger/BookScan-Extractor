"""Seam scoring debug tool.

Prints the score of every adjacent pair of a PDF with the detail of each metric, so
the weights and thresholds can be calibrated on real files. With ``--plate`` it also
writes a contact sheet showing each join, which is the fastest way to see where the
scorer is wrong.

Usage::

    uv run python tools/score_report.py book.pdf --plate report.jpg
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pdfextract.core.document import PdfDocument  # noqa: E402
from pdfextract.core.extractor import load_page_array  # noqa: E402
from pdfextract.core.seam import score_seam  # noqa: E402
from pdfextract.core.settings import load_settings  # noqa: E402

METRIC_ORDER = (
    "row_correlation",
    "colour_histogram",
    "gradient_jump",
    "edge_continuity",
    "content_type",
    "geometry",
    "band_activity",
    "veto",
)

THUMBNAIL_HEIGHT = 220


def _thumbnail(left: np.ndarray, right: np.ndarray, caption: str) -> np.ndarray:
    """Build one row of the contact sheet: the two inner edges, side by side."""
    def fit(image: np.ndarray) -> np.ndarray:
        scale = THUMBNAIL_HEIGHT / image.shape[0]
        return cv2.resize(image, (max(1, int(image.shape[1] * scale)), THUMBNAIL_HEIGHT))

    pair = np.hstack([fit(left), np.full((THUMBNAIL_HEIGHT, 4, 3), 180, np.uint8), fit(right)])
    strip = np.full((THUMBNAIL_HEIGHT + 26, pair.shape[1], 3), 255, np.uint8)
    strip[26:, :] = pair
    cv2.putText(strip, caption, (6, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
    return strip


def main(argv: list[str] | None = None) -> int:
    """Score every adjacent pair of the given PDF files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--plate", type=Path, default=None, help="write a contact sheet")
    parser.add_argument("--settings", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=0, help="stop after N pairs")
    args = parser.parse_args(argv)

    settings = load_settings(args.settings)
    rows: list[np.ndarray] = []
    header = f"{'pair':>12} {'score':>6} {'dy':>5} {'decision':>10}  " + " ".join(
        f"{name[:9]:>9}" for name in METRIC_ORDER
    )
    for path in args.paths:
        print(f"\n{path}")
        print(header)
        with PdfDocument(path) as document:
            previous = None
            count = 0
            for index in range(document.page_count):
                current = load_page_array(document.load_page(index), settings.extraction)
                if previous is not None:
                    score = score_seam(previous, current, settings.seam)
                    metrics = " ".join(
                        f"{score.metrics.get(name, float('nan')):>9.3f}" for name in METRIC_ORDER
                    )
                    label = f"{index}-{index + 1}"
                    print(
                        f"{label:>12} {score.value:>6.3f} {score.vertical_offset:>5} "
                        f"{score.confidence:>10}  {metrics}"
                    )
                    if args.plate is not None:
                        rows.append(
                            _thumbnail(
                                previous,
                                current,
                                f"{path.stem} {label}  score={score.value:.3f} "
                                f"dy={score.vertical_offset} {score.confidence}",
                            )
                        )
                    count += 1
                    if args.limit and count >= args.limit:
                        break
                previous = current

    if args.plate is not None and rows:
        width = max(row.shape[1] for row in rows)
        padded = [
            np.pad(row, ((0, 0), (0, width - row.shape[1]), (0, 0)), constant_values=255)
            for row in rows
        ]
        args.plate.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(args.plate), np.vstack(padded))
        print(f"\ncontact sheet: {args.plate}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
