"""Minimal client to run range queries against a Prometheus/Mimir endpoint."""

import datetime
import logging
import typing

import requests

LOG = logging.getLogger(__name__)


class MimirQueryError(RuntimeError):
    """Raised when Mimir returns a non-successful query result."""


class MimirClient:
    """Thin wrapper around Mimir's Prometheus-compatible HTTP API."""

    def __init__(
        self,
        endpoint: str,
        user: str,
        password: str,
        verify_ssl: bool = True,
        timeout: int = 60,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.auth = (user, password) if user else None
        self.verify_ssl = verify_ssl
        self.timeout = timeout

    def query_range(
        self,
        query: str,
        start: datetime.datetime,
        end: datetime.datetime,
        step_seconds: int,
    ) -> typing.List[dict]:
        """Run a query_range call and return the raw ``data.result`` list."""
        url = f"{self.endpoint}/api/v1/query_range"
        params = {
            "query": query,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": f"{step_seconds}s",
        }
        resp = requests.get(
            url,
            params=params,
            auth=self.auth,
            verify=self.verify_ssl,
            timeout=self.timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        if body.get("status") != "success":
            raise MimirQueryError(f"Mimir query failed: {body}")
        return body.get("data", {}).get("result", [])
