"""Track allocations that were still running at the end of the last run.

Closes the gap where an allocation's last-ever sample happens to land
within the "still running" tolerance of a window's end (see
``main.FINISHED_GAP_STEPS``): the *next* run has zero samples for it, so
there would otherwise never be a later window to notice it actually
stopped and report ``ExecUnitFinished``. Instead, each run persists which
allocations it reported as still running; the following run checks that
list against the allocations it actually saw data for, and synthesizes a
zero-energy "closing" record (see ``main.build_closing_records``) for any
that vanished entirely.

Same "only commit on success" discipline as ``pointer.py``.
"""

import json
import os
import typing


def read_open_allocations(path: str) -> typing.Dict[str, dict]:
    """Return ``{alloc_id: {"end_exec_time": ..., "datacenter": ...}}``."""
    if not os.path.exists(path):
        return {}
    with open(path, "r") as fd:
        content = fd.read().strip()
    return json.loads(content) if content else {}


def write_open_allocations(path: str, open_allocations: typing.Dict[str, dict]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as fd:
        json.dump(open_allocations, fd)
    os.replace(tmp_path, path)
