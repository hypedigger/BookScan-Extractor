# 📖 BookScan Extractor

Desktop application (Windows 11) that extracts page images from scanned book PDFs and
re-joins the double-page spreads.

Three extraction modes:

| Mode | Name | Behaviour |
|---|---|---|
| 1 | Single | One output image per PDF page, no merging. |
| 2 | Systematic pairing | Every page merged two by two, with the starting offset (fly-leaf, half-title…) detected automatically. |
| 3 | Smart | Decided pair by pair from a gutter continuity score; ambiguous pairs go to a validation screen. |

The engine (`src/pdfextract/core/`) never imports PySide6 and is fully usable from
the command line. The interface only drives it and reports its progress.

## Installation

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1
```

Installs `uv` if needed (it brings its own Python), creates the virtual environment
and puts a shortcut in the Start Menu.

The shortcut targets `.venv\Scripts\pdfextract-gui.exe`, the launcher declared
under `[project.gui-scripts]` in `pyproject.toml` and built as a windowed program.
Neither `run.cmd` nor `pythonw.exe` can be used for this: a `.cmd` file brings its
own `cmd.exe` window along, and the `pythonw.exe` of a uv virtual environment is a
trampoline compiled as a console program that starts the console interpreter, so it
allocates a console whatever its name suggests.

## Using it

Adding files, or a folder, lists them in the queue with a tick box already ticked.
Nothing is processed until **Start**, which only takes the ticked files; the others
stay in the list for later, or can be removed from it. The mode can be set globally
or overridden file by file, and a right click offers to run a file again, optionally
imposing where the mode 2 pairing starts.

Update with `update.cmd`, or from *Help > Check for updates* inside the application.
Both do a `git pull` followed by a dependency sync: there is no frozen executable to
rebuild.

## Command line

```bash
uv run pdfextract extract --mode 3 --out out book.pdf
uv run pdfextract extract --mode 2 --recursive --out out scans/
uv run pdfextract calibrate book.pdf          # score distribution and suggested thresholds
uv run pdfextract info book.pdf
uv run pdfextract settings --init
```

`extract` runs the same state machine as the interface: ambiguous pairs land in the
validation queue instead of blocking their file. `--auto-review engine|merge|split`
decides them without a user, for scripted batches.

## Calibrating the continuity score

The default thresholds (`threshold_merge` 0.72, `threshold_split` 0.45) are a
starting point. Calibrate them on real books before relying on mode 3:

```bash
uv run pdfextract calibrate "a real book.pdf" --list-uncertain
uv run python tools/score_report.py "a real book.pdf" --plate report.jpg
```

`calibrate` prints the distribution of the scores and proposes thresholds inside the
gap between the two populations. `score_report.py` prints every metric pair by pair
and, with `--plate`, writes a contact sheet showing each join next to its score,
which is the fastest way to see where the scorer is wrong. The weights of the
individual metrics are editable in *Settings > Continuity*.

## How the engine works

- **Extraction** keeps the original compressed stream untouched whenever the page
  holds a single full-page image and nothing has to be transformed: no re-encoding,
  no loss, nearly instantaneous. A rotated or mirrored page is decoded and
  reoriented at its source resolution rather than re-rendered at a fixed dpi.
  Anything else falls back to rendering.
- **Scoring** (`core/seam.py`) compares the inner edges of two consecutive pages on
  six metrics, ignoring the gutter strip itself, and searches a vertical offset over
  a few percent of the page height. Two pages whose edges carry no structure at all
  are split whatever the rest says: that is the case of two facing text pages, whose
  blank margins otherwise look like a perfect match.
- **Pairing** (`core/pairing.py`) uses a dynamic programme over the sequence of
  pages, so no page can end up in two pairs.
- **Naming** is based on the source page number, never on the order files reach the
  disk, because safe pages are written immediately and ambiguous ones only after the
  user has decided. Sorting the output folder always gives the order of the book.
- **Validation never blocks processing**: a file that needs the user exports
  everything it can, hands its ambiguous pairs over and releases its worker.
- **Sources go to the recycle bin only**, through `send2trash`, once every guard has
  passed, and every move is written to the application log.

## Layout

```
src/pdfextract/
├── cli.py                 headless interface
├── core/                  the engine, no Qt anywhere
│   ├── document.py        opening, page inventory, integrity
│   ├── extractor.py       native stream extraction, fallback rendering
│   ├── seam.py            gutter continuity scoring
│   ├── pairing.py         pairing decision, modes 2 and 3
│   ├── merger.py          merging two half-pages
│   ├── postprocess.py     deskew, crop, gutter shadow, contrast, resize
│   ├── naming.py          filename template engine
│   ├── sidecar.py         persistence of the user decisions
│   ├── settings.py        configuration and profiles
│   ├── trash.py           guarded move to the recycle bin
│   ├── review_queue.py    queue of batches awaiting validation
│   ├── job.py             job model and extraction plan
│   └── runner.py          the state machine and the worker pool
└── ui/                    PySide6
    ├── main_window.py     import, queue, settings
    ├── review_window.py   validation screen
    ├── preloader.py       high priority look ahead loader
    ├── queue_panel.py     the queue table
    ├── settings_dialog.py forms generated from the configuration dataclasses
    └── workers.py         engine callbacks to Qt signals
```

`runner.py` is not in the original specification: the job model has to stay
importable by `sidecar.py`, so the execution lives in its own module.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
uv run python tools/make_fixtures.py     # sample PDF files under tests/fixtures/generated
```

Code, names and comments are in English; the engine follows PEP 8 with type hints
throughout.

`send2trash` is replaced by a recorder for the whole test suite, so a regression in
the guards fails a test rather than moving one of your files.

## Support

If BookScan Extractor saved your book-digitizing project some pain:

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/hypedigger)

## License

[AGPL-3.0](LICENSE). The engine is built on PyMuPDF, which is itself AGPL v3; this
project therefore carries the same license. If you need a permissively licensed
alternative, the rendering could be ported to `pypdfium2` (BSD/Apache), which is
less convenient for pulling raw streams out but perfectly viable for rendering.
