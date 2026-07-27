"""Home Assistant REST API client with retry / exponential backoff.

Security notes:
  - The token is passed only in the Authorization header.
  - It is never logged, stored in files, or included in error messages.
  - Auth errors (401, 403) cause immediate abort — no retry.
  - The HA_URL can be any reachable URL: local mDNS, IP address,
    Tailscale address, or any other remote address the user configures.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, List

import requests

from .exceptions import AuthError, HAAPIError, HAConnectionError

logger = logging.getLogger(__name__)

# HTTP status codes that warrant a retry with backoff.
_RETRYABLE_STATUS = {500, 502, 503, 504}


class HomeAssistantClient:
    """Thin wrapper around the HA REST API.

    The base URL is read from the HA_URL environment variable and can be:
      http://homeassistant.local:8123   (mDNS — local network only)
      http://192.0.2.10:8123          (IP address)
      http://100.x.x.x:8123             (Tailscale)
      https://myhome.duckdns.org:8123   (external HTTPS)
    """

    def __init__(
        self,
        url: str,
        token: str,
        timeout: int = 120,
        max_retries: int = 3,
        backoff_seconds: List[int] | None = None,
    ) -> None:
        self._base = url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff = backoff_seconds or [2, 5, 15]
        self._session = requests.Session()
        # Token only in the header, never in URLs or logs.
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }
        )
        # Statistics for the manifest.
        self.total_requests: int = 0
        self.total_retries: int = 0

    # ── internal ──────────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        """GET with retry/backoff.  Raises on fatal errors."""
        url = f"{self._base}{path}"
        last_exc: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                resp = self._session.get(url, params=params, timeout=self._timeout)
                self.total_requests += 1

                if resp.status_code in (401, 403):
                    raise AuthError(
                        f"HTTP {resp.status_code} — token is missing, wrong, or "
                        "lacks access.  Check HA_TOKEN (revoke and recreate if needed)."
                    )

                if resp.status_code in _RETRYABLE_STATUS:
                    logger.warning(
                        "HTTP %s from %s (attempt %d/%d) — will retry.",
                        resp.status_code,
                        path,
                        attempt + 1,
                        self._max_retries + 1,
                    )
                    last_exc = HAAPIError(f"HTTP {resp.status_code}")
                    if attempt < self._max_retries:
                        self.total_retries += 1
                        time.sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
                    continue

                if not resp.ok:
                    raise HAAPIError(
                        f"HTTP {resp.status_code} from {path}: {resp.text[:300]}"
                    )

                return resp

            except AuthError:
                raise
            except (requests.Timeout, requests.ConnectionError) as exc:
                logger.warning(
                    "Network error on %s (attempt %d/%d): %s",
                    path,
                    attempt + 1,
                    self._max_retries + 1,
                    type(exc).__name__,
                )
                last_exc = exc
                if attempt < self._max_retries:
                    self.total_retries += 1
                    time.sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
            except HAAPIError:
                raise

        raise HAConnectionError(
            f"Failed to reach {path} after {self._max_retries + 1} attempt(s). "
            f"Last error: {last_exc}"
        )

    # ── public API ────────────────────────────────────────────────────────────

    def check_api(self) -> None:
        """Verify the HA API is reachable and the token is valid."""
        logger.debug("GET /api/ — checking connectivity and token …")
        resp = self._get("/api/")
        data = resp.json()
        if data.get("message") != "API running.":
            raise HAAPIError(f"Unexpected /api/ response: {data}")
        logger.info("HA API is reachable at %s.", self._base)

    def get_states(self) -> List[dict]:
        """Return all current entity states from /api/states."""
        logger.debug("GET /api/states …")
        resp = self._get("/api/states")
        data = resp.json()
        if not isinstance(data, list):
            raise HAAPIError(
                f"Expected list from /api/states, got {type(data).__name__}"
            )
        logger.info("Received %d entity states from /api/states.", len(data))
        return data

    def get_history(
        self,
        start_dt: datetime,
        end_dt: datetime,
        entity_ids: List[str],
        minimal_response: bool = False,
        no_attributes: bool = False,
        significant_changes_only: bool = False,
    ) -> List[List[dict]]:
        """Fetch history for a batch of entities over a time range.

        The response is a list-of-lists:
            [ [state, ...], [state, ...], ... ]
        Entities with no data in the range simply do not appear — not an error.

        Long-Term-Statistics are never returned by this endpoint — only raw
        Recorder state changes are included.
        """
        start_str = start_dt.isoformat()
        params: dict[str, str] = {
            "end_time": end_dt.isoformat(),
            "filter_entity_id": ",".join(entity_ids),
        }
        if minimal_response:
            params["minimal_response"] = "true"
        if no_attributes:
            params["no_attributes"] = "true"
        if significant_changes_only:
            params["significant_changes_only"] = "true"

        resp = self._get(f"/api/history/period/{start_str}", params=params)
        data = resp.json()
        if not isinstance(data, list):
            raise HAAPIError(
                f"Expected list from history endpoint, got {type(data).__name__}"
            )
        return data  # type: ignore[return-value]

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "HomeAssistantClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
