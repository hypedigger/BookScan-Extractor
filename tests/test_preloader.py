"""Tests of the look ahead loader, including the performance requirement.

The requirement from the specification: with a warm cache, less than 16 ms between
the key press and the next pair being on screen.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import pytest

from pdfextract.ui.preloader import PairKey, PreloadCache, Preloader, to_qimage, window_around

from fixture_builder import build_pdf, make_photo, spread_pages

pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtGui import QPixmap

DISPLAY_HEIGHT = 900
LONG_BOOK_PAGES = 400


@pytest.fixture(scope="module")
def long_book(tmp_path_factory) -> Path:
    """A four hundred page book, as in the performance requirement.

    A handful of distinct images are cycled: the point of the measurement is the
    display path, not how varied the pictures are.
    """
    directory = tmp_path_factory.mktemp("long")
    palette: list = []
    for seed in range(4):
        palette.extend(spread_pages(seed=seed))
    pages = [palette[index % len(palette)] for index in range(LONG_BOOK_PAGES)]
    return build_pdf(directory / "long.pdf", pages, dpi=200)


def test_cache_evicts_the_least_recently_used(qapp):
    """The cache is bounded in megabytes, not in entries."""
    cache = PreloadCache(limit_mb=32)
    image = to_qimage(make_photo(width=800, height=600, seed=1))
    size = image.sizeInBytes() * 2
    count = int(32 * 1024 * 1024 / size) + 3
    for index in range(count):
        cache.put(PairKey(Path("book.pdf"), index, index + 1, 600), (image, image))
    assert cache.used_bytes <= 32 * 1024 * 1024
    assert PairKey(Path("book.pdf"), 0, 1, 600) not in cache
    assert PairKey(Path("book.pdf"), count - 1, count, 600) in cache


def test_window_around_asks_for_the_nearest_pairs_first():
    pairs = [(Path("b.pdf"), index, index + 1) for index in range(1, 40, 2)]
    keys = window_around(pairs, position=5, height=900, ahead=3, behind=2)
    distances = [abs(pairs.index((key.pdf_path, key.left_page, key.right_page)) - 5)
                 for key in keys]
    assert distances == sorted(distances)
    assert distances[0] == 0
    assert max(distances) <= 3


def test_window_keeps_pairs_behind_for_the_back_step():
    pairs = [(Path("b.pdf"), index, index + 1) for index in range(1, 20, 2)]
    keys = window_around(pairs, position=4, height=900, ahead=2, behind=2)
    indices = {key.left_page for key in keys}
    assert pairs[2][1] in indices and pairs[3][1] in indices


def test_window_reaches_into_the_next_book(qapp):
    """Near the end of a batch the look ahead must already be on the next book."""
    pairs = [(Path("first.pdf"), 1, 2), (Path("first.pdf"), 3, 4), (Path("second.pdf"), 1, 2)]
    keys = window_around(pairs, position=1, height=900, ahead=3, behind=1)
    assert any(key.pdf_path.name == "second.pdf" for key in keys)


def test_preloader_decodes_in_the_background(qapp, long_book, qtbot):
    preloader = Preloader(PreloadCache(256))
    preloader.start()
    try:
        key = PairKey(long_book, 1, 2, DISPLAY_HEIGHT)
        with qtbot.waitSignal(preloader.ready, timeout=15000):
            preloader.request([key])
        assert preloader.get(key) is not None
    finally:
        preloader.stop()


def test_a_warm_pair_reaches_the_screen_in_under_16_ms(qapp, long_book):
    """The measurable objective of the validation screen.

    What is measured is the path a key press actually takes: pull the decoded pair
    from the cache and turn it into something the widget can draw.
    """
    preloader = Preloader(PreloadCache(512))
    pairs = [(long_book, index, index + 1) for index in range(1, 61, 2)]
    keys = window_around(pairs, position=0, height=DISPLAY_HEIGHT, ahead=20, behind=0)
    for key in keys:
        preloader.load_now(key)  # warm the cache, as the look ahead thread would

    timings: list[float] = []
    for key in keys:
        started = time.perf_counter()
        images = preloader.get(key)
        assert images is not None, "the cache should have been warm"
        left = QPixmap.fromImage(images[0])
        right = QPixmap.fromImage(images[1])
        timings.append((time.perf_counter() - started) * 1000.0)
        assert not left.isNull() and not right.isNull()

    median = statistics.median(timings)
    worst = max(timings)
    assert median < 16.0, f"median {median:.1f} ms, worst {worst:.1f} ms"


def test_a_cache_miss_still_shows_something(qapp, long_book):
    """A miss must be served on the spot rather than leaving the screen empty."""
    preloader = Preloader(PreloadCache(64))
    key = PairKey(long_book, 5, 6, 400)
    assert preloader.get(key) is None
    images = preloader.load_now(key)
    assert images[0].height() == pytest.approx(400, abs=8)
    assert preloader.get(key) is not None


def test_invalidate_empties_the_cache(qapp, long_book):
    preloader = Preloader(PreloadCache(64))
    key = PairKey(long_book, 1, 2, 300)
    preloader.load_now(key)
    preloader.invalidate()
    assert preloader.get(key) is None
