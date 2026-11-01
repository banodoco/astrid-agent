"""Mockable HTTP client with secret scrubbing and fal.ai helpers.

Uses stdlib ``urllib.request`` by default.  Swap the transport callable in
tests to avoid real HTTP calls.
"""

from __future__ import annotations

import json
import time
from base64 import b64encode
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from astrid.core.contracts.errors import AstridError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FAL_QUEUE_URL = "https://queue.fal.run"

# ---------------------------------------------------------------------------
# Transport type
# ---------------------------------------------------------------------------

# A transport receives a ``urllib.request.Request`` and returns
# ``(status: int, body: bytes)``.
Transport = Callable[[Request], tuple[int, bytes]]


# ---------------------------------------------------------------------------
# Default (real-http) transport
# ---------------------------------------------------------------------------

def _read_response_body(response: Any, *, max_bytes: int | None = None) -> bytes:
    """Read a response with an optional hard byte ceiling."""
    if max_bytes is None:
        return response.read()
    limit = int(max_bytes)
    if limit < 0:
        raise ValueError("max_bytes must be non-negative")
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > limit:
                raise AstridError(
                    f"HTTP response exceeds bounded body limit of {limit} bytes"
                )
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, limit - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise AstridError(
                f"HTTP response exceeds bounded body limit of {limit} bytes"
            )
    return b"".join(chunks)


def _bounded_body(body: bytes, *, max_bytes: int | None = None) -> bytes:
    """Apply the same ceiling to injected/mock transports."""
    if max_bytes is not None and len(body) > int(max_bytes):
        raise AstridError(
            f"HTTP response exceeds bounded body limit of {int(max_bytes)} bytes"
        )
    return body


def _validate_fal_queue_url(value: Any, *, field: str) -> str:
    """Accept only HTTPS queue URLs before attaching the FAL credential."""
    if not isinstance(value, str) or not value:
        raise AstridError(
            f"fal submission {field} must be a URL",
            recovery_command="retry; the fal submission response was malformed",
        )
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as exc:
        raise AstridError(
            f"fal submission {field} has an invalid port",
            recovery_command="retry; the fal submission response was malformed",
        ) from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname != "queue.fal.run"
        or port not in (None, 443)
        or not parsed.path.startswith("/")
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise AstridError(
            f"fal submission {field} is outside the authenticated queue origin",
            recovery_command="retry; the fal submission response was malformed",
        )
    return value


def _read_http_error_body(exc: HTTPError, *, max_bytes: int | None) -> str:
    """Read an HTTP error body under the same ceiling as successful bodies."""
    if not exc.fp:
        return ""
    try:
        body = _read_response_body(exc, max_bytes=max_bytes)
    except AstridError as bounded:
        return str(bounded)
    return body.decode("utf-8", errors="replace")


def _real_transport(request: Request) -> tuple[int, bytes]:
    """Execute *request* against the real network.

    Returns ``(status, body)``.  Raises ``URLError`` on connection failure.
    """
    with urlopen(request, timeout=_extract_timeout(request)) as response:
        return response.status, _read_response_body(
            response,
            max_bytes=getattr(request, "max_response_bytes", None),
        )


def _extract_timeout(request: Request) -> int:
    """Pull a timeout value from *request* extras, defaulting to 60."""
    return getattr(request, "timeout", 60) or 60


# ---------------------------------------------------------------------------
# HttpClient
# ---------------------------------------------------------------------------

