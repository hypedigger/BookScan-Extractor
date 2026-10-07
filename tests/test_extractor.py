"""Tests of stream extraction, fallback rendering and merging."""

from __future__ import annotations

import io

import numpy as np
import pymupdf
import pytest
from PIL import Image

from pdfextract.core.document import PdfDocument, PdfDocumentError, PdfPasswordRequiredError
from pdfextract.core.extractor import (
    load_page_array,
    output_size,
    page_output,
    probe_original_stream,
    render_page,
)
from pdfextract.core.merger import merge_pages
from pdfextract.core.settings import ExtractionConfig, MergeConfig

from fixture_builder import build_pdf, encode_jpeg, make_photo, make_text_page, spread_pages

CONFIG = ExtractionConfig()


def test_full_page_scan_keeps_its_original_stream(tmp_path):
    """The nominal case: the bytes are written untouched, with no re-encoding."""
    image = make_photo(seed=1)
    path = build_pdf(tmp_path / "scan.pdf", [image])
    with PdfDocument(path) as document:
        output = page_output(document.load_page(0), CONFIG)
    assert output.passthrough
    assert output.reason == "native"
    assert output.ext == "jpg"
    assert Image.open(io.BytesIO(output.data)).size == (image.shape[1], image.shape[0])


def test_rotated_page_is_reoriented_at_the_source_resolution(tmp_path):
    """A rotated page cannot keep its stream, but must not lose resolution either."""
    image = make_photo(seed=1)
    path = build_pdf(tmp_path / "rotated.pdf", [image], rotation=90)
    with PdfDocument(path) as document:
        output = page_output(document.load_page(0), CONFIG)
    assert not output.passthrough
    assert "rotate=90" in output.reason
    # Rendering at 300 dpi would give a completely different size; the source
    # resolution is preserved, only swapped.
    assert (output.width, output.height) == (image.shape[0], image.shape[1])


def test_rotation_mode_render_falls_back_to_rendering(tmp_path):
    config = ExtractionConfig(rotation_mode="render", render_dpi=150)
    path = build_pdf(tmp_path / "rotated.pdf", [make_photo(seed=1)], rotation=180)
    with PdfDocument(path) as document:
        output = page_output(document.load_page(0), config)
    assert "render" in output.reason


def test_page_with_two_images_falls_back_to_rendering(tmp_path):
    document = pymupdf.open()
    page = document.new_page(width=400, height=600)
    page.insert_image(pymupdf.Rect(0, 0, 400, 300), stream=encode_jpeg(make_photo(seed=2)))
    page.insert_image(pymupdf.Rect(0, 300, 400, 600), stream=encode_jpeg(make_photo(seed=3)))
    path = tmp_path / "two.pdf"
    document.save(path)
    document.close()

    with PdfDocument(path) as opened:
        stream, _, reason = probe_original_stream(opened.load_page(0), CONFIG)
        output = page_output(opened.load_page(0), CONFIG)
    assert stream is None
    assert reason.startswith("image_count")
    assert not output.passthrough


def test_small_image_on_a_large_page_falls_back_to_rendering(tmp_path):
    document = pymupdf.open()
    page = document.new_page(width=600, height=800)
    page.insert_image(pymupdf.Rect(100, 100, 300, 300), stream=encode_jpeg(make_photo(seed=4)))
    path = tmp_path / "small.pdf"
    document.save(path)
    document.close()

    with PdfDocument(path) as opened:
        stream, _, reason = probe_original_stream(opened.load_page(0), CONFIG)
    assert stream is None
    assert reason.startswith("coverage")


def test_output_size_matches_what_load_page_array_produces(tmp_path):
    """The offset conversion between scoring and merging depends on this."""
    path = build_pdf(tmp_path / "scan.pdf", [make_photo(seed=5)], rotation=270)
    with PdfDocument(path) as document:
        page = document.load_page(0)
        array = load_page_array(page, CONFIG)
        assert output_size(page, CONFIG) == (array.shape[1], array.shape[0])


def test_render_page_returns_a_three_channel_array(tmp_path):
    path = build_pdf(tmp_path / "scan.pdf", [make_text_page(seed=6)])
    with PdfDocument(path) as document:
        array = render_page(document.load_page(0), 72)
    assert array.ndim == 3 and array.shape[2] == 3


def test_missing_file_is_reported_not_raised_as_a_crash(tmp_path):
    with pytest.raises(PdfDocumentError):
        PdfDocument(tmp_path / "nope.pdf").open()


def test_encrypted_pdf_requires_a_password(tmp_path):
    document = pymupdf.open()
    document.new_page(width=200, height=200)
    path = tmp_path / "locked.pdf"
    document.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret")
    document.close()

    with pytest.raises(PdfPasswordRequiredError):
        PdfDocument(path).open()
    with PdfDocument(path, password="secret") as opened:
        assert opened.page_count == 1


def test_merge_places_the_left_page_first():
    left, right = spread_pages(seed=3)
    merged = merge_pages(left, right, 0, MergeConfig())
    assert merged.shape[1] == left.shape[1] + right.shape[1]
    assert np.array_equal(merged[:, : left.shape[1]], left)


def test_merge_applies_the_vertical_offset_without_cropping():
    """The canvas grows to hold the shifted page: cropping would lose content."""
    left, right = spread_pages(seed=3)
    merged = merge_pages(left, right, 20, MergeConfig())
    assert merged.shape[0] == left.shape[0] + 20
    assert np.array_equal(merged[20 : 20 + right.shape[0], left.shape[1] :], right)

    merged = merge_pages(left, right, -20, MergeConfig())
    assert merged.shape[0] == left.shape[0] + 20
    assert np.array_equal(merged[20 : 20 + left.shape[0], : left.shape[1]], left)


def test_merge_can_ignore_the_offset():
    left, right = spread_pages(seed=3)
    merged = merge_pages(left, right, 30, MergeConfig(apply_vertical_offset=False))
    assert merged.shape[0] == left.shape[0]


def test_merge_trims_the_gutter_when_asked():
    left, right = spread_pages(seed=3)
    merged = merge_pages(left, right, 0, MergeConfig(gutter_trim_px=10))
    assert merged.shape[1] == left.shape[1] + right.shape[1] - 20
