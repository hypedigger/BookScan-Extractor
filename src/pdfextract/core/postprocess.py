"""Optional post-processing of an extracted image.

Everything here is disabled by default and each step is independent. The order is
imposed by :func:`apply_chain` and is not a matter of taste: cropping before
deskewing reintroduces white corners that the crop had just removed, and resizing
before anything else throws away the detail the other steps rely on.
"""

from __future__ import annotations

import cv2
import numpy as np

from pdfextract.core.settings import PostProcessConfig

GUTTER_LOCATIONS = ("left", "centre", "right")


def _to_grey(image: np.ndarray) -> np.ndarray:
    """Return a single channel view of an image."""
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image


def _background_is_light(grey: np.ndarray) -> bool:
    """Return True when the page background is lighter than its content."""
    return float(np.median(grey)) > 127.0


def estimate_skew(image: np.ndarray, max_angle: float = 5.0) -> float:
    """Estimate the skew of a page in degrees, clamped to ``max_angle``.

    A positive angle means the content is rotated clockwise.
    """
    grey = _to_grey(image)
    scale = 800.0 / max(grey.shape)
    if scale < 1.0:
        grey = cv2.resize(grey, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    grey = cv2.GaussianBlur(grey, (3, 3), 0)
    flag = cv2.THRESH_BINARY_INV if _background_is_light(grey) else cv2.THRESH_BINARY
    _, mask = cv2.threshold(grey, 0, 255, flag | cv2.THRESH_OTSU)
    coordinates = cv2.findNonZero(mask)
    if coordinates is None or len(coordinates) < 50:
        return 0.0
    angle = float(cv2.minAreaRect(coordinates)[-1])
    if angle > 45.0:
        angle -= 90.0
    elif angle < -45.0:
        angle += 90.0
    return float(np.clip(angle, -max_angle, max_angle))


def deskew(image: np.ndarray, max_angle: float = 5.0) -> np.ndarray:
    """Straighten a page, correcting at most ``max_angle`` degrees."""
    angle = estimate_skew(image, max_angle)
    if abs(angle) < 0.05:
        return image
    height, width = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle, 1.0)
    border = _edge_colour(image)
    return cv2.warpAffine(
        image,
        matrix,
        (width, height),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=border,
    )


def _edge_colour(image: np.ndarray) -> tuple[int, int, int]:
    """Return the dominant colour of the outer border, used to fill new corners."""
    edges = np.concatenate(
        [
            image[:2, :].reshape(-1, image.shape[2]),
            image[-2:, :].reshape(-1, image.shape[2]),
            image[:, :2].reshape(-1, image.shape[2]),
            image[:, -2:].reshape(-1, image.shape[2]),
        ]
    )
    median = np.median(edges, axis=0)
    return int(median[0]), int(median[1]), int(median[2])


def crop_margins(
    image: np.ndarray, threshold: int = 0, margin: int = 8
) -> np.ndarray:
    """Crop the page to its content, keeping ``margin`` pixels of safety.

    ``threshold`` of 0 asks for an automatic Otsu threshold.
    """
    grey = cv2.GaussianBlur(_to_grey(image), (5, 5), 0)
    if threshold > 0:
        flag = cv2.THRESH_BINARY_INV if _background_is_light(grey) else cv2.THRESH_BINARY
        _, mask = cv2.threshold(grey, threshold, 255, flag)
    else:
        flag = cv2.THRESH_BINARY_INV if _background_is_light(grey) else cv2.THRESH_BINARY
        _, mask = cv2.threshold(grey, 0, 255, flag | cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return image
    x, y, width, height = cv2.boundingRect(max(contours, key=cv2.contourArea))
    if width < image.shape[1] * 0.2 or height < image.shape[0] * 0.2:
        return image  # the detection clearly failed, leave the page alone
    left = max(0, x - margin)
    top = max(0, y - margin)
    right = min(image.shape[1], x + width + margin)
    bottom = min(image.shape[0], y + height + margin)
    return image[top:bottom, left:right]


def _shadow_gain(grey: np.ndarray, start: int, stop: int, reference_columns: slice) -> np.ndarray:
    """Return the per-column gain that lifts a darkened band back to the page level."""
    band = grey[:, start:stop].astype(np.float32).mean(axis=0)
    reference = float(np.median(grey[:, reference_columns].astype(np.float32).mean(axis=0)))
    if reference <= 1.0:
        return np.ones(stop - start, dtype=np.float32)
    gain = np.clip(reference / np.maximum(band, 1.0), 1.0, 3.0)
    smoothed = cv2.GaussianBlur(gain.reshape(1, -1), (0, 0), sigmaX=3.0).ravel()
    return smoothed.astype(np.float32)


def remove_gutter_shadow(
    image: np.ndarray, width_ratio: float = 0.06, locations: tuple[str, ...] = GUTTER_LOCATIONS
) -> np.ndarray:
    """Even out the luminance ramp a book gutter leaves on the inner edge.

    The correction is measured, not assumed: an edge that is not darkened gets a
    gain of one and is left untouched. That way the same code handles a merged
    spread, whose gutter sits in the middle, and a single page, whose inner edge the
    engine has no way of knowing.
    """
    grey = _to_grey(image)
    width = grey.shape[1]
    band = max(4, int(width * width_ratio))
    if band * 3 >= width:
        return image
    result = image.astype(np.float32)

    for location in locations:
        if location == "left":
            start, stop = 0, band
            reference = slice(band, 2 * band)
        elif location == "right":
            start, stop = width - band, width
            reference = slice(width - 2 * band, width - band)
        else:
            centre = width // 2
            start, stop = max(0, centre - band), min(width, centre + band)
            reference = slice(max(0, centre - 3 * band), max(1, centre - 2 * band))
        gain = _shadow_gain(grey, start, stop, reference)
        if float(gain.max()) < 1.03:
            continue  # nothing to correct here
        result[:, start:stop] *= gain[None, :, None]
    return np.clip(result, 0, 255).astype(np.uint8)


def normalise_contrast(
    image: np.ndarray, clip_limit: float = 2.0, grid_size: int = 8
) -> np.ndarray:
    """Even out the contrast with CLAHE, on the luminance channel only."""
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=(grid_size, grid_size))
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def resize_to_maximum(image: np.ndarray, maximum: int) -> np.ndarray:
    """Cap the largest dimension of an image, keeping its aspect ratio."""
    height, width = image.shape[:2]
    largest = max(height, width)
    if maximum <= 0 or largest <= maximum:
        return image
    scale = maximum / float(largest)
    return cv2.resize(
        image,
        (max(1, int(round(width * scale))), max(1, int(round(height * scale)))),
        interpolation=cv2.INTER_AREA,
    )


def apply_chain(
    image: np.ndarray, config: PostProcessConfig, merged: bool = False
) -> np.ndarray:
    """Run the enabled steps in the imposed order and return the result."""
    result = image
    if config.deskew_enabled:
        result = deskew(result, config.deskew_max_angle)
    if config.crop_enabled:
        result = crop_margins(result, config.crop_threshold, config.crop_margin_px)
    if config.gutter_shadow_enabled:
        locations = ("centre",) if merged else ("left", "right")
        result = remove_gutter_shadow(result, config.gutter_shadow_width_ratio, locations)
    if config.contrast_enabled:
        result = normalise_contrast(result, config.clahe_clip_limit, config.clahe_grid_size)
    if config.resize_enabled:
        result = resize_to_maximum(result, config.resize_max_dimension)
    return result
