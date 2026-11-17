"""One TLS trust policy, also shipped with the standalone Linux pod programs.

This leaf needs only the standard library on Linux. Native Windows/macOS trust
comes from the packaged verifier; certifi supplements the OS roots when present.
The installed Agent's public factory remains in ``core.util.http``.
"""

from __future__ import annotations

import os
import ssl
import sys

_USER_CA_ENV_VARS = ("SSL_CERT_FILE", "SSL_CERT_DIR")


def os_trust_ssl_context(platform: str | None = None) -> ssl.SSLContext:
    """Return a verifying TLS context that trusts what this computer trusts.

    Antivirus and company proxies re-sign HTTPS with a root installed in the
    OS store, which certifi (httpx's default bundle) does not contain.  On
    Windows and macOS this uses the vendored truststore (OS store); elsewhere
    it uses the stdlib default store. When installed, certifi supplements that
    store. The dependency-free installer can use the OS store without certifi.
    An explicit ``SSL_CERT_FILE`` or ``SSL_CERT_DIR`` uses the stdlib context
    unchanged so the user's choice wins. Verification is always on, and the
    global ``ssl`` module is never patched.
    """
    name = sys.platform if platform is None else platform
    user_chose_store = any(os.environ.get(var) for var in _USER_CA_ENV_VARS)
    if user_chose_store:
        return ssl.create_default_context()
    if name in ("win32", "darwin"):
        from astrid.installer._tls_truststore import SSLContext

        context = SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    else:
        context = ssl.create_default_context()
    try:
        import certifi
    except ModuleNotFoundError as exc:
        if exc.name != "certifi":
            raise
    else:
        context.load_verify_locations(cafile=certifi.where())
    return context