class HttpClient:
    """Mockable HTTP client with built-in secret scrubbing.

    Parameters:
        transport: A callable ``(Request) -> (status, bytes)``.  Inject a
            mock in tests; leave ``None`` for real HTTP.
        default_timeout: Seconds for requests that don't specify one.
    """

    def __init__(
        self,
        transport: Transport | None = None,
        default_timeout: int = 60,
    ) -> None:
        self._transport = transport or _real_transport
        self._default_timeout = default_timeout
        self._secrets: list[str] = []

    # -- secret management --------------------------------------------------

    def register_secret(self, value: str) -> None:
        """Register *value* for scrubbing from all future error messages."""
        if value and value not in self._secrets:
            self._secrets.append(value)

    def scrub_secret(self, text: str) -> str:
        """Replace every registered secret in *text* with ``"***"``."""
        for secret in self._secrets:
            if secret:
                text = text.replace(secret, "***")
        return text

    # -- HTTP methods -------------------------------------------------------

    def post_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        max_response_bytes: int | None = None,
    ) -> dict[str, Any]:
        """POST *payload* as JSON, return parsed JSON response."""
        body = json.dumps(payload).encode("utf-8")
        request = Request(url, data=body, method="POST")
        request.add_header("content-type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        return self._send(
            request,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        max_response_bytes: int | None = None,
    ) -> dict[str, Any]:
        """GET *url*, return parsed JSON response."""
        request = Request(url, method="GET")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        return self._send(
            request,
            timeout=timeout,
            max_response_bytes=max_response_bytes,
        )

    def get_bytes(
        self,
        url: str,
        *,
        timeout: int | None = None,
        max_bytes: int | None = None,
    ) -> bytes:
        """GET *url*, return raw bytes."""
        request = Request(url, method="GET")
        try:
            request.timeout = timeout or self._default_timeout
            request.max_response_bytes = max_bytes
            _status, body = self._transport(request)
            body = _bounded_body(body, max_bytes=max_bytes)
        except HTTPError as exc:
            detail = self.scrub_secret(
                _read_http_error_body(exc, max_bytes=max_bytes)
            )
            raise AstridError(
                self.scrub_secret(f"HTTP {exc.code} GET {url}: {detail}"),
                recovery_command="check the URL and credentials, then retry",
            ) from exc
        except URLError as exc:
            raise AstridError(
                self.scrub_secret(f"Network error GET {url}: {exc}"),
                recovery_command="check network connectivity, then retry",
            ) from exc
        return body

    # -- polling ------------------------------------------------------------

    def poll_until(
        self,
        status_url: str,
        response_url: str,
        *,
        headers: dict[str, str] | None = None,
        max_wait_sec: int = 300,
        delay_initial: float = 2.0,
        delay_factor: float = 1.4,
        delay_max: float = 8.0,
        max_response_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Poll *status_url* until COMPLETED/OK, then GET *response_url*.

        Raises ``AstridError`` on FAILED / ERROR / CANCELLED status or timeout.
        """
        deadline = time.monotonic() + max_wait_sec
        delay = delay_initial
        while time.monotonic() < deadline:
            status = self.get_json(
                status_url,
                headers=headers,
                max_response_bytes=max_response_bytes,
            )
            state = str(status.get("status") or "").upper()
            if state in {"COMPLETED", "OK"}:
                return self.get_json(
                    response_url,
                    headers=headers,
                    max_response_bytes=max_response_bytes,
                )
            if state in {"FAILED", "ERROR", "CANCELLED"}:
                raise AstridError(
                    self.scrub_secret(f"fal job {state}: {json.dumps(status)}"),
                    recovery_command="inspect the fal job status and retry",
                )
            time.sleep(delay)
            delay = min(delay * delay_factor, delay_max)
        raise AstridError(
            self.scrub_secret(
                f"fal job timed out after {max_wait_sec}s; "
                f"last status_url={status_url}"
            ),
            recovery_command="retry; if it persists, raise max_wait_sec",
        )

    # -- internal -----------------------------------------------------------

    def _send(
        self,
        request: Request,
        *,
        timeout: int | None = None,
        max_response_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Execute *request*, return parsed JSON body."""
        effective = timeout or self._default_timeout
        if effective:
            request.timeout = effective
        request.max_response_bytes = max_response_bytes
        try:
            _status, body = self._transport(request)
            body = _bounded_body(body, max_bytes=max_response_bytes)
        except HTTPError as exc:
            detail = _read_http_error_body(exc, max_bytes=max_response_bytes)
            raise AstridError(
                self.scrub_secret(
                    f"HTTP {exc.code} {request.method} {request.full_url}: {detail}"
                ),
                recovery_command="check the URL and credentials, then retry",
            ) from exc
        except URLError as exc:
            raise AstridError(
                self.scrub_secret(
                    f"Network error {request.method} {request.full_url}: {exc}"
                ),
                recovery_command="check network connectivity, then retry",
            ) from exc
        return json.loads(body.decode("utf-8")) if body else {}


# ---------------------------------------------------------------------------
# Module-level default client factory
# ---------------------------------------------------------------------------

_default_client: HttpClient | None = None


def default_client() -> HttpClient:
    """Return (or create) the module-level singleton ``HttpClient``."""
    global _default_client
    if _default_client is None:
        _default_client = HttpClient()
    return _default_client


# ---------------------------------------------------------------------------
# fal.ai helpers
# ---------------------------------------------------------------------------

def fal_upload(
    client: HttpClient,
    file_path: Path,
    api_key: str,
) -> str:
    """Return a base64 data URI for *file_path* suitable for fal ``image_url`` fields.

    fal's image-input endpoints accept data URIs inline, so no upload roundtrip
    is needed. ``client`` and ``api_key`` are accepted for signature stability
    but unused.
    """
    data = file_path.read_bytes()
    suffix = file_path.suffix.lower().lstrip(".")
    mime_map = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
    }
    mime = mime_map.get(suffix, "image/png")
    return f"data:{mime};base64,{b64encode(data).decode('ascii')}"


