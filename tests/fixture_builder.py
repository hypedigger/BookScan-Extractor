"""Synthetic fixture generation.

Builds page images whose expected seam score is known by construction: a single
photograph cut in two halves must score high, two unrelated pages must score low.
Variants add the defects of a real scan: noise, vertical misalignment, gutter
shadow, scan border.
"""

from __future__ import annotations

import io
from pathlib import Path

import cv2
import numpy as np
import pymupdf
from PIL import Image

DEFAULT_WIDTH = 1600
DEFAULT_HEIGHT = 1200


def _rng(seed: int) -> np.random.Generator:
    """Return a deterministic random generator."""
    return np.random.default_rng(seed)


def make_photo(
    width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT, seed: int = 0
) -> np.ndarray:
    """Build a photograph-like image: smooth background plus shapes that span it.

    The shapes are deliberately wide, so that cutting the image in two halves leaves
    contours that continue from one half into the other.
    """
    rng = _rng(seed)
    xs = np.linspace(0, 1, width, dtype=np.float32)
    ys = np.linspace(0, 1, height, dtype=np.float32)
    grid_x, grid_y = np.meshgrid(xs, ys)
    base = np.stack(
        [
            0.5 + 0.5 * np.sin(6.0 * grid_x + 1.3 * float(rng.random())),
            0.5 + 0.5 * np.cos(4.0 * grid_y + 2.1 * float(rng.random())),
            0.5 + 0.5 * np.sin(3.0 * (grid_x + grid_y)),
        ],
        axis=-1,
    )
    image = (base * 255).astype(np.uint8)

    # Strokes are kept close to horizontal on purpose. A steep line drifts
    # vertically as it crosses the strip skipped at the gutter, which shifts the
    # best local alignment away from the real misregistration of the two scans; no
    # scorer can tell the two apart from the edge bands alone, so a fixture made of
    # steep diagonals would be testing something impossible.
    max_slope = height * 0.08
    for _ in range(14):
        colour = tuple(int(value) for value in rng.integers(0, 255, size=3))
        y = int(rng.integers(0, height))
        end = int(np.clip(y + rng.uniform(-max_slope, max_slope), 0, height - 1))
        thickness = int(rng.integers(3, 18))
        cv2.line(image, (0, y), (width - 1, end), colour, thickness)
    for _ in range(8):
        colour = tuple(int(value) for value in rng.integers(0, 255, size=3))
        centre = (int(rng.integers(0, width)), int(rng.integers(0, height)))
        radius = int(rng.integers(height // 10, height // 3))
        cv2.circle(image, centre, radius, colour, int(rng.integers(4, 20)))
    return image


def make_text_page(
    width: int = DEFAULT_WIDTH // 2, height: int = DEFAULT_HEIGHT, seed: int = 0
) -> np.ndarray:
    """Build a text-like page: white background, dark lines, generous margins."""
    rng = _rng(seed)
    image = np.full((height, width, 3), 250, np.uint8)
    margin_x = int(width * 0.12)
    y = int(height * 0.12)
    while y < height * 0.9:
        line_width = int((width - 2 * margin_x) * float(rng.uniform(0.45, 1.0)))
        thickness = int(height * 0.012)
        cv2.rectangle(
            image,
            (margin_x, y),
            (margin_x + line_width, y + thickness),
            (40, 40, 40),
            -1,
        )
        y += int(height * 0.035)
    return image


def split_spread(image: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Cut a spread in two halves: left page then right page."""
    middle = image.shape[1] // 2
    return image[:, :middle].copy(), image[:, middle:].copy()


def add_noise(image: np.ndarray, sigma: float = 6.0, seed: int = 0) -> np.ndarray:
    """Add Gaussian noise, as a scanner would."""
    rng = _rng(seed)
    noisy = image.astype(np.float32) + rng.normal(0.0, sigma, image.shape).astype(np.float32)
    return np.clip(noisy, 0, 255).astype(np.uint8)


def shift_vertically(image: np.ndarray, dy: int, fill: int = 255) -> np.ndarray:
    """Shift an image vertically, as two halves of a scan are never quite aligned."""
    if dy == 0:
        return image.copy()
    shifted = np.full_like(image, fill)
    if dy > 0:
        shifted[dy:, :] = image[: image.shape[0] - dy, :]
    else:
        shifted[: image.shape[0] + dy, :] = image[-dy:, :]
    return shifted


def add_gutter_shadow(
    image: np.ndarray, side: str, width_ratio: float = 0.06, strength: float = 0.55
) -> np.ndarray:
    """Darken the inner edge of a page, the way a book gutter does.

    ``side`` is ``"right"`` for a left-hand page and ``"left"`` for a right-hand one.
    """
    width = image.shape[1]
    band = max(1, int(width * width_ratio))
    ramp = np.linspace(1.0 - strength, 1.0, band, dtype=np.float32)
    gain = np.ones(width, dtype=np.float32)
    if side == "right":
        gain[width - band :] = ramp[::-1]
    else:
        gain[:band] = ramp
    result = image.astype(np.float32) * gain[None, :, None]
    return np.clip(result, 0, 255).astype(np.uint8)


def add_scan_border(
    image: np.ndarray, thickness: int = 12, value: int = 15, inner_side: str | None = None
) -> np.ndarray:
    """Add the dark strip a flatbed scanner leaves around the page.

    ``inner_side`` names the gutter side of the page, which is left alone: a scanner
    strip appears around the outside of a scan, never in the middle of a spread.
    Covering the gutter side would destroy the very content the scorer compares.
    """
    bordered = image.copy()
    bordered[:thickness, :] = value
    bordered[-thickness:, :] = value
    if inner_side != "left":
        bordered[:, :thickness] = value
    if inner_side != "right":
        bordered[:, -thickness:] = value
    return bordered


def encode_jpeg(image: np.ndarray, quality: int = 92) -> bytes:
    """Encode a BGR array as JPEG bytes."""
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="JPEG", quality=quality, subsampling=0)
    return buffer.getvalue()


def encode_png(image: np.ndarray) -> bytes:
    """Encode a BGR array as PNG bytes."""
    rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="PNG")
    return buffer.getvalue()


def build_pdf(
    path: Path,
    pages: list[np.ndarray],
    dpi: int = 150,
    rotation: int = 0,
    lossless: bool = False,
) -> Path:
    """Build a PDF holding one full page bitmap per page, as a scanner produces."""
    document = pymupdf.open()
    for image in pages:
        height, width = image.shape[:2]
        rect = pymupdf.Rect(0, 0, width * 72.0 / dpi, height * 72.0 / dpi)
        page = document.new_page(width=rect.width, height=rect.height)
        stream = encode_png(image) if lossless else encode_jpeg(image)
        page.insert_image(page.rect, stream=stream)
        if rotation:
            page.set_rotation(rotation)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path, deflate=True)
    document.close()
    return path


SHIFT_MARGIN = 48


def spread_pages(seed: int = 0, **defects: float) -> tuple[np.ndarray, np.ndarray]:
    """Return the two halves of one continuous spread, with optional defects.

    Supported keyword defects: ``noise`` (sigma), ``dy`` (vertical shift of the right
    half), ``shadow`` (strength), ``border`` (thickness).

    The misalignment is produced by taking a different vertical window of a taller
    source image, exactly as a scanner that framed the two pages slightly
    differently would. Shifting a finished half and padding the gap would add a
    uniform strip that the scan border trimming removes, silently undoing the shift.
    """
    noise = float(defects.get("noise", 0.0) or 0.0)
    dy = int(defects.get("dy", 0) or 0)
    shadow = float(defects.get("shadow", 0.0) or 0.0)
    border = int(defects.get("border", 0) or 0)

    tall = make_photo(height=DEFAULT_HEIGHT + 2 * SHIFT_MARGIN, seed=seed)
    left_full, right_full = split_spread(tall)
    top = SHIFT_MARGIN
    left = left_full[top : top + DEFAULT_HEIGHT].copy()
    right_top = int(np.clip(top - dy, 0, 2 * SHIFT_MARGIN))
    right = right_full[right_top : right_top + DEFAULT_HEIGHT].copy()
    if shadow:
        left = add_gutter_shadow(left, "right", strength=shadow)
        right = add_gutter_shadow(right, "left", strength=shadow)
    if noise:
        left = add_noise(left, noise, seed=seed)
        right = add_noise(right, noise, seed=seed + 1)
    if border:
        # The gutter side of each page is the inside of the spread: no strip there.
        left = add_scan_border(left, border, inner_side="right")
        right = add_scan_border(right, border, inner_side="left")
    return left, right


def unrelated_pages(seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """Return two pages that have nothing to do with each other."""
    left = make_text_page(seed=seed)
    right = split_spread(make_photo(seed=seed + 100))[1]
    right = cv2.resize(right, (left.shape[1], left.shape[0]))
    return left, right
