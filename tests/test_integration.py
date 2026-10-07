"""End to end test: synthetic PDF files in, an expected output tree out."""

from __future__ import annotations

from pathlib import Path

from pdfextract.cli import main

from fixture_builder import build_pdf, make_text_page, spread_pages


def _book(path: Path, spreads: int = 2, fly_leaves: int = 1) -> Path:
    pages = [make_text_page(seed=400 + index) for index in range(fly_leaves)]
    for index in range(spreads):
        left, right = spread_pages(seed=2 + index, noise=3.0, dy=3 - index)
        pages += [left, right]
    return build_pdf(path, pages)


def test_a_recursive_import_reproduces_the_source_tree(tmp_path):
    sources = tmp_path / "scans"
    (sources / "1950").mkdir(parents=True)
    (sources / "1960" / "atlas").mkdir(parents=True)
    _book(sources / "1950" / "flora.pdf")
    _book(sources / "1960" / "atlas" / "maps.pdf", spreads=1)
    output = tmp_path / "out"

    code = main(["extract", "--mode", "3", "--recursive", "--out", str(output),
                 str(sources)])
    assert code == 0

    assert (output / "1950" / "flora").is_dir()
    assert (output / "1960" / "atlas" / "maps").is_dir()


def test_mode_three_produces_the_expected_files(tmp_path):
    source = tmp_path / "book.pdf"
    _book(source, spreads=3, fly_leaves=1)
    output = tmp_path / "out"

    assert main(["extract", "--mode", "3", "--out", str(output), str(source)]) == 0

    produced = sorted(path.name for path in (output / "book").iterdir())
    assert produced == [
        "book_0001.jpg",
        "book_0002-0003.jpg",
        "book_0004-0005.jpg",
        "book_0006-0007.jpg",
    ]


def test_alphabetical_order_matches_the_order_of_the_book(tmp_path):
    """The guarantee behind naming on source pages rather than on writing order."""
    source = tmp_path / "book.pdf"
    _book(source, spreads=3, fly_leaves=1)
    output = tmp_path / "out"
    main(["extract", "--mode", "3", "--out", str(output), str(source)])

    names = sorted(path.name for path in (output / "book").iterdir())
    first_pages = [int(name.split("_")[1][:4]) for name in names]
    assert first_pages == sorted(first_pages)


def test_mode_one_exports_one_image_per_page(tmp_path):
    source = tmp_path / "book.pdf"
    _book(source, spreads=2, fly_leaves=1)
    output = tmp_path / "out"

    assert main(["extract", "--mode", "1", "--out", str(output), str(source)]) == 0
    assert len(list((output / "book").iterdir())) == 5


def test_a_custom_template_is_honoured(tmp_path):
    source = tmp_path / "book.pdf"
    _book(source, spreads=1, fly_leaves=0)
    output = tmp_path / "out"

    assert (
        main(
            [
                "extract", "--mode", "2", "--out", str(output),
                "--template", "{pdf}-{type}-{pages}", "--no-subfolder", str(source),
            ]
        )
        == 0
    )
    names = sorted(path.name for path in output.iterdir())
    assert names == ["book-merged-0001-0002.jpg"]


def test_a_broken_file_does_not_stop_the_others(tmp_path, capsys):
    (tmp_path / "broken.pdf").write_bytes(b"definitely not a pdf")
    _book(tmp_path / "good.pdf", spreads=1)
    output = tmp_path / "out"

    code = main(["extract", "--mode", "1", "--out", str(output), str(tmp_path)])
    assert code == 1  # the failure is reported
    assert (output / "good").is_dir()  # the healthy file was still processed


def test_auto_review_finishes_the_pending_pairs(tmp_path):
    """The scripted path through the validation queue, used for batch runs."""
    source = tmp_path / "book.pdf"
    _book(source, spreads=2, fly_leaves=1)
    output = tmp_path / "out"
    settings = tmp_path / "settings.json"
    settings.write_text(
        '{"schema_version": 1, "seam": {"threshold_merge": 0.99, "threshold_split": 0.01}}',
        encoding="utf-8",
    )

    code = main(
        ["--settings", str(settings), "extract", "--mode", "3", "--out", str(output),
         "--auto-review", "merge", str(source)]
    )
    assert code == 0
    names = sorted(path.name for path in (output / "book").iterdir())
    assert any("-" in name for name in names), "no pair was merged"
    assert len(names) == 3  # 1-2, 3-4 and 5 on its own


def test_the_sidecar_records_the_decisions(tmp_path):
    from pdfextract.core import sidecar

    source = tmp_path / "book.pdf"
    _book(source, spreads=2, fly_leaves=1)
    settings = tmp_path / "settings.json"
    settings.write_text(
        '{"schema_version": 1, "seam": {"threshold_merge": 0.99, "threshold_split": 0.01}}',
        encoding="utf-8",
    )
    main(
        ["--settings", str(settings), "extract", "--mode", "3",
         "--out", str(tmp_path / "out"), "--auto-review", "split", str(source)]
    )
    record = sidecar.load(source)
    assert record is not None
    assert record.state == "done"
    assert set(record.decisions.values()) == {"split"}
