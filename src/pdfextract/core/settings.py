"""Configuration model: dataclasses, JSON persistence and named profiles.

Every tunable value of the engine lives here. The dataclasses are plain data with
sensible defaults, so the engine always runs without a settings file; the user
interface only ever edits an instance of :class:`AppSettings`.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Union, cast, get_args, get_origin, get_type_hints

SCHEMA_VERSION = 1

APP_DIR_NAME = "pdf-image-extractor"


@dataclass
class ExtractionConfig:
    """How a single PDF page is turned into pixels or into an output file."""

    render_dpi: int = 300
    analysis_height: int = 1100  # pixels; the scorer works at 1000, a little over is enough
    full_page_coverage: float = 0.90
    passthrough_enabled: bool = True
    passthrough_extensions: tuple[str, ...] = ("jpeg", "jpg", "png", "tiff", "tif", "bmp")
    allow_smask_passthrough: bool = False
    rotation_mode: str = "rotate_stream"  # "rotate_stream" or "render"
    jpeg_quality: int = 95
    jpeg_subsampling: int = 0  # 0 means 4:4:4, never 4:2:0 on scanned text


@dataclass
class SeamConfig:
    """Gutter continuity scoring parameters (see ``core/seam.py``)."""

    analysis_height: int = 1000
    band_px: int = 24
    gutter_skip_ratio: float = 0.015
    max_offset_ratio: float = 0.03
    offset_step: int = 2
    scale_search: bool = False
    scale_tolerance: float = 0.02
    trim_scan_border: bool = True
    border_trim_max_ratio: float = 0.05
    # Thresholds on the evidence (log-odds) of spread.py, after the parity prior.
    merge_evidence: float = 4.0  # merged without asking at or above
    split_evidence: float = -1.0  # kept apart without asking at or below
    geometry_tolerance: float = 0.06
    detrend_window: int = 25
    offset_penalty: float = 0.04
    flat_band_threshold: float = 1.5
    edge_tolerance_ratio: float = 0.6


@dataclass
class PairingConfig:
    """Global pairing decision parameters (modes 2 and 3)."""

    # Spreads keep the parity of a book: once enough likely spreads agree on one,
    # pairs on that parity gain evidence and pairs on the other lose a lot of it.
    parity_evidence: float = 2.0  # a pair this likely counts as a parity witness
    parity_min_pairs: int = 3  # witnesses needed before the parity is trusted
    parity_bonus: float = 2.0
    parity_penalty: float = 5.0
    mode2_report_confidence: bool = True


@dataclass
class MergeConfig:
    """How two half-pages are glued together."""

    fill_mode: str = "white"  # "white" or "sampled"
    gutter_trim_px: int = 0
    apply_vertical_offset: bool = True


@dataclass
class PostProcessConfig:
    """Optional post-processing. Everything is disabled by default.

    The application order is imposed by the engine: deskew, crop, gutter shadow,
    contrast, resize. A different order degrades the result.
    """

    name: str = "default"
    deskew_enabled: bool = False
    deskew_max_angle: float = 5.0
    crop_enabled: bool = False
    crop_threshold: int = 0  # 0 means Otsu
    crop_margin_px: int = 8
    gutter_shadow_enabled: bool = False
    gutter_shadow_width_ratio: float = 0.06
    contrast_enabled: bool = False
    clahe_clip_limit: float = 2.0
    clahe_grid_size: int = 8
    resize_enabled: bool = False
    resize_max_dimension: int = 4000

    def any_enabled(self) -> bool:
        """Return True when at least one post-processing step is active."""
        return any(
            (
                self.deskew_enabled,
                self.crop_enabled,
                self.gutter_shadow_enabled,
                self.contrast_enabled,
                self.resize_enabled,
            )
        )


@dataclass
class OutputConfig:
    """Where and how output files are written."""

    root: str = ""
    template: str = "{pdf}_{pages}"
    subfolder_per_pdf: bool = True
    mirror_source_tree: bool = True
    overwrite: bool = False
    open_when_done: bool = True  # show the images as soon as the batch is finished


@dataclass
class ReviewConfig:
    """Manual validation screen and its pre-loader."""

    preload_ahead: int = 5
    preload_behind: int = 2
    cache_mb: int = 512
    throttle_background: bool = True
    banner_ms: int = 1000


@dataclass
class TrashConfig:
    """Moving processed sources to the recycle bin. Never a permanent delete."""

    enabled: bool = False
    trash_sidecar: bool = True


@dataclass
class AppSettings:
    """Root settings object, persisted as a single JSON file."""

    schema_version: int = SCHEMA_VERSION
    mode: int = 3  # smart pairing: the mode the application exists for
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    seam: SeamConfig = field(default_factory=SeamConfig)
    pairing: PairingConfig = field(default_factory=PairingConfig)
    merge: MergeConfig = field(default_factory=MergeConfig)
    postprocess: PostProcessConfig = field(default_factory=PostProcessConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    trash: TrashConfig = field(default_factory=TrashConfig)
    profiles: dict[str, PostProcessConfig] = field(default_factory=dict)
    active_profile: str = ""
    theme: str = "dark"  # "dark" or "light"
    last_directory: str = ""  # where the file choosers open next time
    debug_metrics: bool = False
    max_workers: int = 0  # 0 means cpu_count() - 1

    def resolved_postprocess(self) -> PostProcessConfig:
        """Return the active profile, falling back to the inline configuration."""
        if self.active_profile and self.active_profile in self.profiles:
            return self.profiles[self.active_profile]
        return self.postprocess


def app_data_dir() -> Path:
    """Return the per-user application directory, creating it if needed."""
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = Path(base) / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_settings_path() -> Path:
    """Return the default location of the settings file."""
    return app_data_dir() / "settings.json"


def _coerce(annotation: Any, value: Any) -> Any:
    """Convert a JSON value to the type expected by a dataclass field."""
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if value is None or not args:
            return value
        return _coerce(args[0], value)
    if is_dataclass(annotation) and isinstance(value, dict):
        return from_dict(cast(type[Any], annotation), value)
    if origin is tuple:
        return tuple(value)
    if origin is list:
        return list(value)
    if origin is dict and isinstance(value, dict):
        parameters = get_args(annotation)
        if len(parameters) == 2 and is_dataclass(parameters[1]):
            nested = cast(type[Any], parameters[1])
            return {key: from_dict(nested, item) for key, item in value.items()}
        return dict(value)
    if annotation is float and isinstance(value, int) and not isinstance(value, bool):
        return float(value)
    return value


def from_dict(cls: type[Any], data: dict[str, Any]) -> Any:
    """Rebuild a (possibly nested) dataclass from a plain dictionary.

    Unknown keys are ignored and missing keys keep their default, so a settings
    file written by an older version of the application still loads.
    """
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    kwargs = {
        name: _coerce(hints.get(name, object), value)
        for name, value in data.items()
        if name in known
    }
    return cls(**kwargs)


def load_settings(path: Path | None = None) -> AppSettings:
    """Load settings from disk, returning defaults when the file is absent or invalid."""
    target = path or default_settings_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return AppSettings()
    if not isinstance(raw, dict):
        return AppSettings()
    settings: AppSettings = from_dict(AppSettings, raw)
    return settings


def save_settings(settings: AppSettings, path: Path | None = None) -> Path:
    """Write settings to disk atomically and return the path written."""
    target = path or default_settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(settings), indent=2, ensure_ascii=False)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, target)
    return target
