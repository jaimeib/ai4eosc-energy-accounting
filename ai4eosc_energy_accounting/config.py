"""Configuration loading for the AI4EOSC CIM messenger.

Settings live in a YAML file (see ``config.example.yaml``). Passwords may
be left out of the file and provided instead via the ``MIMIR_PASSWORD`` /
``MIMIR_USER`` / ``CIM_EMAIL`` / ``CIM_PASSWORD`` environment variables,
which take precedence when set.
"""

import dataclasses
import os
import typing

import yaml

# Root of the project (the directory containing this package). Used so that
# default paths for the pointer file, sender output, etc. live inside the
# project directory regardless of the current working directory the script
# is invoked from (e.g. from cron).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def local_path(*parts: str) -> str:
    """Build a path inside the project's ``var/`` directory."""
    return os.path.join(PROJECT_ROOT, "var", *parts)


@dataclasses.dataclass
class MimirConfig:
    endpoint: str
    user: str
    password: str
    query: str = (
        "sum by(container_label_com_hashicorp_nomad_alloc_id, datacenter) "
        "(scaph_process_power_consumption_microwatts)"
    )
    alloc_id_label: str = "container_label_com_hashicorp_nomad_alloc_id"
    datacenter_label: str = "datacenter"
    # Optional: GPU power (DCGM_FI_DEV_POWER_USAGE, in Watts) joined against
    # nomad_gpu_allocation_info to attribute it to a Nomad alloc_id. Empty
    # string disables GPU accounting (not every deployment scrapes DCGM /
    # the alloc-mapper exporter). See config.example.yaml for the query.
    gpu_query: str = ""
    gpu_alloc_id_label: str = "alloc_id"
    step_seconds: int = 30
    verify_ssl: bool = True
    max_points_per_query: int = 11_000


@dataclasses.dataclass
class CIMConfig:
    base_url: str = "https://greendigit-cim.sztaki.hu"
    email: typing.Optional[str] = None
    password: typing.Optional[str] = None
    verify_ssl: bool = True


@dataclasses.dataclass
class AccountingConfig:
    # Fixed for now: all AI4EOSC/iMagine PaaS containers run at this site.
    site_name: str = "IFCA-LCG2"
    cloud_type: str = "container (PaaS)"
    # iMagine is a deployment of the AI4EOSC platform, not a separate one,
    # so CloudComputeService is fixed regardless of `datacenter`. The
    # `datacenter` label still distinguishes the deployment via Owner.
    compute_service: str = "AI4EOSC"
    # cASO's CPU-benchmark normalization factor, fixed for these containers.
    cpu_normalization_factor: float = 2.03
    # Maps the Prometheus `datacenter` label to the Owner reported to CIM
    # (the deployment's domain name).
    owner_map: typing.Dict[str, str] = dataclasses.field(
        default_factory=lambda: {
            "ifca-ai4eosc": "ai4eosc.eu",
            "ifca-imagine": "imagine-ai.eu",
        }
    )


@dataclasses.dataclass
class PointerConfig:
    file: str = local_path("lastrun")
    # Allocations reported as still running at the end of the last run, so
    # a later run whose window has zero samples for one of them can send a
    # closing record instead of leaving it "running" forever. See state.py.
    open_allocations_file: str = local_path("open_allocations.json")
    initial_lookback_hours: int = 6
    query_lag_seconds: int = 60
    # If set, the window end ("now") is floored to the nearest multiple of
    # this many seconds since UTC midnight before subtracting
    # query_lag_seconds -- so a run a few seconds late (cron/scheduler
    # jitter) still closes its window at the intended boundary (e.g. 21600
    # for 6h keeps windows ending at 00:00/06:00/12:00/18:00 UTC regardless
    # of exactly when the process started). 0 disables alignment.
    align_seconds: int = 0


@dataclasses.dataclass
class SenderConfig:
    # "cim" pushes to the GreenDIGIT CIM service; "file" appends the same
    # payload to a local file instead, for testing/inspection.
    type: str = "cim"
    file_path: str = local_path("records.jsonl")


@dataclasses.dataclass
class DataQualityConfig:
    # Per-series correction of out-of-magnitude power samples before they are
    # folded into the Wh sum: a sample more than `outlier_factor` times the
    # local level (the median of the `window` samples) on *both* sides, or a
    # NaN/Inf, is treated as a bad measurement and overwritten from its
    # nearest trustworthy neighbours (see data_quality.py). Requiring both
    # sides leaves a genuine sustained ramp (microwatts -> milliwatts ->
    # watts) alone. Only high spikes are corrected; a drop toward zero is left
    # alone (legitimate idle period).
    enabled: bool = True
    outlier_factor: float = 10.0
    # Number of samples on each side used to establish the local level a
    # sample is compared against.
    window: int = 5
    # Series shorter than this are left untouched (too little context to tell a
    # spike from a genuine level).
    min_samples: int = 5


