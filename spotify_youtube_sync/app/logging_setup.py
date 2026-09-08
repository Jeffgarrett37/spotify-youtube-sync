"""Logging configuration.

Home Assistant shows whatever the add-on writes to stdout/stderr in the
add-on "Log" tab, so we just need a clean single-line formatter and a level
map that accepts the add-on's ``log_level`` option (including ``trace``).
"""

from __future__ import annotations

import logging
import sys

TRACE_LEVEL = 5

_LEVELS = {
    "trace": TRACE_LEVEL,
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}


def _trace(self: logging.Logger, message: str, *args: object, **kwargs: object) -> None:
    if self.isEnabledFor(TRACE_LEVEL):
        self._log(TRACE_LEVEL, message, args, **kwargs)  # type: ignore[arg-type]


def configure(level: str) -> None:
    logging.addLevelName(TRACE_LEVEL, "TRACE")
    logging.Logger.trace = _trace  # type: ignore[attr-defined]

    resolved = _LEVELS.get(level.lower().strip(), logging.INFO)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved)

    # Third-party libraries are noisy at INFO; keep them at WARNING unless we
    # are explicitly debugging.
    noisy = ["googleapiclient", "google_auth_httplib2", "google.auth",
             "urllib3", "requests", "oauthlib", "requests_oauthlib"]
    lib_level = logging.DEBUG if resolved <= logging.DEBUG else logging.WARNING
    for name in noisy:
        logging.getLogger(name).setLevel(lib_level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
