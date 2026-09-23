"""Public-web URL checks; the egress proxy also pins DNS at connection time."""

from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import quote, urlsplit, urlunsplit


class UnsafeURL(ValueError):
    """A URL cannot be accessed by the public-web reader."""


_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".home", ".lan", ".test", ".invalid")
_TRANSITION_NETWORKS = tuple(ipaddress.ip_network(value) for value in (
    "64:ff9b::/96", "64:ff9b:1::/48", "2001::/32", "2002::/16",
    "::ffff:0:0/96", "::/96",
))


def is_public_address(value: str) -> bool:
    """Reject special-use, mapped and transition addresses on Python 3.10+."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if not address.is_global or address.is_multicast or address.is_reserved:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped or address.sixtofour or address.teredo:
            return False
        if any(address in network for network in _TRANSITION_NETWORKS):
            return False
    # Python's special-use registry varies between supported runtime versions.
    if isinstance(address, ipaddress.IPv4Address):
        if address in ipaddress.ip_network("192.0.0.0/24"):
            return False
    return True


def validate_url_syntax(url: str) -> str:
    """Normalize a candidate URL without DNS or optional dependencies.

    This is an early check, not a connection-time SSRF defense. Hostnames must
    additionally pass ``resolve_public_url`` before any outbound connection.
    """
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise UnsafeURL("Supply an HTTP or HTTPS URL of at most 4096 characters.")
    if "\\" in url or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        raise UnsafeURL("URL contains disallowed whitespace or control characters.")
    try:
        parts = urlsplit(url)
        port = parts.port
        hostname = parts.hostname
    except (ValueError, UnicodeError):
        raise UnsafeURL("URL is malformed.") from None
    if parts.scheme.lower() not in {"http", "https"} or not hostname:
        raise UnsafeURL("Only absolute HTTP and HTTPS URLs are supported.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeURL("URLs containing credentials are not allowed.")
    if port not in {None, 80, 443}:
        raise UnsafeURL("Only public-web ports 80 and 443 are supported.")
    if "%" in hostname:
        raise UnsafeURL("Encoded hosts and IPv6 zone identifiers are not allowed.")
    hostname = hostname.rstrip(".").lower()
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        try:
            hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            raise UnsafeURL("URL hostname is malformed.") from None
        if ("." not in hostname or hostname.endswith(_LOCAL_SUFFIXES)
                or len(hostname) > 253 or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in hostname.split("."))):
            raise UnsafeURL("Local or malformed hostnames are not allowed.")
    else:
        if not is_public_address(str(address)):
            raise UnsafeURL("Private and special-use addresses are not allowed.")
        hostname = f"[{address.compressed}]" if address.version == 6 else str(address)
    authority = hostname + (f":{port}" if port is not None else "")
    try:
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="/%?:@!$&'()*+,;=-._~")
    except UnicodeError:
        raise UnsafeURL("URL contains invalid Unicode.") from None
    return urlunsplit((parts.scheme.lower(), authority, path, query, ""))


def resolve_public_url(url: str, resolver=None) -> tuple[str, list[tuple]]:
    """Resolve once, reject *every* non-public answer, return pinned sockaddrs.

    ``resolver`` has the signature of ``socket.getaddrinfo``. Consumers connect
    to the returned numeric socket address, never re-resolve the hostname.
    """
    normalized = validate_url_syntax(url)
    parts = urlsplit(normalized)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    resolver = resolver or socket.getaddrinfo
    try:
        records = resolver(parts.hostname, port, type=socket.SOCK_STREAM)
    except OSError:
        raise UnsafeURL("Public hostname could not be resolved.") from None
    if not records:
        raise UnsafeURL("Public hostname could not be resolved.")
    addresses = []
    for family, socktype, protocol, _canonical, sockaddr in records:
        if family not in {socket.AF_INET, socket.AF_INET6} or not is_public_address(sockaddr[0]):
            raise UnsafeURL("Hostname resolves to a private or special-use address.")
        record = (family, socktype, protocol, sockaddr)
        if record not in addresses:
            addresses.append(record)
    return normalized, addresses


def validate_public_url(url: str, resolver=None) -> str:
    """Validate all current DNS answers; use pinned results when connecting."""
    return resolve_public_url(url, resolver=resolver)[0]
