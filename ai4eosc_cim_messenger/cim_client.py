"""Client to authenticate against and publish records to GreenDIGIT CIM.

Mirrors the request/response flow of cASO's ``caso.messenger.greendigit_cim``
messenger: a bearer token is obtained from ``/gd-cim-api/v1/token`` using
email/password, then records are POSTed as a JSON list to
``/gd-cim-api/v1/submit``.
"""

import logging
import typing

import requests

from ai4eosc_cim_messenger import record as record_mod

LOG = logging.getLogger(__name__)


class CIMError(RuntimeError):
    """Raised when the CIM service cannot be authenticated against."""


def get_token(
    base_url: str, email: str, password: str, verify_ssl: bool = True, timeout: int = 60
) -> str:
    """Obtain a Bearer token from the GreenDIGIT CIM Service."""
    token_url = f"{base_url.rstrip('/')}/gd-cim-api/v1/token"
    resp = requests.get(
        token_url,
        params={"email": email, "password": password},
        verify=verify_ssl,
        timeout=timeout,
    )
    resp.raise_for_status()

    data = resp.json()
    token = data.get("access_token") or data.get("token")
    if not token:
        raise CIMError("Token not found in GreenDIGIT CIM response")
    return token


def push_records(
    base_url: str,
    email: str,
    password: str,
    records: typing.Sequence["record_mod.EnergyRecord"],
    verify_ssl: bool = True,
    timeout: int = 60,
) -> None:
    """Send a list of EnergyRecord objects to the GreenDIGIT CIM Service."""
    if not records:
        return

    publish_url = f"{base_url.rstrip('/')}/gd-cim-api/v1/submit"
    token = get_token(base_url, email, password, verify_ssl=verify_ssl, timeout=timeout)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    payload = [r.cim_message() for r in records]

    resp = requests.post(
        publish_url, headers=headers, json=payload, verify=verify_ssl, timeout=timeout
    )
    resp.raise_for_status()

    LOG.info(
        "Published %d record(s) to GreenDIGIT CIM (%s), status=%s",
        len(payload),
        publish_url,
        resp.status_code,
    )
