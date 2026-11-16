"""Installer HTTPS opens using the shared, verifying OS trust policy.

The historical helper names are kept for their internal callers. There is no
fallback: every connection, including redirects, uses the same trust context
from the start. The shared factory works in the dependency-free bootstrap.
"""

from __future__ import annotations

import urllib.request

from astrid.core.util import http as http_util


def urlopen_with_tls_fallback(request: urllib.request.Request, *, timeout: float):
    """Open once with the same verifying policy as all external HTTP clients."""
    return urllib.request.urlopen(
        request, timeout=timeout, context=http_util.os_trust_ssl_context()
    )


def open_with_tls_fallback(request: urllib.request.Request, *, timeout: float, handlers=()):
    """Open once with the shared trust policy and the caller's redirect policy."""
    opener = urllib.request.build_opener(
        *handlers, urllib.request.HTTPSHandler(context=http_util.os_trust_ssl_context())
    )
    return opener.open(request, timeout=timeout)
