"""Application log.

Anything irreversible, and in particular every move to the recycle bin, is written
here: if there is ever a doubt about what was removed, this file has to be able to
answer it.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pdfextract.core.settings import app_data_dir

LOGGER_NAME = "pdfextract"
_configured = False


def log_directory() -> Path:
    """Return the folder holding the application logs."""
    path = app_data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path() -> Path:
    """Return the path of the current log file."""
    return log_directory() / "pdfextract.log"


def get_logger(directory: Path | None = None) -> logging.Logger:
    """Return the application logger, configuring it on first use."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    if not _configured:
        target = (directory or log_directory()) / "pdfextract.log"
        handler = RotatingFileHandler(
            target, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        _configured = True
    return logger
