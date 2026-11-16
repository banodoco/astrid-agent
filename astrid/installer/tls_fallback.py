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


def open_with_tls_fallback(request: urllib.request.Request, *, timeout: float, handlers=()):
    """Like :func:`urlopen_with_tls_fallback`, for an opener with extra handlers.

    The default opener runs first; only a certificate failure retries once with
    the native-store context.  Use this where the caller needs a custom opener
    (for example, a redirect policy) that ``urlopen`` cannot express.
    """
    try:
        return urllib.request.build_opener(*handlers).open(request, timeout=timeout)
    except ssl.SSLCertVerificationError:
        pass
    except urllib.error.URLError as error:
        if not isinstance(error.reason, ssl.SSLCertVerificationError):
            raise

    from ._tls_truststore import SSLContext

    context = SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    opener = urllib.request.build_opener(
        *handlers, urllib.request.HTTPSHandler(context=context)
    )
    return opener.open(request, timeout=timeout)
