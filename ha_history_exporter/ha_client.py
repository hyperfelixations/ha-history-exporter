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
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Callable, List

import requests

from .errors import AuthError, HAAPIError, HAConnectionError, Remedy

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
        backoff_seconds: Sequence[float] | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base = url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff = tuple(backoff_seconds) if backoff_seconds else (2.0, 5.0, 15.0)
        # Injectable so retry timing is deterministic in tests.
        self._sleep = sleep
        self._session = session if session is not None else requests.Session()
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
                        "Authentication failed: Home Assistant rejected the "
                        f"access token (HTTP {resp.status_code}).",
                        details=(
                            "The token is missing, expired, was revoked, or "
                            "belongs to a user without access. HHE never "
                            "retries an authentication failure."
                        ),
                        remedies=(
                            Remedy(
                                "Create a fresh long-lived access token in Home "
                                "Assistant under Profile, Security, Long-Lived "
                                "Access Tokens, then set it:",
                                '$env:HA_TOKEN = "<your-token>"',
                            ),
                        ),
                        context={"endpoint": path, "status": str(resp.status_code)},
                    )

                if resp.status_code in _RETRYABLE_STATUS:
                    logger.warning(
                        "HTTP %s from %s (attempt %d/%d) - will retry.",
                        resp.status_code,
                        path,
                        attempt + 1,
                        self._max_retries + 1,
                    )
                    last_exc = HAAPIError(f"HTTP {resp.status_code}")
                    if attempt < self._max_retries:
                        self.total_retries += 1
                        self._sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
                    continue

                if not resp.ok:
                    # The response body may carry private content; only the
                    # status and the endpoint are safe to report or persist.
                    raise HAAPIError(
                        f"HTTP {resp.status_code} from {path}.",
                        details=(
                            "Home Assistant rejected the request. The response "
                            "body is deliberately not shown, logged, or stored."
                        ),
                        context={"endpoint": path, "status": str(resp.status_code)},
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
                    self._sleep(self._backoff[min(attempt, len(self._backoff) - 1)])
            except HAAPIError:
                raise

        raise HAConnectionError(
            f"Cannot reach Home Assistant at {self._base}.",
            details=(
                f"{path} did not answer after "
                f"{self._max_retries + 1} attempt(s): "
                f"{type(last_exc).__name__}. Home Assistant may be down, the "
                "address may be wrong, or this machine may have no route to it."
            ),
            remedies=(
                Remedy(
                    "Check that the configured address is reachable, for "
                    "example in a browser or with curl:",
                    f"curl -sS {self._base}/api/",
                ),
                Remedy(
                    "Raise the timeout or the retry count for one run:",
                    "hhe export --timeout 300 --max-retries 5 "
                    "--date yesterday",
                ),
            ),
            context={"url": self._base, "endpoint": path},
        )

    @staticmethod
    def _decode_json(resp: requests.Response, endpoint: str) -> Any:
        """Decode a response without exposing its body in parse errors."""
        try:
            return resp.json()
        except ValueError as exc:
            raise HAAPIError(f"Invalid JSON from {endpoint}") from exc

    # ── public API ────────────────────────────────────────────────────────────

    def check_api(self) -> None:
        """Verify the HA API is reachable and the token is valid."""
        logger.debug("GET /api/ - checking connectivity and token ...")
        resp = self._get("/api/")
        data = self._decode_json(resp, "/api/")
        if not isinstance(data, dict):
            raise HAAPIError(
                f"Expected object from /api/, got {type(data).__name__}"
            )
        if data.get("message") != "API running.":
            raise HAAPIError(f"Unexpected /api/ response: {data}")
        logger.info("HA API is reachable at %s.", self._base)

    def get_states(self) -> List[dict]:
        """Return all current entity states from /api/states."""
        logger.debug("GET /api/states ...")
        resp = self._get("/api/states")
        data = self._decode_json(resp, "/api/states")
        if not isinstance(data, list):
            raise HAAPIError(
                f"Expected list from /api/states, got {type(data).__name__}"
            )
        if any(not isinstance(state, dict) for state in data):
            raise HAAPIError("Expected state objects from /api/states")
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
        endpoint = "/api/history/period"
        data = self._decode_json(resp, endpoint)
        if not isinstance(data, list):
            raise HAAPIError(
                f"Expected list from history endpoint, got {type(data).__name__}"
            )
        if any(not isinstance(history, list) for history in data):
            raise HAAPIError("Expected entity history lists from history endpoint")
        if any(
            not isinstance(state, dict)
            for history in data
            for state in history
        ):
            raise HAAPIError("Expected state objects in history endpoint response")
        return data

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> HomeAssistantClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