@dataclasses.dataclass
class Config:
    mimir: MimirConfig
    cim: CIMConfig
    accounting: AccountingConfig
    pointer: PointerConfig
    sender: SenderConfig
    data_quality: DataQualityConfig = dataclasses.field(
        default_factory=DataQualityConfig
    )
    dry_run: bool = False
    log_level: str = "INFO"
    log_file: typing.Optional[str] = local_path("ai4eosc-energy-accounting.log")


def _env_override(value: typing.Optional[str], env_var: str) -> typing.Optional[str]:
    return os.environ.get(env_var, value)


def load_config(
    path: str, sender_type_override: typing.Optional[str] = None
) -> Config:
    """Load and validate configuration from a YAML file.

    :param sender_type_override: if set, overrides ``sender.type`` before
        validating required fields (e.g. a CLI ``--sender file`` flag
        should not require CIM credentials to be present).
    """
    with open(path, "r") as fd:
        raw = yaml.safe_load(fd) or {}

    mimir_raw = raw.get("mimir", {})
    cim_raw = raw.get("cim", {})
    accounting_raw = raw.get("accounting", {})
    pointer_raw = raw.get("pointer", {})
    sender_raw = raw.get("sender", {})
    data_quality_raw = raw.get("data_quality", {})

    mimir = MimirConfig(
        endpoint=mimir_raw["endpoint"],
        user=_env_override(mimir_raw.get("user"), "MIMIR_USER"),
        password=_env_override(mimir_raw.get("password"), "MIMIR_PASSWORD"),
        query=mimir_raw.get("query", MimirConfig.query),
        alloc_id_label=mimir_raw.get("alloc_id_label", MimirConfig.alloc_id_label),
        datacenter_label=mimir_raw.get("datacenter_label", MimirConfig.datacenter_label),
        gpu_query=mimir_raw.get("gpu_query", MimirConfig.gpu_query),
        gpu_alloc_id_label=mimir_raw.get(
            "gpu_alloc_id_label", MimirConfig.gpu_alloc_id_label
        ),
        step_seconds=int(mimir_raw.get("step_seconds", 30)),
        verify_ssl=bool(mimir_raw.get("verify_ssl", True)),
        max_points_per_query=int(mimir_raw.get("max_points_per_query", 11_000)),
    )

    cim = CIMConfig(
        base_url=cim_raw.get("base_url", CIMConfig.base_url),
        email=_env_override(cim_raw.get("email"), "CIM_EMAIL"),
        password=_env_override(cim_raw.get("password"), "CIM_PASSWORD"),
        verify_ssl=bool(cim_raw.get("verify_ssl", True)),
    )

    default_accounting = AccountingConfig()
    accounting = AccountingConfig(
        site_name=accounting_raw.get("site_name", default_accounting.site_name),
        cloud_type=accounting_raw.get("cloud_type", default_accounting.cloud_type),
        compute_service=accounting_raw.get(
            "compute_service", default_accounting.compute_service
        ),
        cpu_normalization_factor=float(
            accounting_raw.get(
                "cpu_normalization_factor", default_accounting.cpu_normalization_factor
            )
        ),
        owner_map=accounting_raw.get("owner_map", default_accounting.owner_map),
    )

    pointer = PointerConfig(
        file=pointer_raw.get("file", PointerConfig.file),
        open_allocations_file=pointer_raw.get(
            "open_allocations_file", PointerConfig.open_allocations_file
        ),
        initial_lookback_hours=int(pointer_raw.get("initial_lookback_hours", 6)),
        query_lag_seconds=int(pointer_raw.get("query_lag_seconds", 60)),
        align_seconds=int(pointer_raw.get("align_seconds", 0)),
    )

    sender = SenderConfig(
        type=sender_type_override or sender_raw.get("type", SenderConfig.type),
        file_path=sender_raw.get("file_path", SenderConfig.file_path),
    )

    data_quality = DataQualityConfig(
        enabled=bool(data_quality_raw.get("enabled", DataQualityConfig.enabled)),
        outlier_factor=float(
            data_quality_raw.get("outlier_factor", DataQualityConfig.outlier_factor)
        ),
        window=int(data_quality_raw.get("window", DataQualityConfig.window)),
        min_samples=int(
            data_quality_raw.get("min_samples", DataQualityConfig.min_samples)
        ),
    )

    config = Config(
        mimir=mimir,
        cim=cim,
        accounting=accounting,
        pointer=pointer,
        sender=sender,
        data_quality=data_quality,
        dry_run=bool(raw.get("dry_run", False)),
        log_level=raw.get("log_level", "INFO"),
        log_file=raw.get("log_file", Config.log_file),
    )

    missing = [
        name
        for name, value in (
            ("mimir.user", mimir.user),
            ("mimir.password", mimir.password),
        )
        if not value
    ]
    if sender.type == "cim":
        missing += [
            name
            for name, value in (
                ("cim.email", cim.email),
                ("cim.password", cim.password),
            )
            if not value
        ]
    if missing:
        raise ValueError(f"Missing required configuration values: {', '.join(missing)}")

    return config
