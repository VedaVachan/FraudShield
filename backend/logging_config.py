"""Configure the project's standard-library logging once per process."""

import logging
import os
import sys

LOG_LEVEL_VARIABLE = "FRAUDSHIELD_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
PROJECT_LOGGERS = ("backend", "fraudshield")
_ALLOWED_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
_HANDLER_MARK = "_fraudshield_stderr_handler"


def resolve_level(value=None):
    """Return a numeric level from a name, falling back to INFO if unknown."""
    if value is None:
        value = os.environ.get(LOG_LEVEL_VARIABLE, DEFAULT_LEVEL)
    name = value.strip().upper() if isinstance(value, str) else ""
    return _ALLOWED_LEVELS.get(name, _ALLOWED_LEVELS[DEFAULT_LEVEL])


def configure_logging(level=None):
    """Attach one stderr handler to the project loggers; repeat calls add none.

    The level comes from the argument or FRAUDSHIELD_LOG_LEVEL and defaults to
    INFO. Only the project's own loggers are configured; the root logger and
    third-party loggers are left unchanged.
    """
    numeric = resolve_level(level)
    for name in PROJECT_LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(numeric)
        logger.propagate = False
        if not any(getattr(h, _HANDLER_MARK, False) for h in logger.handlers):
            handler = logging.StreamHandler(sys.stderr)
            handler.setFormatter(logging.Formatter(LOG_FORMAT))
            setattr(handler, _HANDLER_MARK, True)
            logger.addHandler(handler)
    return numeric