def fal_storage_upload(
    client: HttpClient,
    file_path: Path,
    api_key: str,
) -> str:
    """Upload *file_path* to fal CDN storage and return a public ``https://v3.fal.media`` URL.

    This is the recommended path for video/audio inputs and for any file larger
    than a few KB (fal docs discourage base64 data URIs for large files).

    Flow:
      1. Mint a signed upload token via ``POST /storage/auth/token``.
      2. Upload raw bytes to the returned base URL.
      3. Return the ``access_url`` (or ``url``) from the upload response.
    """
    data = file_path.read_bytes()
    suffix = file_path.suffix.lower().lstrip(".")
    mime_map = {
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "webp": "image/webp",
        "gif": "image/gif",
        "mp4": "video/mp4",
        "mov": "video/quicktime",
        "webm": "video/webm",
        "m4v": "video/mp4",
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "flac": "audio/flac",
        "m4a": "audio/mp4",
    }
    mime = mime_map.get(suffix, "application/octet-stream")
    file_name = file_path.name

    # 1. Request upload token
    token_url = "https://rest.alpha.fal.ai/storage/auth/token?storage_type=fal-cdn-v3"
    token_headers = {
        "authorization": f"Key {api_key}",
        "accept": "application/json",
        "content-type": "application/json",
    }
    token_resp = client.post_json(token_url, {}, headers=token_headers)
    token = token_resp.get("token")
    base_url = token_resp.get("base_upload_url") or token_resp.get("base_url")
    if not token or not base_url:
        raise AstridError(
            client.scrub_secret(
                f"fal storage token response missing token/base_url: "
                f"{json.dumps(token_resp)}"
            ),
            recovery_command="retry; the fal storage token response was malformed",
        )

    # 2. Upload file bytes
    upload_url = f"{base_url.rstrip('/')}/files/upload"
    upload_headers = {
        "authorization": f"Bearer {token}",
        "content-type": mime,
        "x-fal-file-name": file_name,
    }
    request = Request(upload_url, data=data, method="POST")
    for key, value in upload_headers.items():
        request.add_header(key, value)
    try:
        _status, body = client._transport(request)
    except HTTPError as exc:
        detail = (
            exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        )
        raise AstridError(
            client.scrub_secret(
                f"HTTP {exc.code} POST {upload_url}: {detail}"
            ),
            recovery_command="check the file and credentials, then retry",
        ) from exc
    except URLError as exc:
        raise AstridError(
            client.scrub_secret(f"Network error POST {upload_url}: {exc}"),
            recovery_command="check network connectivity, then retry",
        ) from exc

    upload_resp = json.loads(body.decode("utf-8")) if body else {}
    access_url = upload_resp.get("access_url") or upload_resp.get("url")
    if not access_url:
        raise AstridError(
            client.scrub_secret(
                f"fal storage upload response missing access_url: "
                f"{json.dumps(upload_resp)}"
            ),
            recovery_command="retry; the fal upload response was malformed",
        )
    return access_url


