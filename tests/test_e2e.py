"""End-to-end tests: run main() against local Mimir/CIM stand-ins.

Covers the accounting rules agreed for EnergyRecord fields: lifespan
bounding, ExecUnitFinished/Status detection, per-datacenter Owner mapping,
the fixed fields (including CloudComputeService), and both senders
(cim/file).
"""

import json
import uuid

import yaml

from ai4eosc_energy_accounting import main as main_mod

STEP = 30


def _write_config(tmp_path, mimir_server, cim_server=None, **overrides):
    cfg = {
        "mimir": {
            "endpoint": mimir_server.endpoint,
            "user": "u",
            "password": "p",
            "step_seconds": STEP,
            "verify_ssl": False,
        },
        "cim": {
            "base_url": cim_server.base_url if cim_server else "http://unused",
            "email": "e@x.com",
            "password": "p",
            "verify_ssl": False,
        },
        "accounting": {},
        "pointer": {
            "file": str(tmp_path / "lastrun"),
            "open_allocations_file": str(tmp_path / "open_allocations.json"),
            "initial_lookback_hours": 1,
            "query_lag_seconds": 5,
        },
        "sender": {"file_path": str(tmp_path / "records.jsonl")},
    }
    for section, values in overrides.items():
        cfg.setdefault(section, {}).update(values)

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path)


def _series(alloc_id, datacenter, values):
    return {
        "metric": {
            "container_label_com_hashicorp_nomad_alloc_id": alloc_id,
            "datacenter": datacenter,
        },
        "values": values,
    }


def test_running_allocation_reports_fixed_and_derived_fields(
    tmp_path, mimir_server, cim_server
):
    alloc_id = str(uuid.uuid4())

    def build(start, end):
        values = []
        t = start
        while t <= end:
            values.append([t, "5000000"])  # 5W
            t += STEP
        return [_series(alloc_id, "ifca-ai4eosc", values)]

    mimir_server.set_window_fn(build)
    config_path = _write_config(tmp_path, mimir_server, cim_server)

    rc = main_mod.main(["--config", config_path])
    assert rc == 0

    [rec] = cim_server.payload
    assert rec["ExecUnitID"] == alloc_id
    assert rec["CloudComputeService"] == "AI4EOSC"
    assert rec["Owner"] == "ai4eosc.eu"
    assert rec["SiteName"] == "IFCA-LCG2"
    assert rec["CloudType"] == "container (PaaS)"
    assert rec["Efficiency"] == 1.0
    assert rec["SuspendDuration_s"] == 0
    assert rec["CPUNormalizationFactor"] == 2.03
    assert rec["ExecUnitFinished"] == 0
    assert rec["Status"] == "running"
    assert rec["EnergyWh"] > 0
    assert rec["CpuDuration_s"] == rec["WallClockTime_s"]
    assert rec["Work"] == rec["CpuDuration_s"] / rec["EnergyWh"]


def test_allocation_stopped_mid_window_is_finished_and_bounded(
    tmp_path, mimir_server, cim_server
):
    alloc_id = str(uuid.uuid4())

    def build(start, end):
        cutoff = end - 600  # stopped 10 minutes before window end
        values = []
        t = start
        while t <= cutoff:
            values.append([t, "3000000"])  # 3W
            t += STEP
        return [_series(alloc_id, "ifca-imagine", values)]

    mimir_server.set_window_fn(build)
    config_path = _write_config(tmp_path, mimir_server, cim_server)

    rc = main_mod.main(["--config", config_path])
    assert rc == 0

    [rec] = cim_server.payload
    assert rec["ExecUnitFinished"] == 1
    assert rec["Status"] == "completed"
    assert rec["CloudComputeService"] == "AI4EOSC"  # fixed regardless of datacenter
    assert rec["Owner"] == "imagine-ai.eu"
    # bounded to the allocation's actual lifespan, well short of a 1h window
    assert rec["WallClockTime_s"] < 3300


