#!/usr/bin/env python3
"""Send AI4EOSC PaaS container energy accounting records to GreenDIGIT CIM.

Queries Mimir for the ``scaph_process_power_consumption_microwatts`` metric,
aggregated per Nomad allocation (``container_label_com_hashicorp_nomad_alloc_id``)
and platform (``datacenter``), and pushes one EnergyRecord per allocation to
CIM (or to a local file, see ``sender.type``, for testing). Meant to be run
periodically (e.g. every 6h via cron); a pointer file tracks the end of the
last successfully-sent window so each run only covers new data.

Optionally, when ``mimir.gpu_query`` is configured, GPU power
(``DCGM_FI_DEV_POWER_USAGE``, joined against ``nomad_gpu_allocation_info`` to
attribute it to a Nomad alloc_id) is folded into the same per-allocation Wh
total as CPU, before the CPU normalization factor is applied -- see
``config.example.yaml`` for the query. Disabled by default (empty query),
since not every deployment scrapes DCGM / the alloc-mapper exporter.

Containers are reported the same way cASO reports VMs, but as Cloud PaaS
Execution Units rather than Cloud IaaS Execution Units (see
``accounting.cloud_type`` / ``accounting.compute_service`` in the
config).
"""

import argparse
import datetime
import logging
import logging.handlers
import os
import sys
import uuid

from ai4eosc_energy_accounting import cim_client
from ai4eosc_energy_accounting import config as config_mod
from ai4eosc_energy_accounting import file_client
from ai4eosc_energy_accounting import mimir
from ai4eosc_energy_accounting import pointer as pointer_mod
from ai4eosc_energy_accounting import record as record_mod
from ai4eosc_energy_accounting import state as state_mod

LOG = logging.getLogger("ai4eosc_energy_accounting")

# Tolerance, in multiples of the query step, used to decide whether an
# allocation's last sample falling short of the window end means it
# actually finished (as opposed to just scrape jitter).
FINISHED_GAP_STEPS = 1.5


def _split_time_chunks(start, end, step_seconds, max_points):
    """Yield non-overlapping (chunk_start, chunk_end) tuples.

    Keeps each Mimir query under ``max_points`` samples per series, in case
    the pointer is far behind (e.g. after an outage).
    """
    while start < end:
        remaining_steps = int((end - start).total_seconds() / step_seconds)
        chunk_steps = min(remaining_steps, max_points)
        chunk_end = start + datetime.timedelta(seconds=chunk_steps * step_seconds)
        yield start, min(chunk_end, end)
        start = chunk_end + datetime.timedelta(seconds=step_seconds)


def _accumulate_series(metrics, result, alloc_id_label, datacenter_label, factor):
    """Fold a Mimir query_range result into ``metrics``, keyed by
    ``(alloc_id, datacenter)``, converting each instantaneous sample to Wh
    via ``factor`` (a Riemann-sum approximation: each sample represents
    ``step_seconds`` of constant power). Used for both the CPU (scaphandre)
    and, optionally, GPU (DCGM) queries, so their Wh land in the same
    accumulator ahead of CPU normalization in ``build_records``.
    """
    for series in result:
        metric = series.get("metric", {})
        alloc_id = metric.get(alloc_id_label)
        datacenter = metric.get(datacenter_label)
        if not alloc_id or not datacenter:
            continue
        entry = metrics.setdefault(
            (alloc_id, datacenter), {"energy_wh": 0.0, "first_ts": None, "last_ts": None}
        )
        for ts, value in series.get("values", []):
            entry["energy_wh"] += float(value) * factor
            if entry["first_ts"] is None or ts < entry["first_ts"]:
                entry["first_ts"] = ts
            if entry["last_ts"] is None or ts > entry["last_ts"]:
                entry["last_ts"] = ts


def collect_metrics(client, cfg, extract_from, extract_to):
    """Query Mimir and return per-allocation energy and sample time bounds.

    :returns: ``{(alloc_id, datacenter): {"energy_wh", "first_ts", "last_ts"}}``
        where ``first_ts``/``last_ts`` (Unix timestamps) are the bounds of
        the allocation's samples actually seen in ``[extract_from,
        extract_to]`` -- i.e. its lifespan within the window if it started
        or stopped mid-window, or the window bounds if it ran throughout.
    """
    # scaph_process_power_consumption_microwatts is an instantaneous power
    # sample, not a counter, so energy is approximated as a Riemann sum:
    # each sample represents `step_seconds` of constant power.
    cpu_factor = cfg.step_seconds / 3600 / 1_000_000  # microwatt-seconds -> Wh
    # DCGM_FI_DEV_POWER_USAGE is reported in Watts (not microwatts).
    gpu_factor = cfg.step_seconds / 3600  # watt-seconds -> Wh

    metrics: dict = {}
    for chunk_start, chunk_end in _split_time_chunks(
        extract_from, extract_to, cfg.step_seconds, cfg.max_points_per_query
    ):
        LOG.debug("Querying Mimir [%s -> %s]", chunk_start, chunk_end)
        result = client.query_range(cfg.query, chunk_start, chunk_end, cfg.step_seconds)
        _accumulate_series(metrics, result, cfg.alloc_id_label, cfg.datacenter_label, cpu_factor)

        if cfg.gpu_query:
            gpu_result = client.query_range(
                cfg.gpu_query, chunk_start, chunk_end, cfg.step_seconds
            )
            _accumulate_series(
                metrics, gpu_result, cfg.gpu_alloc_id_label, cfg.datacenter_label, gpu_factor
            )

    return metrics