def fal_submit_and_poll(
    client: HttpClient,
    model_id: str,
    payload: dict[str, Any],
    api_key: str,
    *,
    max_wait_sec: int = 300,
    max_response_bytes: int | None = None,
) -> dict[str, Any]:
    """Submit a job to ``fal-ai/<model_id>``, poll until completion.

    Returns the completed job result dictionary.
    """
    submit_url = f"{FAL_QUEUE_URL}/{model_id}"
    headers = {"authorization": f"Key {api_key}"}
    submission = client.post_json(
        submit_url,
        payload,
        headers=headers,
        max_response_bytes=max_response_bytes,
    )

    status_url = submission.get("status_url")
    response_url = submission.get("response_url")
    if not status_url or not response_url:
        raise AstridError(
            client.scrub_secret(
                f"fal submission missing status_url/response_url: "
                f"{json.dumps(submission)}"
            ),
            recovery_command="retry; the fal submission response was malformed",
        )
    status_url = _validate_fal_queue_url(status_url, field="status_url")
    response_url = _validate_fal_queue_url(response_url, field="response_url")
    result = client.poll_until(
        status_url,
        response_url,
        headers=headers,
        max_wait_sec=max_wait_sec,
        max_response_bytes=max_response_bytes,
    )
    # Merge the request_id so callers can record it.
    if "request_id" not in result and "request_id" in submission:
        result["request_id"] = submission["request_id"]
    return result


WAVESPEED_API_URL = "https://api.wavespeed.ai"


def wavespeed_submit_and_poll(
    client: HttpClient,
    endpoint: str,
    payload: dict[str, Any],
    api_key: str,
    *,
    max_wait_sec: int = 600,
) -> dict[str, Any]:
    """Submit a prediction to a WaveSpeedAI endpoint, poll until completion.

    ``endpoint`` is the full submission URL (e.g.
    ``https://api.wavespeed.ai/api/v3/minimax/music-3.0``) or a path under
    :data:`WAVESPEED_API_URL`.  Submissions use ``Authorization: Bearer``.
    Returns the completed prediction result dictionary with the prediction
    id merged in as ``request_id``.
    """
    submit_url = (
        endpoint
        if endpoint.startswith("http://") or endpoint.startswith("https://")
        else f"{WAVESPEED_API_URL}/{endpoint.lstrip('/')}"
    )
    headers = {"authorization": f"Bearer {api_key}"}
    submission = client.post_json(submit_url, payload, headers=headers)

    task = submission.get("data") if isinstance(submission.get("data"), dict) else submission
    prediction_id = task.get("id")
    if not prediction_id:
        raise AstridError(
            client.scrub_secret(
                f"wavespeed submission missing prediction id: "
                f"{json.dumps(submission)}"
            ),
            recovery_command="retry; the wavespeed submission response was malformed",
        )
    result_url = task.get("urls", {}).get("get") or (
        f"{WAVESPEED_API_URL}/api/v3/predictions/{prediction_id}/result"
    )

    deadline = time.monotonic() + max_wait_sec
    delay = 2.0
    while time.monotonic() < deadline:
        status_body = client.get_json(result_url, headers=headers)
        result = status_body.get("data") if isinstance(status_body.get("data"), dict) else status_body
        state = str(result.get("status") or "").lower()
        if state == "completed":
            if "request_id" not in result and prediction_id:
                result["request_id"] = prediction_id
            return result
        if state in {"failed", "cancelled", "timeout"}:
            raise AstridError(
                client.scrub_secret(
                    f"wavespeed prediction {state}: {json.dumps(result)}"
                ),
                recovery_command="inspect the wavespeed prediction and retry",
            )
        if state not in {"created", "processing"}:
            raise AstridError(
                client.scrub_secret(
                    f"wavespeed prediction unexpected status {state!r}: "
                    f"{json.dumps(result)}"
                ),
                recovery_command="inspect the wavespeed prediction and retry",
            )
        time.sleep(delay)
        delay = min(delay * 1.4, 8.0)
    raise AstridError(
        client.scrub_secret(
            f"wavespeed prediction timed out after {max_wait_sec}s; "
            f"prediction id={prediction_id}"
        ),
        recovery_command="retry; if it persists, raise max_wait_sec",
    )
