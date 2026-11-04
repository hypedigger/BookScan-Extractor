"""Is this pair of pages one double page spread? A model fitted on real scans.

The first scorer of this project was tuned on synthetic pages and fell apart on
real books: it merged facing pages that merely shared a dark tone, and asked about
pairs that could never be one picture, such as a framed print facing another. This
module replaces its decision with two stages, both measured on hand labelled books.

1. **Vetoes.** Some pairs are impossible and are discarded before anything else:
   a blank page, a page whose picture stops on a light margin with a straight edge
   along the gutter (a framed print or a text block), or a strip of light paper
   along the gutter. On the labelled books these rules remove about seven pairs
   out of ten and never a real spread.
2. **Evidence.** What survives is scored by a small logistic model over gutter
   features: how the rows, the texture, the tone and the colour carry on across the
   gutter. The output is a log-odds, an *evidence*: positive means "spread", and
   every two units multiply the odds by about seven.

The book level reasoning, chiefly the fact that spreads keep the same parity
through a book, lives in ``pairing.py``.

Everything here works on page renders of any size: pages are brought to a fixed
height first, which is also the height the model was fitted at.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

HEIGHT = 1000  # analysis height the model was fitted at
INNER = 0.30  # part of each page, from the gutter, that is examined
SKIP = 0.006  # the very edge of a scan is never trusted
STRIP = 0.03  # width of the gutter strip used for row profiles
FLAT_STD = 9.0  # a column flatter than this carries no content

IMPOSSIBLE = -50.0  # evidence given to a vetoed pair

# (feature, cap, mean, scale, weight): logistic model fitted on the labelled books,
# features standardised as (min(x, cap) - mean) / scale.
_MODEL: tuple[tuple[str, float | None, float, float, float], ...] = (
    ("colour_histogram", None, 0.519488, 0.310108, 0.669604),
    ("grad", None, 0.130099, 0.182932, 1.13559),
    ("rowdiff_w", 4.0, 1.27837, 1.23834, -1.00684),
    ("absdiff_n", 80.0, 39.3384, 30.5532, -0.345107),
    ("tone", 60.0, 31.4326, 25.0332, -0.618129),
    ("chroma", 30.0, 4.27028, 6.10003, 0.250894),
    ("gradient_jump", None, 0.507902, 0.386371, 0.0351634),
    ("rawcorr_n", None, 0.300289, 0.406569, 0.178908),
    ("prof", None, 0.156424, 0.154076, 0.351546),
    ("texratio_w", None, 0.627183, 0.240897, 1.07253),
)
_BIAS = -1.9410255924714628


@dataclass(frozen=True)
class _Page:
    grey: np.ndarray  # float32, HEIGHT rows
    lab: np.ndarray  # float32 CIE Lab, same geometry


def _prepare(image: np.ndarray) -> _Page:
    height, width = image.shape[:2]
    scale = HEIGHT / max(1, height)
    size = (max(8, int(round(width * scale))), HEIGHT)
    small = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    grey = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
    lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32)
    return _Page(grey=grey, lab=lab)


def _inner(array: np.ndarray, side: str, fraction: float = INNER) -> np.ndarray:
    """Inner part of a page, oriented so that column 0 lies against the gutter."""
    width = array.shape[1]
    keep = max(4, int(width * fraction))
    if side == "left":  # the inner edge of a left hand page is its right side
        return array[:, width - keep :][:, ::-1]
    return array[:, :keep]


def _margin(band: np.ndarray, page_width: int) -> float:
    """Width of the content free strip along the gutter, as a fraction of the page.

    A column without content is flat from top to bottom: paper, a frame, a black
    band. The margin ends at the first run of columns that are not flat.
    """
    skip = max(1, int(page_width * SKIP))
    deviations = band.std(axis=0)
    run = 0
    for column in range(skip, band.shape[1]):
        if deviations[column] >= FLAT_STD:
            run += 1
            if run >= 3:
                return (column - 2 - skip) / page_width
        else:
            run = 0
    return band.shape[1] / page_width


def _frame(band: np.ndarray, page_width: int) -> tuple[float, float]:
    """Describe how the content meets the gutter: ``(straightness, brightness)``.

    For every row the first content pixel from the gutter is found. A framed print
    or a block of text starts at the same column on most rows, a straight edge; a
    figure on a backdrop does not. The brightness of what lies between the gutter
    and the content tells paper from a black backdrop.
    """
    skip = max(1, int(page_width * SKIP))
    body = band[:, skip:]
    reference = np.median(body[:, :3], axis=1, keepdims=True)
    departs = np.abs(body - reference) > 25
    content = cv2.blur(departs.astype(np.float32), (5, 1)) > 0.8
    reached = content.any(axis=1)
    first = np.where(reached, content.argmax(axis=1), body.shape[1])
    rows = first[reached]
    if rows.size < 20:
        return 0.0, float(body[:, :3].mean())
    mode = np.bincount(rows).argmax()
    straight = float(np.mean(np.abs(rows - mode) <= max(2, int(page_width * 0.004))))
    width = int(np.median(rows))
    return straight, float(body[:, : max(1, width)].mean())


def _detrend(profile: np.ndarray, window: int = 25) -> np.ndarray:
    size = max(3, min(window | 1, (profile.size // 2) * 2 - 1))
    smooth = cv2.blur(profile.reshape(-1, 1), (1, size), borderType=cv2.BORDER_REFLECT)
    return profile - smooth.ravel()


def _corr(a: np.ndarray, b: np.ndarray) -> float:
    a = a - a.mean()
    b = b - b.mean()
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denominator) if denominator > 1e-6 else 0.0


def _best_corr(a: np.ndarray, b: np.ndarray, limit: int = 30) -> float:
    """Best correlation over small vertical shifts, larger shifts slightly penalised."""
    best = -1.0
    length = min(a.size, b.size)
    a = a[:length]
    b = b[:length]
    for offset in range(-limit, limit + 1, 2):
        if offset >= 0:
            x, y = a[offset:], b[: length - offset]
        else:
            x, y = a[: length + offset], b[-offset:]
        best = max(best, _corr(x, y) - 0.04 * abs(offset) / limit)
    return best


def _row_content(strip: np.ndarray, background: float) -> np.ndarray:
    """Rows of a gutter strip that carry something other than the background."""
    return (np.abs(strip.mean(axis=1) - background) > 18) | (strip.std(axis=1) > 12)


def _continuity(left: np.ndarray, right: np.ndarray) -> dict[str, float]:
    """Compare the strips on each side of the gutter without removing their trend.

    The overall difference, light against dark, is exactly what tells two different
    photographs apart, so the profiles are compared as they are.
    """
    width_l, width_r = left.shape[1], right.shape[1]
    out: dict[str, float] = {}
    for fraction, tag in ((0.015, "n"), (0.05, "w")):
        start_l, start_r = max(1, int(width_l * SKIP)), max(1, int(width_r * SKIP))
        end_l = max(2, int(width_l * SKIP)) + max(2, int(width_l * fraction))
        end_r = max(2, int(width_r * SKIP)) + max(2, int(width_r * fraction))
        strip_l = _inner(left, "left")[:, start_l:end_l]
        strip_r = _inner(right, "right")[:, start_r:end_r]
        profile_l = cv2.blur(strip_l.mean(axis=1).reshape(-1, 1), (1, 9)).ravel()
        profile_r = cv2.blur(strip_r.mean(axis=1).reshape(-1, 1), (1, 9)).ravel()
        best_c, best_d = -1.0, 1e9
        length = min(profile_l.size, profile_r.size)
        for offset in range(-30, 31, 2):
            if offset >= 0:
                x, y = profile_l[offset:length], profile_r[: length - offset]
            else:
                x, y = profile_l[: length + offset], profile_r[-offset:length]
            best_c = max(best_c, _corr(x, y))
            best_d = min(best_d, float(np.median(np.abs(x - y))))
        spread = max(float(profile_l.std()), float(profile_r.std()), 10.0)
        texture_l = float(np.abs(np.diff(strip_l, axis=0)).mean())
        texture_r = float(np.abs(np.diff(strip_r, axis=0)).mean())
        out[f"rawcorr_{tag}"] = best_c
        out[f"rowdiff_{tag}"] = best_d / spread
        out[f"absdiff_{tag}"] = best_d
        out[f"texratio_{tag}"] = min(texture_l, texture_r) / max(texture_l, texture_r, 1e-3)
    return out


def pair_features(left: np.ndarray, right: np.ndarray) -> dict[str, float]:
    """Measure everything the vetoes and the model need about a pair of pages.

    ``left`` and ``right`` are BGR page renders in reading order. The two metrics
    the model borrows from the gutter scorer (``colour_histogram`` and
    ``gradient_jump``) are not computed here: ``seam.score_seam`` adds them.
    """
    page_l, page_r = _prepare(left), _prepare(right)
    grey_l, grey_r = page_l.grey, page_r.grey
    width_l, width_r = grey_l.shape[1], grey_r.shape[1]
    band_l, band_r = _inner(grey_l, "left"), _inner(grey_r, "right")

    skip_l, skip_r = max(1, int(width_l * SKIP)), max(1, int(width_r * SKIP))
    strip_w_l, strip_w_r = max(3, int(width_l * STRIP)), max(3, int(width_r * STRIP))
    strip_l = band_l[:, skip_l : skip_l + strip_w_l]
    strip_r = band_r[:, skip_r : skip_r + strip_w_r]

    background_l = float(np.median(np.concatenate([grey_l[:8].ravel(), grey_l[-8:].ravel()])))
    background_r = float(np.median(np.concatenate([grey_r[:8].ravel(), grey_r[-8:].ravel()])))
    occupied_l = _row_content(strip_l, background_l)
    occupied_r = _row_content(strip_r, background_r)

    profile_l, profile_r = strip_l.mean(axis=1), strip_r.mean(axis=1)
    rows_l = np.abs(np.diff(strip_l, axis=0)).mean(axis=1)
    rows_r = np.abs(np.diff(strip_r, axis=0)).mean(axis=1)

    lab_l = _inner(page_l.lab, "left")[:, skip_l : skip_l + strip_w_l].reshape(-1, 3)
    lab_r = _inner(page_r.lab, "right")[:, skip_r : skip_r + strip_w_r].reshape(-1, 3)
    chroma = float(np.abs(lab_l.mean(axis=0)[1:] - lab_r.mean(axis=0)[1:]).sum())

    straight_l, bright_l = _frame(band_l, width_l)
    straight_r, bright_r = _frame(band_r, width_r)

    features = {
        "margin_l": _margin(band_l, width_l),
        "margin_r": _margin(band_r, width_r),
        "occ_l": float(occupied_l.mean()),
        "occ_r": float(occupied_r.mean()),
        "prof": _best_corr(_detrend(profile_l), _detrend(profile_r)),
        "grad": _best_corr(rows_l, rows_r),
        "tone": float(abs(profile_l.mean() - profile_r.mean())),
        "chroma": chroma,
        "std_min": min(float(grey_l.std()), float(grey_r.std())),
        "straight_l": straight_l,
        "straight_r": straight_r,
        "bright_l": bright_l,
        "bright_r": bright_r,
    }
    features.update(_continuity(grey_l, grey_r))
    return features


def veto_reason(features: dict[str, float]) -> str | None:
    """Return why a pair cannot be one spread, or None when it might be one."""
    if features["std_min"] < 12:
        return "blank page"

    def light(side: str) -> bool:
        return features[f"bright_{side}"] > 110

    for side in ("l", "r"):
        if features[f"margin_{side}"] > 0.02 and light(side) and features[f"straight_{side}"] > 0.5:
            return "framed picture or text block along the gutter"
    for side in ("l", "r"):
        if features[f"occ_{side}"] < 0.02 and light(side):
            return "light paper along the gutter"
    return None


def evidence(features: dict[str, float]) -> float:
    """Return the log-odds that a pair which passed the vetoes is one spread."""
    total = _BIAS
    for name, cap, mean, scale, weight in _MODEL:
        value = features.get(name, 0.0)
        if cap is not None:
            value = min(value, cap)
        total += weight * (value - mean) / scale
    return float(total)


def probability(value: float) -> float:
    """Turn an evidence into a probability, for display and for mode 2."""
    return 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, value))))