def _is_finished(last_sample_time, extract_to, step_seconds):
    """Whether ``last_sample_time`` falling short of the window end means finished."""
    finished_gap_s = step_seconds * FINISHED_GAP_STEPS
    return (extract_to - last_sample_time).total_seconds() > finished_gap_s


def _resolve_owner(cfg, datacenter, alloc_id):
    """Look up Owner for a `datacenter` label (CloudComputeService is fixed:
    iMagine is a deployment of the AI4EOSC platform, not a separate one)."""
    owner = cfg.owner_map.get(datacenter)
    if owner is None:
        LOG.warning(
            "Unknown datacenter label %r for allocation %s, reporting Owner as-is",
            datacenter,
            alloc_id,
        )
        owner = datacenter
    return owner


def build_records(metrics, cfg, extract_to, step_seconds):
    """Turn collected per-allocation metrics into EnergyRecord objects.

    :returns: ``(records, open_allocations)`` where ``open_allocations`` is
        ``{alloc_id: {"end_exec_time", "datacenter"}}`` for the allocations
        reported as still running (``ExecUnitFinished`` = 0) here -- to be
        persisted so a later run can detect if they actually stopped
        without ever producing a "finished" window (see state.py).
    """
    records = []
    open_allocations = {}

    for (alloc_id, datacenter), data in metrics.items():
        if data["energy_wh"] <= 0 or data["first_ts"] is None:
            continue
        try:
            exec_unit_id = uuid.UUID(alloc_id)
        except ValueError:
            LOG.warning("Skipping non-UUID allocation id %r", alloc_id)
            continue

        start_time = datetime.datetime.fromtimestamp(
            data["first_ts"], tz=datetime.timezone.utc
        )
        end_time = datetime.datetime.fromtimestamp(
            data["last_ts"], tz=datetime.timezone.utc
        )
        wall_clock_time_s = max(int((end_time - start_time).total_seconds()), 0)

        # No independent CPU usage source for containers: assume a single
        # CPU-equivalent, so CpuDuration_s tracks wall clock time and
        # Efficiency (CpuDuration_s / WallClockTime_s) is always 1.
        cpu_duration_s = wall_clock_time_s
        energy_wh = data["energy_wh"] * cfg.cpu_normalization_factor
        # Same formula as cASO: work achieved (CPU time) per Wh consumed.
        work = cpu_duration_s / energy_wh if energy_wh > 0 else 0.0

        finished = _is_finished(end_time, extract_to, step_seconds)
        exec_unit_finished = 1 if finished else 0
        status = "completed" if finished else "running"

        owner = _resolve_owner(cfg, datacenter, alloc_id)

        if not finished:
            open_allocations[alloc_id] = {
                "end_exec_time": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "datacenter": datacenter,
            }

        records.append(
            record_mod.EnergyRecord(
                exec_unit_id=exec_unit_id,
                start_exec_time=start_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                end_exec_time=end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                energy_wh=energy_wh,
                work=work,
                efficiency=1.0,
                wall_clock_time_s=wall_clock_time_s,
                cpu_duration_s=cpu_duration_s,
                suspend_duration_s=0,
                cpu_normalization_factor=cfg.cpu_normalization_factor,
                exec_unit_finished=exec_unit_finished,
                status=status,
                owner=owner,
                site_name=cfg.site_name,
                cloud_type=cfg.cloud_type,
                compute_service=cfg.compute_service,
            )
        )

    return records, open_allocations


