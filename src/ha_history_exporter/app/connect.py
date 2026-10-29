"""Opening the Home Assistant source a configuration describes."""

from __future__ import annotations

from ..settings import ResolvedSettings
from ..source.rest import RestHistorySource
from ..source.retry import RetryPolicy


def open_source(settings: ResolvedSettings) -> RestHistorySource:
    """The REST source; an error that says how to configure a missing URL or token."""
    connection = settings.connection()
    requests = settings.config.requests
    return RestHistorySource(
        connection.url,
        connection.token,
        ca_bundle=connection.ca_bundle,
        timeout=requests.timeout,
        retry=RetryPolicy(requests.max_retries, requests.backoff),
    )
