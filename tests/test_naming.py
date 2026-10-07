"""Tests of the filename template engine."""

from __future__ import annotations

from datetime import date

import pytest

from pdfextract.core import naming
from pdfextract.core.naming import NameContext, NamingError


def test_pages_token_is_zero_padded():
    """A single page is padded to four digits, a merge shows both pages."""
    assert naming.format_pages(12) == "0012"
    assert naming.format_pages(12, 13) == "0012-0013"
    assert naming.format_pages(12, 12) == "0012"


def test_every_token_expands():
    context = NameContext(
        pdf_stem="book", page_start=7, page_end=8, mode=3, index=2,
        processed_on=date(2026, 9, 16),
    )
    template = "{pdf}-{pages}-{page_start}-{page_end}-{type}-{mode}-{date}-{index}"
    assert naming.render_template(template, context) == (
        "book-0007-0008-0007-0008-merged-3-2026-09-16-0002"
    )


def test_unknown_token_is_rejected():
    with pytest.raises(NamingError):
        naming.render_template("{pdf}_{oops}", NameContext(pdf_stem="b", page_start=1))


def test_index_token_raises_a_warning():
    """The interface must warn about {index}: it does not follow the book order."""
    warnings = naming.validate_template("{pdf}_{index}")
    assert any("index" in warning for warning in warnings)


def test_template_without_page_token_raises_a_warning():
    warnings = naming.validate_template("{pdf}")
    assert any("page token" in warning for warning in warnings)


@pytest.mark.parametrize("raw", ['a/b', 'a\\b', 'a:b', 'a*b', 'a?b', 'a"b', 'a<b', 'a>b', 'a|b'])
def test_forbidden_windows_characters_are_replaced(raw):
    assert naming.sanitise_filename(raw) == "a_b"


def test_reserved_device_names_are_escaped():
    assert naming.sanitise_filename("CON") == "_CON"
    assert naming.sanitise_filename("nul.jpg") == "_nul.jpg"


def test_trailing_dots_and_spaces_are_dropped():
    assert naming.sanitise_filename("name. ") == "name"


def test_collisions_get_an_incremental_suffix(tmp_path):
    first = naming.build_output_path(tmp_path, "page", "jpg")
    first.write_bytes(b"x")
    second = naming.build_output_path(tmp_path, "page", "jpg")
    assert second.name == "page_2.jpg"


def test_paths_allocated_but_not_written_still_collide(tmp_path):
    taken: set = set()
    first = naming.build_output_path(tmp_path, "page", "jpg", taken=taken)
    second = naming.build_output_path(tmp_path, "page", "jpg", taken=taken)
    assert first != second


def test_long_names_are_truncated_to_the_path_limit(tmp_path):
    path = naming.build_output_path(tmp_path, "x" * 400, "jpg")
    assert len(str(path)) <= naming.MAX_PATH_LENGTH


def test_output_order_follows_the_book_not_the_writing_order(tmp_path):
    """The guarantee that makes {pages} the default token.

    Safe pages are written immediately and ambiguous ones only after the user has
    decided, possibly much later and in any order. Sorting the output folder must
    still give the order of the book.
    """
    writing_order = [(5, None), (1, None), (8, 9), (2, 3), (6, None), (10, None), (4, None)]
    for position, (start, end) in enumerate(writing_order):
        context = NameContext(pdf_stem="book", page_start=start, page_end=end, mode=3,
                              index=position)
        stem = naming.render_template(naming.DEFAULT_TEMPLATE, context)
        naming.build_output_path(tmp_path, stem, "jpg").write_bytes(b"x")

    names = sorted(path.name for path in tmp_path.iterdir())
    first_pages = [int(name.split("_")[1][:4]) for name in names]
    assert first_pages == sorted(first_pages)
    assert names[0] == "book_0001.jpg"
    assert names[1] == "book_0002-0003.jpg"


def test_output_directory_mirrors_the_source_tree(tmp_path):
    source_root = tmp_path / "scans"
    pdf = source_root / "1950" / "atlas.pdf"
    directory = naming.output_directory(
        tmp_path / "out", pdf, source_root, subfolder_per_pdf=True, mirror_source_tree=True
    )
    assert directory == tmp_path / "out" / "1950" / "atlas"


def test_output_directory_without_mirroring(tmp_path):
    pdf = tmp_path / "scans" / "1950" / "atlas.pdf"
    directory = naming.output_directory(
        tmp_path / "out", pdf, None, subfolder_per_pdf=False, mirror_source_tree=False
    )
    assert directory == tmp_path / "out"
