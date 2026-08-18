"""Track the end of the last successfully-sent accounting window.

Modeled after cASO's ``lastrun`` file (``caso.extract.manager.Manager``):
a single small text file holds an ISO-8601 UTC timestamp. It is only
updated after a run completes successfully, so a failed run retries the
same window next time instead of losing data.
"""

import datetime
import os

import dateutil.parser


def read_pointer(path: str, default_lookback_hours: int) -> datetime.datetime:
    """Return the start of the extraction window.

    If no pointer file exists yet (first run), fall back to
    ``now - default_lookback_hours`` so the first invocation does not try
    to backfill unbounded history.
    """
    if os.path.exists(path):
        with open(path, "r") as fd:
            content = fd.read().strip()
        if content:
            parsed = dateutil.parser.isoparse(content)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            return parsed

    return datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=default_lookback_hours
    )


def write_pointer(path: str, when: datetime.datetime) -> None:
    """Persist ``when`` as the new last-sent instant, atomically."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as fd:
        fd.write(when.strftime("%Y-%m-%dT%H:%M:%SZ"))
    os.replace(tmp_path, path)
