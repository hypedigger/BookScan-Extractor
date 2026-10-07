"""Native stream extraction and fallback rendering.

The nominal case of a scanned book is one full page bitmap per PDF page. When that
holds and nothing has to be transformed, the original compressed stream is written
out untouched: no re-encoding, no loss, and extraction is nearly instantaneous.
Everything else falls back to rendering the page.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np
import pymupdf
from PIL import Image

from pdfextract.core.settings import ExtractionConfig

# A normally placed, unmirrored image has a positive placement determinant in
# PyMuPDF (verified against a freshly inserted image): the matrix looks like
# (width, 0, 0, height, x, y). A negative scale on an axis therefore means a flip.
_SKEW_TOLERANCE = 1e-3

LOSSLESS_EXTENSIONS = frozenset({"png", "tiff", "tif", "bmp"})


@dataclass(frozen=True)
class OriginalStream:
    """Raw compressed bytes of an embedded image, exactly as stored in the PDF."""

    data: bytes
    ext: str
    width: int
    height: int
    colourspace: str
    xref: int


@dataclass(frozen=True)
class Placement:
    """How an image is placed on the page."""

    coverage: float  # fraction of the page area covered by the image
    mirrored_x: bool
    mirrored_y: bool
    skewed: bool  # rotated or sheared placement, not axis aligned


@dataclass(frozen=True)
class PageOutput:
    """Bytes ready to be written for a single source page."""

    data: bytes
    ext: str
    passthrough: bool
    reason: str
    width: int
    height: int


def _page_area(page: pymupdf.Page) -> float:
    """Return the page area in square points (rotation invariant)."""
    rect = page.rect
    return float(rect.width) * float(rect.height)


def _placement(page: pymupdf.Page, xref: int) -> Placement | None:
    """Return the placement of image ``xref`` on ``page``, or None when unknown."""
    try:
        infos: list[dict[str, Any]] = page.get_image_info(xrefs=True)
    except Exception:
        return None
    area = _page_area(page)
    for info in infos:
        if int(info.get("xref", 0)) != xref:
            continue
        bbox = pymupdf.Rect(info["bbox"])
        transform = info.get("transform") or (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        a, b, c, d = (float(transform[0]), float(transform[1]),
                      float(transform[2]), float(transform[3]))
        scale = max(abs(a), abs(b), abs(c), abs(d), 1.0)
        skewed = abs(b) / scale > _SKEW_TOLERANCE or abs(c) / scale > _SKEW_TOLERANCE
        coverage = (bbox.width * bbox.height / area) if area else 0.0
        return Placement(
            coverage=float(coverage),
            mirrored_x=a < 0,
            mirrored_y=d < 0,
            skewed=skewed,
        )
    return None


def probe_original_stream(
    page: pymupdf.Page, config: ExtractionConfig
) -> tuple[OriginalStream | None, Placement | None, str]:
    """Look for a single full page image whose stream can be reused as is.

    Returns ``(stream, placement, reason)``. ``stream`` is None when the page does
    not qualify, and ``reason`` then explains why, for logging and debugging.
    """
    images = page.get_images(full=True)
    if len(images) != 1:
        return None, None, f"image_count={len(images)}"
    xref, smask = int(images[0][0]), int(images[0][1])
    if smask and not config.allow_smask_passthrough:
        return None, None, "transparency_mask"
    placement = _placement(page, xref)
    if placement is None:
        return None, None, "placement_unknown"
    if placement.skewed:
        return None, placement, "skewed_placement"
    if placement.coverage < config.full_page_coverage:
        return None, placement, f"coverage={placement.coverage:.2f}"
    try:
        raw = page.parent.extract_image(xref)
    except Exception as exc:
        return None, placement, f"extract_failed={exc}"
    if not raw or not raw.get("image"):
        return None, placement, "empty_stream"
    stream = OriginalStream(
        data=bytes(raw["image"]),
        ext=str(raw.get("ext", "")).lower(),
        width=int(raw.get("width", 0)),
        height=int(raw.get("height", 0)),
        colourspace=str(raw.get("cs-name", "")),
        xref=xref,
    )
    return stream, placement, "native"


def decode_image_bytes(data: bytes) -> np.ndarray | None:
    """Decode compressed image bytes to a BGR array, or None when unsupported."""
    buffer = np.frombuffer(data, dtype=np.uint8)
    decoded = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
    if decoded is not None:
        return decoded
    try:
        with Image.open(io.BytesIO(data)) as image:
            rgb = image.convert("RGB")
            return cv2.cvtColor(np.asarray(rgb), cv2.COLOR_RGB2BGR)
    except Exception:
        return None


def render_page(page: pymupdf.Page, dpi: int) -> np.ndarray:
    """Render a page to a BGR array at the requested resolution.

    Rendering always reflects the page as displayed, so ``/Rotate`` and any
    placement matrix are already applied.
    """
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    samples = np.frombuffer(pixmap.samples, dtype=np.uint8)
    array = samples.reshape(pixmap.height, pixmap.width, pixmap.n)
    if pixmap.n == 1:
        return cv2.cvtColor(array, cv2.COLOR_GRAY2BGR)
    if pixmap.n == 4:
        return cv2.cvtColor(array, cv2.COLOR_RGBA2BGR)
    return cv2.cvtColor(array, cv2.COLOR_RGB2BGR)


def _apply_orientation(
    array: np.ndarray, rotation: int, placement: Placement | None
) -> np.ndarray:
    """Apply the page rotation and any mirroring to a natively decoded image."""
    result = array
    if placement is not None:
        if placement.mirrored_x:
            result = cv2.flip(result, 1)
        if placement.mirrored_y:
            result = cv2.flip(result, 0)
    rotation = rotation % 360
    if rotation == 90:
        result = cv2.rotate(result, cv2.ROTATE_90_CLOCKWISE)
    elif rotation == 180:
        result = cv2.rotate(result, cv2.ROTATE_180)
    elif rotation == 270:
        result = cv2.rotate(result, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return result


def load_page_array(
    page: pymupdf.Page, config: ExtractionConfig, dpi: int | None = None
) -> np.ndarray:
    """Return the page as a BGR array, at its native resolution when possible.

    Decoding the embedded stream keeps the full scan resolution and avoids a
    resampling step, which matters for merging; rendering is only a fallback.
    """
    stream, placement, _ = probe_original_stream(page, config)
    if stream is not None:
        decoded = decode_image_bytes(stream.data)
        if decoded is not None:
            return _apply_orientation(decoded, int(page.rotation), placement)
    return render_page(page, dpi or config.render_dpi)


def render_for_height(page: pymupdf.Page, height: int) -> np.ndarray:
    """Render a page straight to roughly the requested pixel height.

    Used by the validation screen: rendering small is much cheaper than decoding a
    full resolution scan and then shrinking it, and the screen only ever shows a
    reduced image anyway.
    """
    page_height = float(page.rect.height) or 1.0
    dpi = max(12, int(round(72.0 * height / page_height)))
    return render_page(page, dpi)


def analysis_array(page: pymupdf.Page, config: ExtractionConfig) -> np.ndarray:
    """Return a reduced rendering of a page, for scoring only.

    The scorer normalises to its own analysis height anyway, so rendering small is
    far cheaper than decoding a full resolution scan.
    """
    return render_page(page, config.analysis_dpi)


def output_size(page: pymupdf.Page, config: ExtractionConfig) -> tuple[int, int]:
    """Return the ``(width, height)`` that :func:`load_page_array` would produce.

    Scores are computed on the reduced rendering but the merger works at full
    resolution, so the vertical offset has to be converted between the two. That
    conversion needs this size, and getting it wrong misaligns every merge.
    """
    stream, _placement, _ = probe_original_stream(page, config)
    if stream is not None:
        width, height = stream.width, stream.height
        if int(page.rotation) % 180 == 90:
            width, height = height, width
        return width, height
    scale = config.render_dpi / 72.0
    return int(round(page.rect.width * scale)), int(round(page.rect.height * scale))


def output_height(page: pymupdf.Page, config: ExtractionConfig) -> int:
    """Return the height in pixels of what :func:`load_page_array` would produce."""
    return output_size(page, config)[1]


def encode_array(
    array: np.ndarray, config: ExtractionConfig, lossless: bool = False
) -> tuple[bytes, str]:
    """Encode a BGR array for output.

    JPEG quality 95 with 4:4:4 chroma, because chroma subsampling visibly damages
    scanned text. ``lossless`` produces a PNG instead, used when the source stream
    was itself lossless and only had to be reoriented.
    """
    rgb = cv2.cvtColor(array, cv2.COLOR_BGR2RGB)
    image = Image.fromarray(rgb)
    buffer = io.BytesIO()
    if lossless:
        image.save(buffer, format="PNG", compress_level=6)
        return buffer.getvalue(), "png"
    image.save(
        buffer,
        format="JPEG",
        quality=config.jpeg_quality,
        subsampling=config.jpeg_subsampling,
        optimize=True,
    )
    return buffer.getvalue(), "jpg"


def encoded_output(array: np.ndarray, config: ExtractionConfig, reason: str) -> PageOutput:
    """Wrap an array into a :class:`PageOutput` encoded as JPEG."""
    data, ext = encode_array(array, config)
    height, width = array.shape[:2]
    return PageOutput(
        data=data,
        ext=ext,
        passthrough=False,
        reason=reason,
        width=int(width),
        height=int(height),
    )


def page_output(
    page: pymupdf.Page, config: ExtractionConfig, force_render: bool = False
) -> PageOutput:
    """Produce the bytes to write for a single page, in mode 1 or for a lone page.

    ``force_render`` is used when the caller knows the page will be transformed
    anyway (post-processing enabled), in which case reusing the stream is pointless.
    """
    if force_render or not config.passthrough_enabled:
        return encoded_output(render_page(page, config.render_dpi), config, "forced_render")

    stream, placement, reason = probe_original_stream(page, config)
    if stream is None:
        return encoded_output(render_page(page, config.render_dpi), config, reason)

    if stream.ext not in config.passthrough_extensions:
        return encoded_output(render_page(page, config.render_dpi), config, f"ext={stream.ext}")

    rotation = int(page.rotation) % 360
    mirrored = placement is not None and (placement.mirrored_x or placement.mirrored_y)
    if rotation == 0 and not mirrored:
        return PageOutput(
            data=stream.data,
            ext=_normalise_ext(stream.ext),
            passthrough=True,
            reason="native",
            width=stream.width,
            height=stream.height,
        )

    # The stream is not oriented the way the page displays. Re-encoding from the
    # decoded stream preserves the scan resolution, which a fixed dpi render would
    # not; rendering stays available as an explicit option.
    if config.rotation_mode == "render":
        return encoded_output(
            render_page(page, config.render_dpi), config, f"rotate={rotation}:render"
        )
    decoded = decode_image_bytes(stream.data)
    if decoded is None:
        return encoded_output(
            render_page(page, config.render_dpi), config, f"rotate={rotation}:decode_failed"
        )
    oriented = _apply_orientation(decoded, rotation, placement)
    lossless = stream.ext in LOSSLESS_EXTENSIONS
    data, ext = encode_array(oriented, config, lossless=lossless)
    height, width = oriented.shape[:2]
    return PageOutput(
        data=data,
        ext=ext,
        passthrough=False,
        reason=f"rotate={rotation}:reencoded",
        width=int(width),
        height=int(height),
    )


def _normalise_ext(ext: str) -> str:
    """Return the file extension used on disk for a given stream extension."""
    if ext == "jpeg":
        return "jpg"
    if ext == "tiff":
        return "tif"
    return ext
