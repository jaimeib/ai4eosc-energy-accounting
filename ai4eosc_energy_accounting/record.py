"""EnergyRecord definition for AI4EOSC PaaS containers.

Field names and the wire format mirror cASO's ``caso.record.EnergyRecord``
(see the IFCA cASO project, ``caso/record.py``), so that CIM can ingest VM
records (from cASO) and container records (from this messenger) the same
way. VMs are reported as Cloud IaaS Execution Units; containers here are
reported as Cloud PaaS Execution Units, distinguished via ``CloudType`` /
``CloudComputeService``.
"""

import json
import typing
import uuid

import pydantic


def _map_energy_fields(field: str) -> str:
    d = {
        "exec_unit_id": "ExecUnitID",
        "start_exec_time": "StartExecTime",
        "end_exec_time": "EndExecTime",
        "energy_wh": "EnergyWh",
        "work": "Work",
        "efficiency": "Efficiency",
        "wall_clock_time_s": "WallClockTime_s",
        "cpu_duration_s": "CpuDuration_s",
        "suspend_duration_s": "SuspendDuration_s",
        "cpu_normalization_factor": "CPUNormalizationFactor",
        "exec_unit_finished": "ExecUnitFinished",
        "status": "Status",
        "owner": "Owner",
        "site_name": "SiteName",
        "cloud_type": "CloudType",
        "compute_service": "CloudComputeService",
    }
    return d.get(field, field)


class EnergyRecord(pydantic.BaseModel):
    """Energy consumption record for a single PaaS container execution unit."""

    exec_unit_id: uuid.UUID
    start_exec_time: str
    end_exec_time: str
    energy_wh: float
    work: float
    efficiency: float
    wall_clock_time_s: int
    cpu_duration_s: int
    suspend_duration_s: int
    cpu_normalization_factor: float
    exec_unit_finished: int
    status: str
    owner: str
    site_name: str
    cloud_type: str
    compute_service: str

    def cim_message(self) -> dict:
        """Render the record as a dict ready to be JSON-serialized for CIM."""
        opts: typing.Dict[str, typing.Any] = {"by_alias": True, "exclude_none": True}
        return json.loads(self.model_dump_json(**opts))

    model_config = pydantic.ConfigDict(
        alias_generator=_map_energy_fields,
        populate_by_name=True,
        extra="forbid",
    )
