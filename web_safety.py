"""Public-web fetches with SSRF and redirect validation."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import requests


def validate_public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("only public HTTP(S) URLs without embedded credentials are allowed")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("URL hostname could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(address.split("%", 1)[0]).is_global for address in addresses):
        raise ValueError("URL resolves to a private, local, or reserved address")
    return url


def safe_get(url, *, headers=None, params=None, timeout=15, max_redirects=5):
    current = validate_public_url(url)
    for _ in range(max_redirects + 1):
        response = requests.get(current, headers=headers, params=params, timeout=timeout, allow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        target = response.headers.get("Location")
        if not target:
            return response
        current = validate_public_url(urljoin(current, target))
        params = None
    raise ValueError("too many redirects")
