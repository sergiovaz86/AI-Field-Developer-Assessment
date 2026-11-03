"""Stderr logging for the gateway process."""

from __future__ import annotations

import logging
import sys
from typing import Any

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def configure_logging(level_name: str) -> None:
    """Replace root handlers with one stderr handler.

    Call this before serving so later libraries do not inherit a stdout
    handler installed by an implicit basicConfig.
    """
    level = _LEVELS[level_name]
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root.addHandler(handler)
    root.setLevel(level)


def stderr_log_config(level_name: str) -> dict[str, Any]:
    """Return a uvicorn logging dict that writes only to stderr.

    Access logs stay disabled by the server entry point. This configuration
    still points every uvicorn logger at stderr so a later opt-in cannot
    fall back to stdout.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                "datefmt": "%Y-%m-%dT%H:%M:%S%z",
            }
        },
        "handlers": {
            "stderr": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            }
        },
        "loggers": {
            "uvicorn": {"handlers": ["stderr"], "level": level_name, "propagate": False},
            "uvicorn.error": {"handlers": ["stderr"], "level": level_name, "propagate": False},
            "uvicorn.access": {"handlers": ["stderr"], "level": level_name, "propagate": False},
        },
    }
