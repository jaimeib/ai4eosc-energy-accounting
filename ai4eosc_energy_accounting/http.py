"""Shared HTTP session with retry/backoff for transient failures.

Both the Mimir and CIM clients call external services that can blip
mid-run (dropped connection, a 502 during a deploy, rate limiting) --
without this, one such blip failed the whole run and left it to the next
cron cycle (up to 6h later) to catch up, instead of a few seconds of
retry within the same run.
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

RETRY_TOTAL = 3
RETRY_BACKOFF_FACTOR = 0.5  # ~0s, 0s, 1s between attempts
RETRY_STATUS_FORCELIST = (429, 500, 502, 503, 504)


def session_with_retries() -> requests.Session:
    """A requests.Session that retries transient network errors and the
    status codes above, with backoff, on both GET and POST -- POST is
    included on the assumption (matching cASO's greendigit_cim messenger)
    that resubmitting the same CIM payload is safe."""
    retry = Retry(
        total=RETRY_TOTAL,
        backoff_factor=RETRY_BACKOFF_FACTOR,
        status_forcelist=RETRY_STATUS_FORCELIST,
        allowed_methods=None,
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session
