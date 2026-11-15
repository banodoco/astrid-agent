# Vendored truststore 0.10.4 (https://github.com/sethmlarson/truststore).
# Source: truststore-0.10.4-py3-none-any.whl from PyPI.
# SHA256: adaeaecf1cbb5f4de3b1959b42d41f6fab57b2b1666adb59e89cb0b53361d981
# The installer bootstrap uses --no-deps: keep this stdlib-only fallback bundled.
# Local changes: import ordering only, plus this provenance/license header.
# Use SSLContext directly; never inject this into the process-wide ssl module.
#
# The MIT License (MIT)
#
# Copyright (c) 2022 Seth Michael Larson
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""Verify certificates using native system trust stores"""

import sys as _sys

if _sys.version_info < (3, 10):
    raise ImportError("truststore requires Python 3.10 or later")

# Detect Python runtimes which don't implement SSLObject.get_unverified_chain() API
# This API only became public in Python 3.13 but was available in CPython and PyPy since 3.10.
if _sys.version_info < (3, 13) and _sys.implementation.name not in ("cpython", "pypy"):
    try:
        import ssl as _ssl
    except ImportError:
        raise ImportError("truststore requires the 'ssl' module")
    else:
        _sslmem = _ssl.MemoryBIO()
        _sslobj = _ssl.create_default_context().wrap_bio(
            _sslmem,
            _sslmem,
        )
        try:
            while not hasattr(_sslobj, "get_unverified_chain"):
                _sslobj = _sslobj._sslobj  # type: ignore[attr-defined]
        except AttributeError:
            raise ImportError(
                "truststore requires peer certificate chain APIs to be available"
            ) from None

        del _ssl, _sslobj, _sslmem  # noqa: F821

from ._api import SSLContext, extract_from_ssl, inject_into_ssl  # noqa: E402

del _api, _sys  # type: ignore[name-defined] # noqa: F821

__all__ = ["SSLContext", "inject_into_ssl", "extract_from_ssl"]
__version__ = "0.10.4"
