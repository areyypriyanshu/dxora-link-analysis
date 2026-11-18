"""URL validation with SSRF protection for user-supplied targets."""

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = {"http", "https"}
BLOCKED_HOSTNAMES = {"localhost", "metadata", "metadata.google.internal"}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


class UnsafeURLError(ValueError):
    """Raised when a URL is malformed or points at a non-public target."""


def is_public_ip(value: str) -> bool:
    """Return True only for globally routable addresses (strict allowlist).

    ``is_global`` rejects private, loopback, link-local, multicast, reserved,
    unspecified and CGNAT (100.64.0.0/10) ranges. IPv6 site-local addresses are
    rejected explicitly because Python's database reports them as global.
    """
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip.is_site_local:
            return False
    return ip.is_global


def _resolve(hostname: str, port: int) -> set[str]:
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"Hostname '{hostname}' could not be resolved") from exc
    return {info[4][0] for info in infos}


def validate_public_url(raw_url: str) -> str:
    """Return the URL unchanged if it is safe to fetch, otherwise raise UnsafeURLError.

    Blocks non-HTTP schemes, embedded credentials, local/internal hostnames,
    and hostnames that resolve to any non-globally-routable address. Called
    again on every redirect hop by the extractor.
    """
    url = raw_url.strip()
    if not url:
        raise UnsafeURLError("URL must not be empty")

    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError("URL must use http:// or https://")
    if parts.username or parts.password:
        raise UnsafeURLError("URLs with embedded credentials are not allowed")

    hostname = (parts.hostname or "").lower().rstrip(".")
    if not hostname:
        raise UnsafeURLError("URL must include a hostname")

    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeURLError("URL contains an invalid port") from exc

    if hostname in BLOCKED_HOSTNAMES or hostname.endswith(BLOCKED_SUFFIXES):
        raise UnsafeURLError("URL points to a local or internal hostname")

    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        if not is_public_ip(str(literal_ip)):
            raise UnsafeURLError("URL points to a private, loopback or reserved address")
        return url

    addresses = _resolve(hostname, port)
    if not addresses:
        raise UnsafeURLError(f"Hostname '{hostname}' did not resolve to any address")
    for address in addresses:
        if not is_public_ip(address):
            raise UnsafeURLError("URL resolves to a private or internal network address")
    return url