def test_allocation_vanishing_right_at_window_end_is_closed_next_run(
    tmp_path, mimir_server, cim_server
):
    """The edge case the pointer alone can't catch.

    Run 1: the allocation's very last-ever sample happens to land exactly
    at the window end, so it's (correctly, from that window's point of
    view) reported as still "running". Run 2: it produces zero samples at
    all (it's actually gone). Without persisted state there would be no
    record to ever mark it finished; with it, run 2 must synthesize a
    zero-energy closing record from what run 1 remembered.
    """
    alloc_id = str(uuid.uuid4())
    config_path = _write_config(tmp_path, mimir_server, cim_server)

    # Run 1: single sample exactly at the window's end timestamp.
    mimir_server.set_window_fn(
        lambda start, end: [_series(alloc_id, "ifca-ai4eosc", [[end, "5000000"]])]
    )
    rc = main_mod.main(["--config", config_path])
    assert rc == 0

    [rec1] = cim_server.payload
    assert rec1["ExecUnitID"] == alloc_id
    assert rec1["ExecUnitFinished"] == 0
    assert rec1["Status"] == "running"

    # Run 2: the allocation is gone, no samples anywhere.
    mimir_server.set_window_fn(lambda start, end: [])
    rc = main_mod.main(["--config", config_path])
    assert rc == 0

    [rec2] = cim_server.payload
    assert rec2["ExecUnitID"] == alloc_id
    assert rec2["ExecUnitFinished"] == 1
    assert rec2["Status"] == "completed"
    assert rec2["EnergyWh"] == 0.0
    assert rec2["CloudComputeService"] == "AI4EOSC"
    assert rec2["Owner"] == "ai4eosc.eu"
    # closed at its last known instant, not stretched to run 2's window
    assert rec2["StartExecTime"] == rec2["EndExecTime"] == rec1["EndExecTime"]

    # and it's not reported as open any more after being closed
    open_allocs = json.loads((tmp_path / "open_allocations.json").read_text())
    assert alloc_id not in open_allocs


def test_unknown_datacenter_falls_back_to_raw_label_for_owner(
    tmp_path, mimir_server, cim_server
):
    alloc_id = str(uuid.uuid4())

    def build(start, end):
        return [
            _series(
                alloc_id,
                "some-other-dc",
                [[start, "1000000"], [start + STEP, "1000000"]],
            )
        ]

    mimir_server.set_window_fn(build)
    config_path = _write_config(tmp_path, mimir_server, cim_server)

    rc = main_mod.main(["--config", config_path])
    assert rc == 0

    [rec] = cim_server.payload
    # CloudComputeService is always fixed, unaffected by an unmapped datacenter
    assert rec["CloudComputeService"] == "AI4EOSC"
    assert rec["Owner"] == "some-other-dc"


def test_file_sender_writes_payload_and_advances_pointer(tmp_path, mimir_server):
    alloc_id = str(uuid.uuid4())

    def build(start, end):
        return [
            _series(
                alloc_id,
                "ifca-ai4eosc",
                [[start, "5000000"], [start + STEP, "5000000"]],
            )
        ]

    mimir_server.set_window_fn(build)
    config_path = _write_config(tmp_path, mimir_server, sender={"type": "file"})

    output_path = tmp_path / "records.jsonl"
    rc = main_mod.main(["--config", config_path])
    assert rc == 0

    payload = json.loads(output_path.read_text())
    assert len(payload) == 1
    assert payload[0]["ExecUnitID"] == alloc_id
    assert (tmp_path / "lastrun").exists()


def test_no_data_still_advances_pointer_and_exits_cleanly(
    tmp_path, mimir_server, cim_server
):
    mimir_server.set_series([])
    config_path = _write_config(tmp_path, mimir_server, cim_server)

    rc = main_mod.main(["--config", config_path])
    assert rc == 0
    assert cim_server.payload is None
    # a run with no records still advances the pointer (nothing to retry)
    assert (tmp_path / "lastrun").exists()


def test_failed_push_does_not_advance_pointer(tmp_path, mimir_server, cim_server):
    alloc_id = str(uuid.uuid4())

    def build(start, end):
        return [
            _series(
                alloc_id,
                "ifca-ai4eosc",
                [[start, "5000000"], [start + STEP, "5000000"]],
            )
        ]

    mimir_server.set_window_fn(build)
    cim_server.set_fail(True)
    config_path = _write_config(tmp_path, mimir_server, cim_server)

    rc = main_mod.main(["--config", config_path])
    assert rc == 1
    assert not (tmp_path / "lastrun").exists()
    assert not (tmp_path / "open_allocations.json").exists()


def test_sender_type_can_be_overridden_from_cli(tmp_path, mimir_server, cim_server):
    """--sender file must not require CIM credentials to validate."""
    alloc_id = str(uuid.uuid4())

    def build(start, end):
        return [_series(alloc_id, "ifca-ai4eosc", [[start, "5000000"]])]

    mimir_server.set_window_fn(build)
    config_path = _write_config(tmp_path, mimir_server, cim_server=None)

    output_path = tmp_path / "cli_output.jsonl"
    rc = main_mod.main(
        ["--config", config_path, "--sender", "file", "--output-file", str(output_path)]
    )
    assert rc == 0
    assert json.loads(output_path.read_text())
