"""Merging two half-pages into one spread.

The vertical offset used here is the one returned by the scorer, with the same
convention: a positive offset moves the right page down.
"""

from __future__ import annotations

import cv2
import numpy as np

from pdfextract.core.settings import MergeConfig

WHITE = (255, 255, 255)


def _sampled_fill(left: np.ndarray, right: np.ndarray) -> tuple[int, int, int]:
    """Return the average colour of the outer edges, used to fill the gaps."""
    samples = np.concatenate(
        [
            left[:, :4].reshape(-1, 3),
            right[:, -4:].reshape(-1, 3),
            left[:4, :].reshape(-1, 3),
            right[:4, :].reshape(-1, 3),
        ]
    )
    mean = samples.mean(axis=0)
    return int(mean[0]), int(mean[1]), int(mean[2])


def _trim_gutter(image: np.ndarray, side: str, pixels: int) -> np.ndarray:
    """Remove a strip from the inner edge of a page before gluing it."""
    if pixels <= 0 or pixels >= image.shape[1] - 1:
        return image
    if side == "left":  # the inner edge of a left hand page is its right side
        return image[:, : image.shape[1] - pixels]
    return image[:, pixels:]


def merge_pages(
    left: np.ndarray,
    right: np.ndarray,
    vertical_offset: int = 0,
    config: MergeConfig | None = None,
) -> np.ndarray:
    """Glue two half-pages side by side, left first, honouring the vertical offset.

    The two halves rarely have the same height, and the offset shifts one of them
    further, so the canvas is as tall as the tallest placement and the gaps are
    filled rather than cropped: cropping would silently lose content.
    """
    config = config or MergeConfig()
    if left.ndim != 3 or right.ndim != 3:
        raise ValueError("merge_pages expects two colour images")

    trim = max(0, config.gutter_trim_px)
    left_page = _trim_gutter(left, "left", trim)
    right_page = _trim_gutter(right, "right", trim)

    offset = vertical_offset if config.apply_vertical_offset else 0
    left_top = max(0, -offset)
    right_top = max(0, offset)
    height = max(left_top + left_page.shape[0], right_top + right_page.shape[0])
    width = left_page.shape[1] + right_page.shape[1]

    fill = _sampled_fill(left_page, right_page) if config.fill_mode == "sampled" else WHITE
    canvas = np.full((height, width, 3), fill, dtype=np.uint8)
    canvas[left_top : left_top + left_page.shape[0], : left_page.shape[1]] = left_page
    canvas[right_top : right_top + right_page.shape[0], left_page.shape[1] :] = right_page
    return canvas


def merge_preview(
    left: np.ndarray, right: np.ndarray, vertical_offset: int, height: int = 600
) -> np.ndarray:
    """Return a reduced merge, for debugging tools only.

    The validation screen deliberately shows the two pages as they are rather than a
    preview of the merge, so this is never used by the interface.
    """
    merged = merge_pages(left, right, vertical_offset)
    scale = height / merged.shape[0]
    return cv2.resize(merged, (max(1, int(merged.shape[1] * scale)), height))
