"""FORCE_IPV4: resolve hostnames to IPv4 addresses only, process-wide.

On a network whose IPv6 route is broken, a connection that lands on a
host's IPv6 address dies in the TLS handshake (ssl.SSLEOFError:
UNEXPECTED_EOF_WHILE_READING) while IPv4 works. Every client here —
httplib2 (Gmail / Sheets / Pub/Sub), httpx (OpenRouter, WhatsApp) — resolves
through socket.getaddrinfo, so filtering its results covers them all.
"""

import logging
import socket

logger = logging.getLogger(__name__)

_original_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, *args, **kwargs):
    if family in (0, socket.AF_UNSPEC):
        results = _original_getaddrinfo(host, port, socket.AF_INET, *args, **kwargs)
        if results:
            return results
    return _original_getaddrinfo(host, port, family, *args, **kwargs)


def apply(force_ipv4: bool) -> None:
    if force_ipv4 and socket.getaddrinfo is not _ipv4_getaddrinfo:
        socket.getaddrinfo = _ipv4_getaddrinfo
        logger.info("FORCE_IPV4 on — outgoing connections use IPv4 only")
