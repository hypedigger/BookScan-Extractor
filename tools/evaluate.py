"""Grade the smart mode on real books against hand made labels.

The labels live outside the repository, in a JSON file of this shape::

    {
      "book.pdf": {"spreads": [2, 6, 12], "unsure": [5]},
      ...
    }

``spreads`` lists the first page (1-based) of every double page spread, ``unsure``
the pairs not to grade. For every book the script runs the same plan as the
application (mode 3) and reports what the user would see::

    uv run python tools/evaluate.py labels.json --books "D:/my books"

* merged: spreads merged without a question, the goal;
* asked: spreads the user is asked about;
* missed: spreads silently exported as two pages;
* wrong: pairs merged without a question that are not spreads, the worst error;
* questions: every question asked, and how many of them were about non-spreads.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from pdfextract.core.document import PdfDocument
from pdfextract.core.job import ItemKind
from pdfextract.core.pairing import build_plan
from pdfextract.core.settings import AppSettings


def grade(path: Path, spreads: set[int], unsure: set[int]) -> dict[str, int]:
    """Run mode 3 on one book and count the outcomes."""
    settings = AppSettings()
    settings.mode = 3
    with PdfDocument(path) as document:
        plan = build_plan(document, settings)
    sure = {
        item.page_start
        for item in plan.items
        if item.kind is ItemKind.MERGED and not item.uncertain
    }
    asked = {pair.page_start for pair in plan.review}
    counts = {"merged": 0, "asked": 0, "missed": 0, "wrong": 0, "questions": 0, "useless": 0}
    for page in spreads - unsure:
        if page in sure:
            counts["merged"] += 1
        elif page in asked:
            counts["asked"] += 1
        else:
            counts["missed"] += 1
    counts["wrong"] = len(sure - spreads - unsure)
    counts["questions"] = len(asked)
    counts["useless"] = len(asked - spreads - unsure)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("labels", type=Path, help="JSON file of labels, kept out of the repo")
    parser.add_argument("--books", type=Path, default=None, help="folder holding the PDFs")
    args = parser.parse_args(argv)

    labels = json.loads(args.labels.read_text(encoding="utf-8"))
    folder = args.books or args.labels.parent
    total: dict[str, int] = {}
    for name, entry in labels.items():
        path = folder / name
        if not path.exists():
            print(f"{name}: not found in {folder}", file=sys.stderr)
            continue
        started = time.perf_counter()
        counts = grade(path, set(entry.get("spreads", [])), set(entry.get("unsure", [])))
        elapsed = time.perf_counter() - started
        for key, value in counts.items():
            total[key] = total.get(key, 0) + value
        print(
            f"{name[:40]:40} merged {counts['merged']:3}  asked {counts['asked']:3}  "
            f"missed {counts['missed']:3}  wrong {counts['wrong']:3}  "
            f"questions {counts['questions']:3} ({counts['useless']} on non-spreads)  "
            f"{elapsed:5.1f} s"
        )
    if total:
        print(
            f"{'TOTAL':40} merged {total['merged']:3}  asked {total['asked']:3}  "
            f"missed {total['missed']:3}  wrong {total['wrong']:3}  "
            f"questions {total['questions']:3} ({total['useless']} on non-spreads)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
