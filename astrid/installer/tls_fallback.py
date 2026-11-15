"""Retry installer HTTPS opens once using native certificate verification.

Keep the default urllib path untouched until certificate verification fails.
Truststore is bundled because the bootstrap installs Astrid with --no-deps;
on Windows its CryptoAPI chain engine can retrieve missing roots. No global
SSL injection or dependency download is needed.
"""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request


def urlopen_with_tls_fallback(request: urllib.request.Request, *, timeout: float):
    """Open with the default verifier, then at most one native-store retry."""
    try:
        return urllib.request.urlopen(request, timeout=timeout)
    except ssl.SSLCertVerificationError:
        pass
    except urllib.error.URLError as error:
        if not isinstance(error.reason, ssl.SSLCertVerificationError):
            raise

    from ._tls_truststore import SSLContext

    context = SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # PROTOCOL_TLS_CLIENT requires both certificate and hostname verification.
    return urllib.request.urlopen(request, timeout=timeout, context=context)
