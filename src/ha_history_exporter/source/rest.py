"""Home Assistant's REST API as a :class:`~.protocol.HistorySource`.

Classification of a failed attempt, see internal dev doc § "Home Assistant
source":

* HTTP 401/403: the token is rejected; never retried.
* TLS verification failure: never retried.
* lost or refused connections, timeouts, broken transfers, HTTP 429 and
  502-504: retried; when retries run out, Home Assistant is unavailable.
* HTTP 500: retried; when retries run out, the request failed.
* every other status, a redirect, an undecodable or unexpected body: the
  request failed; not retried.

The token travels in the ``Authorization`` header only, redirects are never
followed, and no message quotes a response body. JSON is decoded with the
standard library from the raw bytes.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Literal, Protocol, Self
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from .. import __version__
from ..domain.clock import Clock, SystemClock
from ..domain.query import HistoryQuery
from ..domain.window import DayWindow
from ..errors import (
    AuthError,
    HAAPIError,
    HAConnectionError,
    HHEError,
    Remedy,
    TLSError,
)
from .protocol import CallStats, HistoryCall, HistoryFetchError, InstanceConfig, StateObject
from .retry import RetryPolicy, parse_retry_after

logger = logging.getLogger(__name__)

USER_AGENT = f"ha-history-exporter/{__version__}"
CONNECT_TIMEOUT = 10.0
HISTORY_ENDPOINT = "/api/history/period"

_UNAVAILABLE_STATUS = frozenset({429, 502, 503, 504})
_RETRIED_FAILURE_STATUS = frozenset({500})
_TRANSIENT = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
    requests.exceptions.ContentDecodingError,
)


class HttpResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def content(self) -> bytes: ...


class HttpSession(Protocol):
    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str],
        timeout: tuple[float, float],
        verify: bool | str,
        allow_redirects: bool,
    ) -> HttpResponse: ...

    def close(self) -> None: ...


Kind = Literal["auth", "tls", "unavailable", "timeout", "failed"]


@dataclass(frozen=True)
class _Problem:
    kind: Kind
    reason: str
    retryable: bool
    status: int | None = None


class _Failed(Exception):
    def __init__(self, error: HHEError, stats: CallStats) -> None:
        super().__init__(error.summary)
        self.error = error
        self.stats = stats


class RestHistorySource:
    def __init__(
        self,
        url: str,
        token: str,
        *,
        ca_bundle: Path | None = None,
        timeout: float,
        retry: RetryPolicy,
        session: HttpSession | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Clock | None = None,
    ) -> None:
        self._base = url
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        self._verify: bool | str = str(ca_bundle) if ca_bundle is not None else True
        self._timeout = (min(CONNECT_TIMEOUT, timeout), timeout)
        self._retry = retry
        self._session: HttpSession = session if session is not None else requests.Session()
        self._sleep = sleep
        self._clock = clock or SystemClock()

    # ── the HistorySource protocol ───────────────────────────────────────────

    def check_api(self) -> None:
        data = self._json("/api/")
        if not isinstance(data, dict) or data.get("message") != "API running.":
            raise _unexpected("/api/")

    def instance_config(self) -> InstanceConfig:
        data = self._json("/api/config")
        if not isinstance(data, dict):
            raise _unexpected("/api/config")
        zone, version = data.get("time_zone"), data.get("version")
        if not isinstance(zone, str) or not isinstance(version, str):
            raise _unexpected("/api/config")
        try:
            time_zone = ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            raise HAAPIError(
                "Home Assistant reports a time zone this machine does not know.",
                details="Update the tzdata package, or set export.timezone explicitly.",
                context={"endpoint": "/api/config"},
            ) from None
        return InstanceConfig(time_zone=time_zone, version=version)

    def states(self) -> Sequence[StateObject]:
        data = self._json("/api/states")
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise _unexpected("/api/states")
        logger.info("Received %d entity states from /api/states.", len(data))
        return data

    def history(
        self, window: DayWindow, entity_ids: Sequence[str], query: HistoryQuery
    ) -> HistoryCall:
        path = f"{HISTORY_ENDPOINT}/{quote(window.query_start.isoformat(), safe=':')}"
        params = history_params(window, entity_ids, query)
        try:
            body, stats = self._get(HISTORY_ENDPOINT, path, params)
        except _Failed as failure:
            raise HistoryFetchError(failure.error, failure.stats) from None
        try:
            data = _decode(body, HISTORY_ENDPOINT)
        except HAAPIError as error:
            raise HistoryFetchError(error, stats) from None
        if not isinstance(data, list) or not all(
            isinstance(group, list) and all(isinstance(row, dict) for row in group)
            for group in data
        ):
            raise HistoryFetchError(_unexpected(HISTORY_ENDPOINT), stats)
        return HistoryCall(payload=data, stats=stats)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # ── transport ────────────────────────────────────────────────────────────

    def _json(self, endpoint: str) -> object:
        try:
            body, _ = self._get(endpoint, endpoint, None)
        except _Failed as failure:
            raise failure.error from None
        return _decode(body, endpoint)

    def _get(
        self, endpoint: str, path: str, params: Mapping[str, str] | None
    ) -> tuple[bytes, CallStats]:
        attempts = 0
        while True:
            attempts += 1
            retry_after: float | None = None
            try:
                response = self._session.get(
                    self._base + path,
                    params=params,
                    headers=self._headers,
                    timeout=self._timeout,
                    verify=self._verify,
                    allow_redirects=False,
                )
                status = response.status_code
                if 200 <= status < 300:
                    return response.content, CallStats(attempts)
                problem = _classify_status(status)
                retry_after = parse_retry_after(
                    response.headers.get("Retry-After"), self._clock.now()
                )
            except requests.exceptions.SSLError:
                problem = _Problem("tls", "the TLS certificate could not be verified", False)
            except requests.exceptions.Timeout:
                problem = _Problem("timeout", "the request timed out", True)
            except _TRANSIENT as exc:
                problem = _Problem("unavailable", _describe(exc), True)
            except requests.exceptions.RequestException as exc:
                problem = _Problem("failed", f"the request failed ({type(exc).__name__})", False)

            if not problem.retryable or attempts > self._retry.max_retries:
                raise _Failed(self._error(endpoint, problem, attempts), CallStats(attempts))
            delay = self._retry.delay(attempts - 1, retry_after)
            logger.warning(
                "%s: %s (attempt %d of %d); retrying in %.1f s.",
                endpoint,
                problem.reason,
                attempts,
                self._retry.max_retries + 1,
                delay,
            )
            self._sleep(delay)

    def _error(self, endpoint: str, problem: _Problem, attempts: int) -> HHEError:
        context = {"url": self._base, "endpoint": endpoint}
        if problem.status is not None:
            context["status"] = str(problem.status)
        tried = f"{attempts} attempt{'s' if attempts > 1 else ''}"
        match problem.kind:
            case "auth":
                return AuthError(
                    f"Home Assistant rejected the access token (HTTP {problem.status}).",
                    details=(
                        "The token is wrong, expired or revoked, or belongs to a user "
                        "without access. HHE never retries a rejected token."
                    ),
                    remedies=(
                        Remedy(
                            "Create a long-lived access token in Home Assistant under "
                            "Profile, Security, and store it (the input stays hidden):",
                            "hhe config set homeassistant.token",
                        ),
                    ),
                    context=context,
                )
            case "tls":
                return TLSError(
                    "The HTTPS certificate of Home Assistant could not be verified.",
                    details=(
                        "It is signed by an authority this machine does not trust, has "
                        "expired, or was issued for another host name. HHE does not "
                        "connect without verification."
                    ),
                    remedies=(
                        Remedy(
                            "Name the PEM file of the authority that signed the certificate:",
                            "hhe config set homeassistant.ca_bundle <path-to-ca.pem>",
                        ),
                    ),
                    context=context,
                )
            case "timeout":
                return HAConnectionError(
                    "Home Assistant did not answer in time.",
                    details=f"{endpoint}: {problem.reason} on every one of {tried}.",
                    remedies=(
                        Remedy(
                            "Give each request more time, or ask for fewer entities at once:",
                            "hhe export --timeout 300 --batch-size 5",
                        ),
                    ),
                    context=context,
                )
            case "unavailable":
                return HAConnectionError(
                    f"Home Assistant is not available at {self._base}.",
                    details=(
                        f"{endpoint}: {problem.reason} on every one of {tried}. Home "
                        "Assistant may be down, starting or overloaded, or this machine "
                        "has no route to it."
                    ),
                    remedies=(
                        Remedy(
                            "Check that the address answers, then run the export again:",
                            "hhe doctor",
                        ),
                    ),
                    context=context,
                )
            case "failed":
                details = "The response body is not shown."
                if problem.status == 404:
                    details = (
                        "The endpoint does not exist at this address; homeassistant.url "
                        "must be the base URL of Home Assistant."
                    )
                elif problem.status is not None and 300 <= problem.status < 400:
                    details = (
                        "HHE does not follow redirects, so the token never reaches "
                        "another address. Configure the address the redirect points to."
                    )
                return HAAPIError(
                    f"Home Assistant could not answer {endpoint}: {problem.reason}.",
                    details=details,
                    context=context,
                )


def _classify_status(status: int) -> _Problem:
    reason = f"HTTP {status}"
    if status in (401, 403):
        return _Problem("auth", reason, False, status)
    if status in _UNAVAILABLE_STATUS:
        return _Problem("unavailable", reason, True, status)
    if status in _RETRIED_FAILURE_STATUS:
        return _Problem("failed", reason, True, status)
    if 300 <= status < 400:
        return _Problem("failed", f"redirected with HTTP {status}", False, status)
    return _Problem("failed", reason, False, status)


def _describe(exc: requests.exceptions.RequestException) -> str:
    if isinstance(
        exc, requests.exceptions.ChunkedEncodingError | requests.exceptions.ContentDecodingError
    ):
        return "the response broke off"
    return "the connection failed"


def _decode(body: bytes, endpoint: str) -> object:
    try:
        data: object = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HAAPIError(
            f"{endpoint} answered with invalid JSON.",
            details="The response body is not shown.",
            context={"endpoint": endpoint},
        ) from None
    return data


def _unexpected(endpoint: str) -> HAAPIError:
    return HAAPIError(
        f"{endpoint} answered with an unexpected structure.",
        details="Is homeassistant.url the base URL of Home Assistant? The body is not shown.",
        context={"endpoint": endpoint},
    )


def history_params(
    window: DayWindow, entity_ids: Sequence[str], query: HistoryQuery
) -> dict[str, str]:
    """Query parameters of ``/api/history/period``.

    Home Assistant treats ``minimal_response``, ``no_attributes`` and
    ``skip_initial_state`` as set when present, and ``significant_changes_only``
    as set unless it is ``0``.
    """
    params = {
        "end_time": window.end_utc.isoformat(),
        "filter_entity_id": ",".join(entity_ids),
        "significant_changes_only": "1" if query.significant_changes_only else "0",
    }
    for flag in ("minimal_response", "no_attributes", "skip_initial_state"):
        if getattr(query, flag):
            params[flag] = "1"
    return params
