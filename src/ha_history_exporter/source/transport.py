"""Whether the access token would cross a public network unencrypted.

Plain HTTP is common and acceptable inside a home: loopback, the private and
link-local address ranges, the shared range 100.64.0.0/10 that Tailscale
uses, IPv6 unique-local addresses, single-label host names, the names
reserved for local networks and tests (``.local``, ``.home.arpa``,
``.internal``, ``.localhost``, ``.test``, ``.invalid``) and Tailscale's
``.ts.net``. Any other host gets a warning; HHE never refuses the URL. See
internal dev doc § "Home Assistant source".
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "127.0.0.0/8",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "::1/128",
        "fc00::/7",
        "fe80::/10",
    )
)
_PRIVATE_SUFFIXES = (
    ".local",
    ".home.arpa",
    ".internal",
    ".localhost",
    ".test",
    ".invalid",
    ".ts.net",
)


def _private_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or "." not in host or host.endswith(_PRIVATE_SUFFIXES)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in _PRIVATE_NETWORKS)


def plaintext_warning(url: str) -> str | None:
    """A warning for plain HTTP to a host outside the local network, else None."""
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".").lower()
    if parts.scheme.lower() != "http" or not host or _private_host(host):
        return None
    return (
        f"Home Assistant at {host} is reached over plain HTTP outside the local "
        "network, so the access token travels unencrypted. Use an https:// URL."
    )
