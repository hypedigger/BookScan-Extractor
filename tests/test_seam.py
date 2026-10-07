"""Tests of the gutter continuity scoring.

The most important suite of the project: if the scorer is not reliable, mode 3 has
no value.
"""

from __future__ import annotations

import pytest

from pdfextract.core.seam import MERGE, SPLIT, score_seam
from pdfextract.core.settings import SeamConfig

from fixture_builder import make_text_page, spread_pages, unrelated_pages

CONFIG = SeamConfig()

CONTINUOUS_CASES = [
    pytest.param({}, id="clean"),
    pytest.param({"noise": 6.0}, id="noisy"),
    pytest.param({"dy": 12}, id="misaligned"),
    pytest.param({"shadow": 0.5}, id="gutter-shadow"),
    pytest.param({"border": 12}, id="scan-border"),
    pytest.param({"noise": 8.0, "dy": 10, "shadow": 0.45, "border": 10}, id="all-defects"),
]


@pytest.mark.parametrize("defects", CONTINUOUS_CASES)
@pytest.mark.parametrize("seed", [2, 3, 6])
def test_continuous_pairs_score_above_merge_threshold(defects, seed):
    """Two halves of one image must score high enough to be merged unattended."""
    left, right = spread_pages(seed=seed, **defects)
    score = score_seam(left, right, CONFIG)
    assert score.value > CONFIG.threshold_merge, score.metrics
    assert score.confidence == MERGE


@pytest.mark.parametrize("seed", [12, 13, 14])
def test_unrelated_pairs_score_below_split_threshold(seed):
    """A text page facing a photograph must be split unattended."""
    left, right = unrelated_pages(seed=seed)
    score = score_seam(left, right, CONFIG)
    assert score.value < CONFIG.threshold_split, score.metrics
    assert score.confidence == SPLIT


def test_two_text_pages_are_split():
    """Two facing text pages have blank margins at the gutter, which look alike.

    Without the flat band guard, the colour and gradient metrics both report a
    perfect match and the pair scores high for the worst possible reason.
    """
    left = make_text_page(seed=4)
    right = make_text_page(seed=5)
    score = score_seam(left, right, CONFIG)
    assert score.confidence == SPLIT, score.metrics
    assert score.metrics.get("veto") == 1.0


def test_unrelated_halves_of_different_photographs_are_not_merged():
    """Two photo halves that do not belong together must not reach the merge threshold."""
    left = spread_pages(seed=20)[0]
    right = spread_pages(seed=21)[1]
    score = score_seam(left, right, CONFIG)
    assert score.confidence != MERGE, score.metrics


@pytest.mark.parametrize("shift", [-14, -6, 0, 8, 16])
def test_relative_vertical_offset_is_recovered(shift):
    """The offset must track a known misalignment of the right half.

    The contract is tested relatively: the absolute best alignment of two bands
    separated by the skipped gutter strip also depends on how the content itself
    drifts over that distance, which no scorer can separate from the real
    misregistration of the two scans.
    """
    reference = score_seam(*spread_pages(seed=3, dy=0), CONFIG).vertical_offset
    moved = score_seam(*spread_pages(seed=3, dy=shift), CONFIG).vertical_offset
    assert moved - reference == pytest.approx(-shift, abs=3)


def test_vertical_offset_is_expressed_in_source_pixels():
    """A page taller than the analysis height must report an offset in its own scale."""
    left, right = spread_pages(seed=3, dy=20)
    config = SeamConfig(analysis_height=500)
    score = score_seam(left, right, config)
    # The source pages are 1200 px tall, the analysis runs at 500 px: an offset
    # reported in analysis pixels would be roughly half of what is expected.
    assert score.vertical_offset == pytest.approx(-20, abs=5)


def test_geometry_mismatch_vetoes_the_pair():
    """Pages of very different dimensions cannot be two halves of one image."""
    left, right = spread_pages(seed=3)
    narrow = right[:, : right.shape[1] // 2]
    score = score_seam(left, narrow, CONFIG)
    assert score.value <= CONFIG.veto_cap
    assert score.metrics.get("veto") == 1.0


def test_metrics_are_reported_for_debugging():
    """Every computed metric is exposed, so the weights can be tuned on real files."""
    left, right = spread_pages(seed=2)
    score = score_seam(left, right, CONFIG)
    assert "row_correlation" in score.metrics
    assert "gradient_jump" in score.metrics
    assert 0.0 <= score.value <= 1.0
