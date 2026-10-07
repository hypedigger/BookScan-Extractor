"""Gutter continuity scoring.

This module answers one question: do the inner edges of two consecutive pages
belong to the same image? It feeds both mode 2, where it decides the starting
offset of a systematic pairing, and mode 3, where it decides pair by pair.

Two conventions matter here.

``vertical_offset`` is the number of pixels the right page must be moved **down** to
line up with the left one, so that row ``y`` of the left page matches row
``y - vertical_offset`` of the right page. The merger reuses that value as is.

The comparison never uses the very last column of a page: a book scan carries a
curved shadow and a distortion along the gutter, so a strip is skipped on each
side. That skip leaves a real gap of content between the two compared bands, and
every metric is told about it rather than pretending the two columns were adjacent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from pdfextract.core.settings import SeamConfig

MERGE = "merge"
SPLIT = "split"
UNCERTAIN = "uncertain"

_EPSILON = 1e-6


@dataclass(frozen=True)
class SeamScore:
    """Result of comparing the inner edges of two consecutive pages."""

    value: float  # 0.0 (unrelated) to 1.0 (perfectly continuous)
    vertical_offset: int  # best vertical alignment found, in source pixels
    metrics: dict[str, float] = field(default_factory=dict)
    confidence: str = UNCERTAIN

    @property
    def merges(self) -> bool:
        """Return True when the pair can be merged without asking the user."""
        return self.confidence == MERGE

    @property
    def splits(self) -> bool:
        """Return True when the pair can be split without asking the user."""
        return self.confidence == SPLIT


@dataclass(frozen=True)
class _Prepared:
    """A page reduced to what the metrics need."""

    grey: np.ndarray  # normalised height, borders trimmed
    colour: np.ndarray  # same geometry, reduced colour copy
    original_height: int
    original_width: int
    scale: float  # normalised height / original height


@dataclass(frozen=True)
class _Edge:
    """The inner edge of a page, ready to be compared."""

    band_grey: np.ndarray  # narrow strip used for correlation and colour
    band_colour: np.ndarray
    context_grey: np.ndarray  # wider strip used for gradient and contours
    gap: int  # columns skipped between this band and the physical page edge


def classify(value: float, config: SeamConfig) -> str:
    """Turn a score into a decision using the two configured thresholds."""
    if value >= config.threshold_merge:
        return MERGE
    if value <= config.threshold_split:
        return SPLIT
    return UNCERTAIN


def _trim_run(means: np.ndarray, deviations: np.ndarray, limit: int) -> int:
    """Return how many lines to drop from the start of a sequence of page lines.

    A scanner strip is a uniform plateau, either black or blown out white. The run
    stops at the first line that is no longer uniform: the page content next to the
    strip is under no obligation to resemble the middle of the page, so anything
    more eager than this eats real content.
    """
    count = 0
    while (
        count < limit
        and deviations[count] < 12.0
        and (means[count] < 45.0 or means[count] > 240.0)
    ):
        count += 1
    return count


def _trim_scan_border(grey: np.ndarray, max_ratio: float) -> tuple[int, int, int, int]:
    """Return the crop bounds that drop the strip left by the scanner.

    Returns ``(top, bottom, left, right)`` as indices usable for slicing.
    """
    height, width = grey.shape[:2]
    max_v = max(1, int(height * max_ratio))
    max_h = max(1, int(width * max_ratio))
    values = grey.astype(np.float32)

    row_means = values.mean(axis=1)
    row_deviations = values.std(axis=1)
    column_means = values.mean(axis=0)
    column_deviations = values.std(axis=0)

    top = _trim_run(row_means, row_deviations, max_v)
    bottom = height - _trim_run(row_means[::-1], row_deviations[::-1], max_v)
    left = _trim_run(column_means, column_deviations, max_h)
    right = width - _trim_run(column_means[::-1], column_deviations[::-1], max_h)
    if bottom - top < height // 2 or right - left < width // 2:
        return 0, height, 0, width
    return top, bottom, left, right


def prepare(image: np.ndarray, config: SeamConfig) -> _Prepared:
    """Normalise a page for analysis: fixed height, trimmed border, grey and colour.

    Normalising the height makes the score independent of the scan resolution and
    keeps the cost of the comparison constant.
    """
    original_height, original_width = image.shape[:2]
    target = max(64, config.analysis_height)
    scale = target / float(original_height) if original_height else 1.0
    width = max(8, int(round(original_width * scale)))
    reduced = cv2.resize(image, (width, target), interpolation=cv2.INTER_AREA)
    grey = cv2.cvtColor(reduced, cv2.COLOR_BGR2GRAY)
    if config.trim_scan_border:
        top, bottom, left, right = _trim_scan_border(grey, config.border_trim_max_ratio)
        grey = grey[top:bottom, left:right]
        reduced = reduced[top:bottom, left:right]
    return _Prepared(
        grey=grey,
        colour=reduced,
        original_height=original_height,
        original_width=original_width,
        scale=scale,
    )


def _inner_edge(page: _Prepared, config: SeamConfig, side: str) -> _Edge:
    """Extract the inner edge of a page, skipping the gutter strip itself."""
    width = page.grey.shape[1]
    skip = max(0, min(int(round(width * config.gutter_skip_ratio)), max(0, width // 4)))
    band = max(2, min(config.band_px, max(2, width - skip - 1)))
    context = max(band, min(3 * max(band, 2 * skip + 1), width - skip))

    if side == "left":  # the inner edge of a left hand page is its right side
        band_end = max(band, width - skip)
        band_columns = slice(band_end - band, band_end)
        context_columns = slice(max(0, band_end - context), band_end)
    else:
        band_start = min(skip, max(0, width - band))
        band_columns = slice(band_start, band_start + band)
        context_columns = slice(band_start, min(width, band_start + context))

    return _Edge(
        band_grey=page.grey[:, band_columns],
        band_colour=page.colour[:, band_columns],
        context_grey=page.grey[:, context_columns],
        gap=skip,
    )


def _profile(band: np.ndarray) -> np.ndarray:
    """Return the per-row mean of a band, as a float profile."""
    return band.astype(np.float32).mean(axis=1)


def _detrend(profile: np.ndarray, window: int) -> np.ndarray:
    """Remove the slow component of a profile, keeping the structure that aligns.

    Without this, a smooth gradient correlates with itself at every offset and the
    alignment search has no peak to lock on to.
    """
    if profile.size < 8:
        return profile
    size = max(3, min(window | 1, profile.size // 2 * 2 - 1))
    column = profile.reshape(-1, 1)
    smooth = cv2.blur(column, (1, size), borderType=cv2.BORDER_REFLECT).ravel()
    return profile - smooth


def _pearson(a: np.ndarray, b: np.ndarray) -> float:
    """Return the Pearson correlation of two vectors, 0.0 when undefined."""
    if a.size < 8 or b.size < 8:
        return 0.0
    a_centred = a - a.mean()
    b_centred = b - b.mean()
    denominator = float(np.linalg.norm(a_centred) * np.linalg.norm(b_centred))
    if denominator < _EPSILON:
        return 0.0
    return float(np.dot(a_centred, b_centred) / denominator)


def _overlap(a: np.ndarray, b: np.ndarray, offset: int) -> tuple[np.ndarray, np.ndarray]:
    """Return the overlapping parts of two arrays for a given vertical offset."""
    length = min(a.shape[0], b.shape[0])
    a = a[:length]
    b = b[:length]
    if offset >= 0:
        return a[offset:], b[: length - offset]
    return a[: length + offset], b[-offset:]


def _search_offset(
    left_profile: np.ndarray, right_profile: np.ndarray, config: SeamConfig
) -> tuple[int, float]:
    """Find the vertical offset that best aligns the two edge profiles.

    The two halves of a scan are never perfectly aligned, so the search sweeps a few
    percent of the page height. Near equal candidates are broken in favour of the
    smallest shift, which keeps a flat correlation curve from picking a random
    offset at the edge of the search range.
    """
    left_detrended = _detrend(left_profile, config.detrend_window)
    right_detrended = _detrend(right_profile, config.detrend_window)
    height = min(left_detrended.shape[0], right_detrended.shape[0])
    limit = max(1, int(round(height * config.max_offset_ratio)))
    step = max(1, config.offset_step)

    def evaluate(offset: int) -> tuple[float, float]:
        a, b = _overlap(left_detrended, right_detrended, offset)
        raw = _pearson(a, b)
        penalty = config.offset_penalty * abs(offset) / limit
        return raw - penalty, raw

    best_offset, (best_adjusted, best_raw) = 0, evaluate(0)
    for offset in range(-limit, limit + 1, step):
        adjusted, raw = evaluate(offset)
        if adjusted > best_adjusted:
            best_offset, best_adjusted, best_raw = offset, adjusted, raw
    if step > 1:  # refine around the coarse optimum
        for offset in range(best_offset - step + 1, best_offset + step):
            if abs(offset) > limit:
                continue
            adjusted, raw = evaluate(offset)
            if adjusted > best_adjusted:
                best_offset, best_adjusted, best_raw = offset, adjusted, raw
    return best_offset, best_raw


def _activity(profile: np.ndarray, config: SeamConfig) -> float:
    """Return how much structure an edge profile carries.

    Averaging over the width of the band cancels the scanner noise, so a blank
    margin lands near zero whereas any real content stays well above.
    """
    return float(_detrend(profile, config.detrend_window).std())


def _metric_colour_histogram(left: np.ndarray, right: np.ndarray) -> float | None:
    """Compare the colour of the two edge bands.

    Thin bands make a full histogram comparison brittle, so a smoothed hue and
    saturation histogram is combined with a plain mean colour distance.
    """
    if left.size == 0 or right.size == 0:
        return None
    left_hsv = cv2.cvtColor(left, cv2.COLOR_BGR2HSV)
    right_hsv = cv2.cvtColor(right, cv2.COLOR_BGR2HSV)
    bins = [16, 16]
    ranges = [0, 180, 0, 256]
    left_hist = cv2.calcHist([left_hsv], [0, 1], None, bins, ranges)
    right_hist = cv2.calcHist([right_hsv], [0, 1], None, bins, ranges)
    left_hist = cv2.GaussianBlur(left_hist, (3, 3), 0)
    right_hist = cv2.GaussianBlur(right_hist, (3, 3), 0)
    cv2.normalize(left_hist, left_hist)
    cv2.normalize(right_hist, right_hist)
    correlation = max(0.0, float(cv2.compareHist(left_hist, right_hist, cv2.HISTCMP_CORREL)))

    left_mean = left.reshape(-1, 3).mean(axis=0)
    right_mean = right.reshape(-1, 3).mean(axis=0)
    distance = float(np.abs(left_mean - right_mean).mean())
    mean_term = float(np.clip(1.0 - distance / 40.0, 0.0, 1.0))
    return float(np.clip(0.5 * correlation + 0.5 * mean_term, 0.0, 1.0))


def _metric_gradient_jump(left: np.ndarray, right: np.ndarray, lag: int) -> float | None:
    """Compare the step across the join with the internal step over the same distance.

    The two compared columns are ``lag`` pixels apart in the original scan, because
    the gutter strip was skipped on each side. Comparing that step with an adjacent
    column difference would flag every genuine spread as discontinuous, so the
    reference is measured over the same distance.
    """
    if left.shape[1] < 2 or right.shape[1] < 2 or left.shape[0] < 8:
        return None
    lag = max(1, min(lag, left.shape[1] - 1, right.shape[1] - 1))
    seam = np.abs(right[:, 0].astype(np.float32) - left[:, -1].astype(np.float32))
    internal = np.concatenate(
        [
            np.abs(
                left[:, lag:].astype(np.float32) - left[:, :-lag].astype(np.float32)
            ).ravel(),
            np.abs(
                right[:, lag:].astype(np.float32) - right[:, :-lag].astype(np.float32)
            ).ravel(),
        ]
    )
    if internal.size == 0:
        return None
    seam_level = float(np.median(seam))
    # The 80th centile rather than the median: a genuine join often falls on a
    # busier part of the image than the typical pixel pair.
    reference = max(float(np.percentile(internal, 80)), 2.0)
    ratio = seam_level / reference
    return float(np.clip(1.0 / (1.0 + max(0.0, ratio - 1.0)), 0.0, 1.0))


def _metric_edge_continuity(
    left: np.ndarray, right: np.ndarray, tolerance: int
) -> float | None:
    """Count the contours that reach the join at matching rows on both sides."""
    if left.shape[0] < 16 or left.shape[1] < 3 or right.shape[1] < 3:
        return None
    left_edges = cv2.Canny(cv2.GaussianBlur(left, (3, 3), 0), 60, 160)
    right_edges = cv2.Canny(cv2.GaussianBlur(right, (3, 3), 0), 60, 160)
    left_rows = left_edges[:, -1] > 0
    right_rows = right_edges[:, 0] > 0
    if left_rows.sum() < 3 or right_rows.sum() < 3:
        return None  # nothing reaches the join: the metric has nothing to say

    tolerance = max(1, tolerance)
    kernel = np.ones(2 * tolerance + 1, dtype=np.uint8)
    left_dilated = np.convolve(left_rows.astype(np.uint8), kernel, mode="same") > 0
    right_dilated = np.convolve(right_rows.astype(np.uint8), kernel, mode="same") > 0
    matched_left = float((left_rows & right_dilated).sum()) / max(1, int(left_rows.sum()))
    matched_right = float((right_rows & left_dilated).sum()) / max(1, int(right_rows.sum()))
    return float(np.clip(0.5 * (matched_left + matched_right), 0.0, 1.0))


def _ink_ratio(grey: np.ndarray) -> tuple[float, float]:
    """Return the fraction of non-background pixels and the local variance level."""
    background = float(np.median(grey))
    ink = float(np.mean(np.abs(grey.astype(np.float32) - background) > 40.0))
    variance = float(cv2.Laplacian(grey, cv2.CV_32F).var())
    return ink, variance


def _metric_content_type(left: np.ndarray, right: np.ndarray) -> tuple[float, bool]:
    """Compare the nature of the two pages.

    A mostly white page carrying text, facing a full page photograph, is very
    unlikely to be one panorama. Returns the score and whether the mismatch is
    strong enough to veto the pair.
    """
    left_ink, left_variance = _ink_ratio(left)
    right_ink, right_variance = _ink_ratio(right)
    score = float(np.clip(1.0 - abs(left_ink - right_ink), 0.0, 1.0))

    def is_photo(ink: float, variance: float) -> bool:
        return ink > 0.35 and variance > 50.0

    def is_text(ink: float) -> bool:
        return ink < 0.20

    veto = (is_photo(left_ink, left_variance) and is_text(right_ink)) or (
        is_text(left_ink) and is_photo(right_ink, right_variance)
    )
    return score, veto


def _metric_geometry(
    left: _Prepared, right: _Prepared, config: SeamConfig
) -> tuple[float, bool]:
    """Compare the physical dimensions of the two pages."""
    height_gap = abs(left.original_height - right.original_height) / max(
        1, max(left.original_height, right.original_height)
    )
    width_gap = abs(left.original_width - right.original_width) / max(
        1, max(left.original_width, right.original_width)
    )
    gap = 0.5 * (height_gap + width_gap)
    score = float(np.clip(1.0 - gap / max(_EPSILON, 4.0 * config.geometry_tolerance), 0.0, 1.0))
    veto = gap > config.geometry_tolerance * 2.0
    return score, veto


def _weighted_value(metrics: dict[str, float], config: SeamConfig) -> float:
    """Combine the available metrics with the configured weights.

    Metrics that could not be computed are simply absent and their weight is
    redistributed over the others.
    """
    total = 0.0
    weight_sum = 0.0
    for key, value in metrics.items():
        weight = config.weights.get(key, 0.0)
        if weight <= 0.0:
            continue
        total += weight * value
        weight_sum += weight
    if weight_sum <= 0.0:
        return 0.0
    return float(np.clip(total / weight_sum, 0.0, 1.0))


def score_seam(left: np.ndarray, right: np.ndarray, config: SeamConfig) -> SeamScore:
    """Score the continuity between the inner edges of two consecutive pages.

    ``left`` and ``right`` are full page BGR arrays, in reading order. The returned
    ``vertical_offset`` is expressed in the source pixels of the right page.
    """
    left_page = prepare(left, config)
    right_page = prepare(right, config)
    left_edge = _inner_edge(left_page, config, "left")
    right_edge = _inner_edge(right_page, config, "right")

    left_profile = _profile(left_edge.band_grey)
    right_profile = _profile(right_edge.band_grey)
    offset, correlation = _search_offset(left_profile, right_profile, config)

    metrics: dict[str, float] = {"row_correlation": float(np.clip(correlation, 0.0, 1.0))}

    # Neither edge carrying any structure means the image simply does not reach the
    # gutter: whatever the rest says, these two pages are not one picture.
    activity = max(_activity(left_profile, config), _activity(right_profile, config))
    flat = activity < config.flat_band_threshold

    band_grey = _overlap(left_edge.band_grey, right_edge.band_grey, offset)
    band_colour = _overlap(left_edge.band_colour, right_edge.band_colour, offset)
    context = _overlap(left_edge.context_grey, right_edge.context_grey, offset)
    lag = left_edge.gap + right_edge.gap

    if not flat:
        histogram = _metric_colour_histogram(*band_colour)
        if histogram is not None:
            metrics["colour_histogram"] = histogram
        gradient = _metric_gradient_jump(*context, lag=lag)
        if gradient is not None:
            metrics["gradient_jump"] = gradient
        edges = _metric_edge_continuity(
            *context, tolerance=max(3, int(round(lag * config.edge_tolerance_ratio)))
        )
        if edges is not None:
            metrics["edge_continuity"] = edges
    del band_grey

    content, content_veto = _metric_content_type(left_page.grey, right_page.grey)
    metrics["content_type"] = content
    geometry, geometry_veto = _metric_geometry(left_page, right_page, config)
    metrics["geometry"] = geometry
    metrics["band_activity"] = activity

    value = _weighted_value(
        {key: item for key, item in metrics.items() if key != "band_activity"}, config
    )
    if content_veto or geometry_veto or flat:
        # A disqualifying metric caps the score whatever the others say.
        value = min(value, config.veto_cap)
        metrics["veto"] = 1.0

    source_offset = int(round(offset / right_page.scale)) if right_page.scale else offset
    return SeamScore(
        value=value,
        vertical_offset=source_offset,
        metrics={key: float(round(item, 4)) for key, item in metrics.items()},
        confidence=classify(value, config),
    )