def build_closing_records(previously_open, metrics, cfg):
    """Synthesize a zero-energy "finished" record for allocations that
    were reported as still running last time, but produced zero samples
    in the current window (i.e. they stopped without ever landing in a
    window that could observe the gap before its end -- see state.py).
    """
    seen_alloc_ids = {alloc_id for (alloc_id, _datacenter) in metrics}
    records = []

    for alloc_id, info in previously_open.items():
        if alloc_id in seen_alloc_ids:
            continue  # still emitting samples; covered by a normal record
        try:
            exec_unit_id = uuid.UUID(alloc_id)
        except ValueError:
            continue

        end_time_str = info["end_exec_time"]
        datacenter = info["datacenter"]
        owner = _resolve_owner(cfg, datacenter, alloc_id)

        LOG.info(
            "Allocation %s produced no samples this window; closing it as "
            "finished as of its last known sample (%s)",
            alloc_id,
            end_time_str,
        )

        records.append(
            record_mod.EnergyRecord(
                exec_unit_id=exec_unit_id,
                start_exec_time=end_time_str,
                end_exec_time=end_time_str,
                energy_wh=0.0,
                work=0.0,
                efficiency=1.0,
                wall_clock_time_s=0,
                cpu_duration_s=0,
                suspend_duration_s=0,
                cpu_normalization_factor=cfg.cpu_normalization_factor,
                exec_unit_finished=1,
                status="completed",
                owner=owner,
                site_name=cfg.site_name,
                cloud_type=cfg.cloud_type,
                compute_service=cfg.compute_service,
            )
        )

    return records


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=os.path.join(config_mod.PROJECT_ROOT, "config.yaml"),
        help="Path to the YAML config file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query and build records but do not send them or advance the pointer",
    )
    parser.add_argument(
        "--sender",
        choices=("cim", "file"),
        help="Override sender.type: push to CIM, or append to a local file for testing",
    )
    parser.add_argument(
        "--output-file",
        help="Override sender.file_path (only used when the sender is 'file')",
    )
    return parser.parse_args(argv)


def setup_logging(cfg):
    handlers = [logging.StreamHandler()]
    if cfg.log_file:
        directory = os.path.dirname(cfg.log_file)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Runs forever via cron every 6h: rotate so the log can't grow
        # unbounded (5MB x 3 backups).
        handlers.append(
            logging.handlers.RotatingFileHandler(
                cfg.log_file, maxBytes=5_000_000, backupCount=3
            )
        )
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )


def _align_down(when, align_seconds):
    """Floor ``when`` to the nearest multiple of ``align_seconds`` since UTC
    midnight (e.g. 21600 for 6h keeps boundaries at 00:00/06:00/12:00/18:00
    UTC regardless of scheduler jitter)."""
    if not align_seconds:
        return when
    midnight = when.replace(hour=0, minute=0, second=0, microsecond=0)
    elapsed = (when - midnight).total_seconds()
    return midnight + datetime.timedelta(seconds=elapsed - (elapsed % align_seconds))


def run(cfg, dry_run: bool) -> int:
    now = _align_down(
        datetime.datetime.now(datetime.timezone.utc), cfg.pointer.align_seconds
    )
    extract_to = now - datetime.timedelta(seconds=cfg.pointer.query_lag_seconds)
    extract_from = pointer_mod.read_pointer(
        cfg.pointer.file, cfg.pointer.initial_lookback_hours
    )

    if extract_from >= extract_to:
        LOG.info(
            "Nothing to do: pointer (%s) is not before query window end (%s)",
            extract_from,
            extract_to,
        )
        return 0

    LOG.info(
        "Extracting container energy accounting for [%s -> %s]",
        extract_from,
        extract_to,
    )

    client = mimir.MimirClient(
        cfg.mimir.endpoint,
        cfg.mimir.user,
        cfg.mimir.password,
        verify_ssl=cfg.mimir.verify_ssl,
    )
    metrics = collect_metrics(client, cfg.mimir, extract_from, extract_to)
    records, open_allocations = build_records(
        metrics, cfg.accounting, extract_to, cfg.mimir.step_seconds
    )

    previously_open = state_mod.read_open_allocations(cfg.pointer.open_allocations_file)
    closing_records = build_closing_records(previously_open, metrics, cfg.accounting)
    records = records + closing_records

    LOG.info(
        "Built %d energy record(s) (%d closing) from %d allocation(s) with data",
        len(records),
        len(closing_records),
        len(metrics),
    )

    if dry_run:
        LOG.info("Dry run: not sending records or advancing the pointer/state")
        for r in records:
            LOG.info(r.cim_message())
        return 0

    if records:
        if cfg.sender.type == "file":
            file_client.push_records(cfg.sender.file_path, records)
        else:
            cim_client.push_records(
                cfg.cim.base_url,
                cfg.cim.email,
                cfg.cim.password,
                records,
                verify_ssl=cfg.cim.verify_ssl,
            )
    else:
        LOG.info("No records to send")

    pointer_mod.write_pointer(cfg.pointer.file, extract_to)
    state_mod.write_open_allocations(
        cfg.pointer.open_allocations_file, open_allocations
    )
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    cfg = config_mod.load_config(args.config, sender_type_override=args.sender)
    setup_logging(cfg)

    if args.output_file:
        cfg.sender.file_path = args.output_file

    try:
        return run(cfg, dry_run=cfg.dry_run or args.dry_run)
    except Exception:
        LOG.exception("Run failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
