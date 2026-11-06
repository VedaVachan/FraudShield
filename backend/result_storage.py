"""Publish complete worker JSON without replacing existing evidence (POSIX)."""

import json
import os
from pathlib import Path
import tempfile


def publish_result(path, outcome):
    """Publish a synced temporary file with an exclusive atomic hard link.

    The caller must hold the worker lock. A failure after publication may leave
    a complete result beside a running queue row; do not infer a committed row
    or rerun training. Temporary files left by SIGKILL are never result files.
    """
    path = Path(path)
    payload = (json.dumps(outcome, indent=2, allow_nan=False) + "\n").encode("utf-8")
    descriptor, name = tempfile.mkstemp(prefix=".result-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # link() fails if path exists: preserve the old exclusive-create contract.
        os.link(temporary, path)
        temporary.unlink()
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
