"""Reject URLs that could reach private or metadata addresses."""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.google.com",
}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain")


class SsrfError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedUrl:
    """URL pinned to a validated public IP for the TCP connection."""

    original: str
    pinned_url: str
    host_header: str
    sni_hostname: str | None
    ip: str


def assert_public_http_url(url: str) -> None:
    resolve_public_http_url(url)


def resolve_public_http_url(url: str) -> ResolvedUrl:
    parsed = urlsplit(url)
    if parsed.username or parsed.password:
        raise SsrfError("URL userinfo is not allowed")
    if parsed.scheme not in ("http", "https"):
        raise SsrfError(f"unsupported scheme: {parsed.scheme or '(empty)'}")
    host = parsed.hostname
    if not host:
        raise SsrfError("URL host is empty")
    host_l = host.lower().rstrip(".")
    if host_l in BLOCKED_HOSTS or host_l.endswith(BLOCKED_SUFFIXES):
        raise SsrfError(f"blocked host: {host}")
    if host_l.isdigit():
        raise SsrfError("numeric host is not allowed")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        literal = ipaddress.ip_address(host_l)
    except ValueError:
        literal = None

    if literal is not None:
        _assert_public_ip(literal)
        ip = str(literal)
    else:
        ip = _resolve_public_ip(host, port)

    pinned_netloc = _netloc_for_ip(ip, port, parsed.scheme)
    pinned_url = urlunsplit((parsed.scheme, pinned_netloc, parsed.path or "/", parsed.query, ""))
    host_header = _host_header(host_l, parsed.port, parsed.scheme)
    sni = None if literal is not None else host_l
    if parsed.scheme != "https":
        sni = None
    return ResolvedUrl(
        original=url,
        pinned_url=pinned_url,
        host_header=host_header,
        sni_hostname=sni,
        ip=ip,
    )


def _resolve_public_ip(host: str, port: int) -> str:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SsrfError(f"DNS lookup failed for {host}") from exc
    if not infos:
        raise SsrfError(f"DNS lookup failed for {host}")
    public_ips: list[str] = []
    seen: set[str] = set()
    for info in infos:
        addr = info[4][0]
        if addr in seen:
            continue
        seen.add(addr)
        try:
            ip = ipaddress.ip_address(addr.split("%", 1)[0])
        except ValueError as exc:
            raise SsrfError(f"unresolved address for {host}") from exc
        _assert_public_ip(ip)
        public_ips.append(str(ip))
    if not public_ips:
        raise SsrfError(f"DNS lookup failed for {host}")
    # Prefer IPv4 for broader compatibility when both are public.
    for candidate in public_ips:
        if ":" not in candidate:
            return candidate
    return public_ips[0]


def _netloc_for_ip(ip: str, port: int, scheme: str) -> str:
    default = 443 if scheme == "https" else 80
    host = f"[{ip}]" if ":" in ip else ip
    if port == default:
        return host
    return f"{host}:{port}"


def _host_header(hostname: str, port: int | None, scheme: str) -> str:
    if port is None:
        return hostname
    default = 443 if scheme == "https" else 80
    if port == default:
        return hostname
    return f"{hostname}:{port}"


def _assert_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"):
        raise SsrfError(f"blocked address: {ip}")
    if not ip.is_global:
        raise SsrfError(f"blocked address: {ip}")
