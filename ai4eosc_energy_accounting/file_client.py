"""Sender that writes records to a local file instead of CIM.

Meant for testing/inspection: same record payload that would be POSTed to
CIM (see ``cim_client.push_records``), just appended to a file so it can be
read back and eyeballed.
"""

import json
import logging
import os
import typing

if typing.TYPE_CHECKING:
    from ai4eosc_energy_accounting import record as record_mod

LOG = logging.getLogger(__name__)


def push_records(
    path: str, records: typing.Sequence["record_mod.EnergyRecord"]
) -> None:
    """Append the batch of records (as CIM would receive it) to ``path``."""
    if not records:
        return

    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    payload = [r.cim_message() for r in records]
    with open(path, "a") as fd:
        fd.write(json.dumps(payload, indent=2))
        fd.write("\n")

    LOG.info("Wrote %d record(s) to %s", len(payload), path)
