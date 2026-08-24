<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="images/AI4-horizontal-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="images/AI4-horizontal-light.svg">
    <img alt="AI4EOSC" src="images/AI4-horizontal-light.svg" width="500">
  </picture>
</p>

# ai4eosc-energy-accounting

Sends AI4EOSC/iMagine PaaS container energy accounting records to GreenDIGIT Environmental Impact Metric Publication System (EIMPS), throught the Common Information Model (CIM) API. Designed to run every 6h via cron. It reports containers the same way [cASO](https://github.com/IFCA-Advanced-Computing/caso) reports VMs to CIM, but as Cloud **PaaS** Execution Units instead of Cloud IaaS Execution Units:

This tool never talks to Nomad (the orchestrator behind the AI4EOSC/iMagine platform) directly: it only reads the container power metrics already collected in Mimir/Prometheus and exports them. This is a deliberate boundary, not an oversight, but it is also the root of the limitations called out below, since without querying Nomad itself there's no independent CPU-usage source per allocation (see `CpuDuration_s` in step 4) and no allocation-lifecycle event to know exactly when a container stopped (hence the sample-gap heuristic in step 4/6 and the *Known limitation* section).

|                       | VMs (cASO)               | Containers (this tool)                                                                                                                                                                                      |
| --------------------- | ------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `CloudType`           | `caso/5.2.0 (OpenStack)` | `container (PaaS)`                                                                                                                                                                                          |
| `SiteName`            | e.g. `IFCA-LCG2`         | `IFCA-LCG2` (fixed for now)                                                                                                                                                                                 |
| `CloudComputeService` | e.g. `IFCA-LCG2`         | `AI4EOSC` (fixed) — iMagine is a deployment of the AI4EOSC platform, not a separate compute service                                                                                                         |
| `Owner`               | the VM's DN/VO           | `ai4eosc.eu` or `imagine-ai.eu`, derived per-record from the Prometheus `datacenter` label (`ifca-ai4eosc` / `ifca-imagine`) via `accounting.owner_map` — this is what distinguishes the iMagine deployment |

## What it does, each run

1. Reads the pointer file (last successfully-sent instant). On the very
   first run, defaults to `now - initial_lookback_hours`.
2. Queries Mimir (`GET {endpoint}/api/v1/query_range`) with basic auth for:
    ```
    sum by(container_label_com_hashicorp_nomad_alloc_id, datacenter) (scaph_process_power_consumption_microwatts)
    ```
    over `[pointer, now - query_lag_seconds]`, at `step_seconds` resolution
    (chunked if the window is large, to stay under Mimir's per-query point
    limit). `datacenter` is kept in the `sum by(...)` so each series stays
    tagged with the platform it belongs to (`ifca-ai4eosc` / `ifca-imagine`).
3. For each allocation, converts its summed microwatt samples into Wh
   (treating each sample as constant power for one `step_seconds`
   interval), and records the timestamp of its first and last sample in
   the window. Optionally, if `mimir.gpu_query` is configured, also queries
   GPU power (`DCGM_FI_DEV_POWER_USAGE`, in Watts, joined against
   `nomad_gpu_allocation_info` to attribute each GPU to a Nomad
   `alloc_id`), converts it to Wh the same way, and adds it into the same
   allocation's total _before_ the CPU normalization factor is applied
   below (the factor already accounts for both CPU and GPU per cASO's
   benchmark methodology). Disabled by default — not every deployment
   scrapes DCGM / the GPU allocation mapper exporter; see
   `config.example.yaml`.
4. Builds one `EnergyRecord` per Nomad allocation:
    - `ExecUnitID` = the allocation ID (already a UUID).
    - `StartExecTime`/`EndExecTime` = the allocation's first/last sample
      timestamps. If it ran throughout the window these are ~the window
      bounds; if it started or was removed mid-window, they're bounded to
      its actual lifespan within the window.
    - `ExecUnitFinished` = `1` (and `Status` = `"completed"`) when the last
      sample falls short of the window end by more than ~1.5 query steps
      (i.e. metrics stopped before the window closed, so the container is
      gone); otherwise `0` / `"running"`.
    - `CloudComputeService` = `accounting.compute_service` (fixed to
      `"AI4EOSC"` — see table above). `Owner` looked up from the series'
      `datacenter` label via `accounting.owner_map` (falls back to the raw
      label value, with a warning, if it's not in the map).
    - `Efficiency` = `1.0`, `SuspendDuration_s` = `0`,
      `CPUNormalizationFactor` = `2.03` (all fixed).
    - `CpuDuration_s` = `WallClockTime_s` (no independent CPU-usage source
      for containers, so a single CPU-equivalent is assumed) and `Work` is
      computed the same way as cASO: `CpuDuration_s / EnergyWh`.
5. Sends the batch via `sender.type`:
    - `"cim"` (default): bearer-token authenticated POST to CIM
      (`/gd-cim-api/v1/token` then `/gd-cim-api/v1/submit`, identical flow
      to cASO's `greendigit_cim` messenger).
    - `"file"`: appends the same JSON payload to `sender.file_path`
      instead, for testing/inspecting what would be sent without touching
      CIM.

   Both this and the Mimir query in step 2 go through a shared
   `requests.Session` (`ai4eosc_energy_accounting/http.py`) that retries
   transient failures (connection errors, `429`/`5xx`) with backoff, so a
   brief network blip mid-run doesn't fail the whole run and wait for the
   next cron cycle.
6. Before sending, checks which allocations the _previous_ run reported as
   still running (`var/open_allocations.json`) but that produced zero
   samples in this run's window at all — meaning they stopped sometime
   before this window even started, without ever landing in a window that
   could see the gap before its end. For each, synthesizes a zero-energy
   closing record (`ExecUnitFinished` = `1`, `EnergyWh` = `0`, both times
   set to its last known sample) so it isn't left "running" forever. See
   _Known limitation_ below.
7. Only on success, advances the pointer file and the open-allocations
   state to the window's end. A failed run leaves both untouched, so the
   same window is retried next time instead of losing data. A run with no
   matching data still advances the pointer (nothing to retry).

## Known limitation

`ExecUnitFinished`/`Status` for a normal (non-closing) record are decided
from that record's own window (step 4). If an allocation's very last
sample lands within the ~1.5-step tolerance of a window's end, that
window reports it as `"running"` (correctly, from what it can see) and
remembers it as open; the _next_ run then closes it via step 6 once it
sees zero samples for it. The one case this still can't fully rule out:
if that next window instead has a real scrape/collection gap (Mimir
outage, etc.) and the allocation was actually still running the whole
time, it gets closed anyway, and would need a brand new record if/when it
resumes. This is far rarer than the original gap (needs both the
boundary coincidence _and_ a full-window data outage) and never affects
energy totals — only that allocation's terminal status around the outage.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config.example.yaml config.yaml
# fill in mimir/cim credentials, then:
chmod 600 config.yaml
```

Everything the script reads or writes besides `config.yaml` — the pointer
file, the open-allocations state, the `sender.type: file` test output, the
rotating log — lives under `var/` inside the project directory by default
(created automatically), regardless of the working directory it's run
from. `--config` itself defaults to `<project dir>/config.yaml`.

Try it without sending anything:

```bash
.venv/bin/python -m ai4eosc_energy_accounting.main --config config.yaml --dry-run
```

See what a real run would send, without touching CIM, by writing to a file instead:

```bash
.venv/bin/python -m ai4eosc_energy_accounting.main --config config.yaml --sender file --output-file var/records.jsonl
cat var/records.jsonl
```

(This still advances the pointer, same as a normal run — only the destination changes.
Use `--dry-run` instead if you don't want the pointer to move at all.)

Then install the cron job (see `ai4eosc-energy-accounting.cron` for a ready-to-edit line —
just point the `cd` at wherever you cloned this):

```bash
crontab -e
```

## Testing

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest tests/
```

`tests/test_e2e.py` runs `main()` against local Mimir/CIM stand-ins
(`tests/conftest.py`) and checks the accounting rules above: lifespan
bounding, finished detection (including the two-run "vanished right at
the window edge" case from _Known limitation_), the `datacenter` → `Owner`
mapping (including the unmapped fallback), the fixed fields (including
`CloudComputeService`), both senders, and that the pointer/state only
advance on success.

## Config

See `config.example.yaml`. Secrets (`mimir.password`, `cim.password`, etc.)
can be omitted from the file and set instead via `MIMIR_USER`,
`MIMIR_PASSWORD`, `CIM_EMAIL`, `CIM_PASSWORD` environment variables — handy
if the cron job is run through a secrets-injecting wrapper. CIM credentials
are only required when `sender.type` is `"cim"`.

## Funding and Acknowledgments

This work was developed within the [GreenDIGIT](https://greendigit-project.eu/) project, funded by the European Union's Horizon Europe research and innovation programme under grant agreement No. [101131207](https://cordis.europa.eu/project/id/101131207), and by the Swiss State Secretariat for Education, Research and Innovation (SERI).

<img src="https://github.com/wattnet/.github/raw/main/images/GreenDIGIT logo color horizontal2.png" alt="GreenDIGIT Logo" width="230" align="right"/>
<img src="https://github.com/wattnet/.github/raw/main/images/EN_FundedbytheEU_RGB_POS.png" alt="EU Funded Logo" width="260" align="left"/>
<img src="https://github.com/wattnet/.github/raw/main/images/Flag_of_Switzerland.svg" alt="Swiss State Secretariat for Education, Research and Innovation (SERI)" height="50" align="left"/>
<br clear="all"/>

##### © 2026 Spanish National Research Council (CSIC). All rights reserved.
