"""Plain HTTP is accepted everywhere and warned about outside the local network."""

from __future__ import annotations

import ipaddress

import pytest

from ha_history_exporter.source.transport import plaintext_warning

#: The ranges the warning treats as local, as their RFCs define them.
LOCAL_RANGES = (
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "fc00::/7",
    "fe80::/10",
)


def url(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"http://{host}:8123"


def edges(cidr: str) -> list[str]:
    network = ipaddress.ip_network(cidr)
    return [url(network.network_address + 1), url(network.broadcast_address - 1)]


def outside(cidr: str) -> list[str]:
    network = ipaddress.ip_network(cidr)
    return [url(network.network_address - 1), url(network.broadcast_address + 1)]


@pytest.mark.parametrize(
    "target",
    [
        *(address for cidr in LOCAL_RANGES for address in edges(cidr)),
        "http://[::1]:8123",
        "http://localhost:8123",
        "http://homeassistant:8123",
        "http://homeassistant.local:8123",
        "http://HomeAssistant.Local.:8123",
        "http://ha.home.arpa",
        "http://ha.internal",
        "http://ha.localhost",
        "http://home-assistant.invalid:8123",
        "http://ha.test",
        "http://ha.tailnet-1234.ts.net",
        "https://ha.example.net",
        "http://",
    ],
)
def test_local_hosts_and_encrypted_urls_need_no_warning(target: str) -> None:
    assert plaintext_warning(target) is None


@pytest.mark.parametrize(
    "target",
    [
        *(address for cidr in ("100.64.0.0/10", "172.16.0.0/12") for address in outside(cidr)),
        "http://ha.example.net:8123",
        "HTTP://ha.example.net",
        "http://[2001:db8::1]:8123",
        "http://ts.net.example.com",
    ],
)
def test_plain_http_to_any_other_host_is_warned_about(target: str) -> None:
    warning = plaintext_warning(target)
    assert warning is not None
    assert "is reached over plain HTTP" in warning


def test_an_ipv4_address_inside_ipv6_counts_as_the_ipv4_address() -> None:
    private = ipaddress.ip_network("10.0.0.0/8").network_address + 1
    mapped = ipaddress.IPv6Address("::ffff:0:0") + int(private)
    assert plaintext_warning(url(mapped)) is None
