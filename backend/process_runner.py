"""Run POSIX worker commands with bounded process-group cleanup.

SIGINT/SIGTERM handling applies when called from the main thread. SIGKILL and
power loss cannot be handled; the worker passes its lock descriptor to children
so an orphaned direct child retains the exclusion lock until it exits.
"""

from contextlib import contextmanager
import os
import signal
import subprocess
from threading import current_thread, main_thread
import time


class ProcessInterrupted(RuntimeError):
    """A worker command was interrupted by an operator or service stop."""


@contextmanager
def interruption_handlers():
    previous = {}
    state = {"cleaning": False}

    def interrupted(signum, frame):
        if not state["cleaning"]:
            raise ProcessInterrupted(f"Command interrupted by {signal.Signals(signum).name}.")

    if current_thread() is main_thread():
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, interrupted)
    try:
        yield state
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def group_exists(group):
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    return True


def stop_group(process, grace_seconds):
    """Terminate the private group, escalate if needed, and reap its leader."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        process.poll()
        if not group_exists(process.pid):
            break
        time.sleep(0.02)
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def run_process(command, *, timeout, check=False, cleanup_grace=1.0, **kwargs):
    """Match the worker's subprocess.run interface, cleaning up descendants.

Commands must not escape their private process group by daemonizing. The result
is returned only after cleanup, so the worker still holds its lock throughout.
"""
    if "start_new_session" in kwargs:
        raise ValueError("The worker runner owns the child process session.")
    process = None
    with interruption_handlers() as state:
        try:
            process = subprocess.Popen(command, start_new_session=True, **kwargs)
            returncode = process.wait(timeout=timeout)
        finally:
            state["cleaning"] = True
            if process is not None:
                stop_group(process, cleanup_grace)
    result = subprocess.CompletedProcess(command, returncode)
    if check and returncode:
        raise subprocess.CalledProcessError(returncode, command)
    return result
