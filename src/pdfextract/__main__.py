"""Entry point for ``python -m pdfextract``.

With no argument the graphical interface is started; with arguments the call is
forwarded to the command line interface, so that both share a single entry point.
"""

from __future__ import annotations

import sys


def main() -> int:
    """Start the GUI, or the CLI when arguments are supplied."""
    if len(sys.argv) > 1:
        from pdfextract.cli import main as cli_main

        return cli_main(sys.argv[1:])

    from pdfextract.ui.app import run_gui

    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
