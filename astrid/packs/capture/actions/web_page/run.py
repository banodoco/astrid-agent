#!/usr/bin/env python3
"""Capture one admitted HTTPS page to a PNG plus provenance receipt."""

from __future__ import annotations

from astrid.core.pack.entrypoint import guard_canonical_entrypoint, run_pack_main

guard_canonical_entrypoint("capture.web_page")

import argparse
import base64
import hashlib
import json
import os
import re
import select
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from astrid.core._shared.result_manifest import build_manifest, write_manifest
from astrid.core.contracts.errors import AstridError
from astrid.core.env_vars import ASTRID_REMOTION_PROJECT_DIR
from astrid.core.execution.process_group import (
    popen_owned_group,
    terminate_group,
    verify_tree_absent,
)
from astrid.core.rendering.remotion_runtime import resolve_remotion_project_dir

DEFAULT_VIEWPORT = (1600, 1000)
DEFAULT_DEVICE_SCALE = 2
DEFAULT_WAIT_MS = 1500
NAV_TIMEOUT_S = 45.0
CHROME_ENV = "ASTRID_CAPTURE_CHROME"
PROXY_ENV = "ASTRID_BROKER_PROXY"
POLICY_ENV = "ASTRID_NETWORK_POLICY"
ADMISSION_ENV = "ASTRID_NETWORK_ADMISSION"
BROKER_EVIDENCE_ENV = "ASTRID_NETWORK_BROKER_EVIDENCE"
REMOTION_BROWSER_RELATIVE = Path("node_modules/.remotion/chrome-headless-shell")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _origin(url: str) -> str:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError as exc:
        raise AstridError(f"invalid URL: {url!r}") from exc
    if not host:
        raise AstridError(f"url has no host: {url!r}")
    default_port = 443 if parts.scheme.lower() == "https" else 80
    return f"{parts.scheme.lower()}://{host}:{port or default_port}"


def _boolean(value: str) -> bool:
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    raise argparse.ArgumentTypeError(f"expected true or false, got {value!r}")


def parse_viewport(value: str | None) -> tuple[int, int]:
    if value is None or str(value).strip() == "":
        return DEFAULT_VIEWPORT
    match = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", str(value))
    if not match:
        raise AstridError(f"viewport must be WIDTHxHEIGHT, got {value!r}")
    width, height = int(match.group(1)), int(match.group(2))
    if not (16 <= width <= 8000 and 16 <= height <= 8000):
        raise AstridError(f"viewport {width}x{height} is outside 16..8000 px")
    return width, height


def parse_clip(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"none", "off", "null"}:
            return None
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AstridError(f"clip must be JSON {{x,y,w,h}}, got {text!r}") from exc
    if not isinstance(value, dict):
        raise AstridError("clip must be an object with x, y, w, h (CSS px of the viewport)")
    try:
        x, y, w, h = (int(value[key]) for key in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError) as exc:
        raise AstridError("clip needs integer x, y, w and h") from exc
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise AstridError("clip needs x,y >= 0 and w,h > 0")
    return {"x": x, "y": y, "w": w, "h": h}


def validate_url(url: str) -> str:
    """Accept HTTPS URLs only, without credentials or local-file bypasses."""
    text = (url or "").strip()
    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
        _ = parts.port
    except ValueError as exc:
        raise AstridError(f"invalid URL: {url!r}") from exc
    if not host:
        raise AstridError(f"url has no host: {url!r}")
    if parts.username or parts.password:
        raise AstridError("url must not carry credentials")
    if parts.scheme.lower() != "https":
        raise AstridError(f"url must be https, got {parts.scheme}://{host}")
    return text


def _json_env(name: str) -> dict[str, Any]:
    raw = os.environ.get(name, "")
    if not raw:
        raise AstridError(f"host admission is missing {name}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AstridError(f"host admission {name} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise AstridError(f"host admission {name} must be an object")
    return value


def _require_host_admission(url: str) -> dict[str, Any]:
    """Require the exact host-issued broker contract before launching Chromium."""
    proxy = os.environ.get(PROXY_ENV, "").strip()
    if not proxy:
        raise AstridError(
            "capture requires a host-managed broker admission",
            recovery_command="run capture.web_page through the configured host with a declared HTTPS route",
        )
    policy = _json_env(POLICY_ENV)
    admission = _json_env(ADMISSION_ENV)
    broker = policy.get("broker")
    if not isinstance(broker, Mapping):
        raise AstridError("capture network policy has no host-managed broker descriptor")
    if not bool(broker.get("host_managed", broker.get("managed", False))):
        raise AstridError("capture refuses a non-host-managed network broker")
    if not bool(broker.get("enforced")) or not bool(broker.get("observable")):
        raise AstridError("capture requires an enforced and observable host broker")
    if bool(policy.get("allow_redirects", True)):
        raise AstridError("capture refuses a network policy that permits redirects")
    dynamic_inputs = policy.get("dynamic_url_inputs", ())
    if isinstance(dynamic_inputs, str):
        dynamic_inputs = (dynamic_inputs,)
    if "url" not in {str(item) for item in dynamic_inputs or ()}:
        raise AstridError("capture network policy must admit the url as a dynamic route")
    routes = admission.get("allowed_routes", ())
    if isinstance(routes, str):
        routes = (routes,)
    route = _origin(url)
    if route not in {str(item) for item in routes or ()}:
        raise AstridError(f"host admission does not contain the requested origin {route}")
    evidence_path = os.environ.get(BROKER_EVIDENCE_ENV, "").strip()
    if not evidence_path or not Path(evidence_path).is_absolute():
        raise AstridError("host admission has no absolute broker evidence path")
    return {"policy": policy, "admission": admission, "broker": dict(broker), "evidence_path": evidence_path}


def _network_receipt(url: str, admission: Mapping[str, Any]) -> dict[str, Any]:
    policy = admission["policy"]
    broker = admission["broker"]
    return {
        "requested_origin": _origin(url),
        "allow_redirects": False,
        "broker": {
            "kind": str(broker.get("kind", "broker")),
            "host_managed": bool(broker.get("host_managed", broker.get("managed", False))),
            "enforced": bool(broker.get("enforced")),
            "observable": bool(broker.get("observable")),
            "evidence_path": admission["evidence_path"],
            "evidence_status": "host-finalized-after-child-exit",
        },
        "admission_sha256": hashlib.sha256(_canonical(admission["admission"])).hexdigest(),
        "policy_allowed_protocols": list(policy.get("allowed_protocols", ())),
    }


def _checked_executable(path_value: str, label: str) -> str:
    candidate = Path(path_value).expanduser()
    if not candidate.is_absolute():
        raise AstridError(f"{label} must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise AstridError(f"{label} does not exist: {candidate}") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise AstridError(f"{label} is not an executable file: {resolved}")
    return str(resolved)


def find_chrome(chrome: str | None = None) -> str:
    """Resolve a host-configured browser or the provisioned Remotion browser."""
    override = (chrome or os.environ.get(CHROME_ENV, "")).strip()
    if override:
        return _checked_executable(override, CHROME_ENV)
    if not os.environ.get(ASTRID_REMOTION_PROJECT_DIR, "").strip():
        raise AstridError(
            "no provisioned Remotion browser: host must set ASTRID_REMOTION_PROJECT_DIR or ASTRID_CAPTURE_CHROME",
            recovery_command="configure the host-provisioned Remotion project; do not install a browser during capture",
        )
    try:
        project = resolve_remotion_project_dir()
    except (OSError, ValueError) as exc:
        raise AstridError("host-provisioned Remotion project could not be resolved") from exc
    root = project / REMOTION_BROWSER_RELATIVE
    matches = sorted(
        path for path in root.glob("*/*/chrome-headless-shell")
        if path.is_file() and os.access(path, os.X_OK)
    )
    if not matches:
        raise AstridError(
            f"no provisioned Remotion browser found under {root}",
            recovery_command="provision the Remotion browser in the host runtime; do not install it during capture",
        )
    return _checked_executable(str(matches[-1]), "Remotion browser")


class _Cdp:
    """Minimal JSON DevTools client over Chromium's pipe transport."""

    def __init__(self, write_fd: int, read_fd: int) -> None:
        self._w = write_fd
        self._r = read_fd
        self._buf = b""
        self._next_id = 0
        self.events: list[dict[str, Any]] = []

    def send(self, method: str, params: dict[str, Any] | None = None, session_id: str | None = None) -> int:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session_id:
            message["sessionId"] = session_id
        data = json.dumps(message).encode("utf-8") + b"\0"
        view = memoryview(data)
        while view:
            written = os.write(self._w, view)
            view = view[written:]
        return self._next_id

    def _read_one(self, deadline: float) -> dict[str, Any]:
        while b"\0" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AstridError("timed out waiting for the browser")
            ready, _, _ = select.select([self._r], [], [], remaining)
            if not ready:
                continue
            chunk = os.read(self._r, 1 << 16)
            if not chunk:
                raise AstridError("the browser closed its debugging pipe unexpectedly")
            self._buf += chunk
        raw, _, self._buf = self._buf.partition(b"\0")
        return json.loads(raw.decode("utf-8"))

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session_id: str | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        msg_id = self.send(method, params, session_id)
        while True:
            message = self._read_one(deadline)
            if message.get("id") == msg_id:
                if "error" in message:
                    error = message["error"]
                    raise AstridError(f"browser refused {method}: {error.get('message', error)}")
                return message.get("result", {})
            self._keep(message)

    def pump(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                self._keep(self._read_one(deadline))
            except AstridError as exc:
                if "timed out" in str(exc):
                    return
                raise

    def wait_for(self, predicate, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while True:
            for event in self.events:
                if predicate(event):
                    return event
            if time.monotonic() >= deadline:
                return None
            try:
                self._keep(self._read_one(deadline))
            except AstridError as exc:
                if "timed out" in str(exc):
                    return None
                raise

    def _keep(self, message: dict[str, Any]) -> None:
        if "method" in message:
            self.events.append(message)
            if len(self.events) > 5000:
                del self.events[:1000]


def _chrome_args(chrome: str, profile: Path, proxy: str) -> list[str]:
    return [
        chrome,
        "--remote-debugging-pipe",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-extensions",
        "--disable-gpu",
        "--hide-scrollbars",
        "--mute-audio",
        "--font-render-hinting=none",
        f"--proxy-server={proxy}",
        "about:blank",
    ]


def _wire_fds(in_r: int, out_w: int) -> None:
    a = os.dup(in_r)
    b = os.dup(out_w)
    os.dup2(a, 3)
    os.dup2(b, 4)
    os.set_inheritable(3, True)
    os.set_inheritable(4, True)


def _launch(chrome: str, profile: Path, proxy: str, log_path: Path) -> tuple[subprocess.Popen, _Cdp]:
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    try:
        with log_path.open("ab") as log:
            proc = popen_owned_group(
                _chrome_args(chrome, profile, proxy),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                close_fds=False,
                preexec_fn=lambda: _wire_fds(in_r, out_w),
            )
    except (OSError, ValueError) as exc:
        for fd in (in_r, in_w, out_r, out_w):
            try:
                os.close(fd)
            except OSError:
                pass
        raise AstridError(f"could not start host-custodied Chromium: {exc}") from exc
    os.close(in_r)
    os.close(out_w)
    return proc, _Cdp(in_w, out_r)


def _shutdown(proc: subprocess.Popen, cdp: _Cdp) -> None:
    try:
        cdp.call("Browser.close", timeout=5)
    except (AstridError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        terminate_group(proc, grace_seconds=1.0)
        proc.wait(timeout=5)
    verify_tree_absent(proc)
    for fd in (cdp._r, cdp._w):
        try:
            os.close(fd)
        except OSError:
            pass


def _png_size(data: bytes) -> tuple[int, int]:
    if len(data) < 24:
        raise AstridError("browser returned an incomplete PNG")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def capture(
    *,
    url: str,
    out_dir: Path,
    viewport: tuple[int, int] = DEFAULT_VIEWPORT,
    device_scale: int = DEFAULT_DEVICE_SCALE,
    full_page: bool = False,
    wait_ms: int = DEFAULT_WAIT_MS,
    clip: dict[str, int] | None = None,
    dark_mode: bool = False,
    chrome: str | None = None,
) -> dict[str, Any]:
    """Capture one admitted page, writing managed outputs into ``out_dir``."""
    url = validate_url(url)
    admission = _require_host_admission(url)
    if not (1 <= int(device_scale) <= 3):
        raise AstridError("device_scale must be 1, 2 or 3")
    if not (0 <= int(wait_ms) <= 30000):
        raise AstridError("wait_ms must be between 0 and 30000")
    width, height = viewport
    binary = find_chrome(chrome)
    proxy = os.environ[PROXY_ENV].strip()
    out_dir.mkdir(parents=True, exist_ok=True)
    profile = Path(tempfile.mkdtemp(prefix="astrid-capture-web-page-"))
    proc: subprocess.Popen | None = None
    cdp: _Cdp | None = None
    try:
        proc, cdp = _launch(binary, profile, proxy, profile / "browser.log")
        browser = cdp.call("Browser.getVersion", timeout=30)
        user_agent = str(browser.get("userAgent", ""))
        target_id = cdp.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        cdp.call("Page.enable", session_id=session)
        cdp.call("Network.enable", session_id=session)
        cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": int(device_scale), "mobile": False},
            session_id=session,
        )
        if dark_mode:
            cdp.call(
                "Emulation.setEmulatedMedia",
                {"features": [{"name": "prefers-color-scheme", "value": "dark"}]},
                session_id=session,
            )
        nav = cdp.call("Page.navigate", {"url": url}, session_id=session, timeout=NAV_TIMEOUT_S)
        if nav.get("errorText"):
            raise AstridError(
                f"navigation to {url} failed: {nav['errorText']}",
                recovery_command="check the admitted HTTPS origin; broker blocks and redirects are reported here",
            )
        loader_id = nav.get("loaderId")
        loaded = cdp.wait_for(
            lambda event: event.get("method") == "Page.loadEventFired" and event.get("sessionId") == session,
            timeout=NAV_TIMEOUT_S,
        )
        if loaded is None:
            raise AstridError(f"page {url} did not finish loading within {int(NAV_TIMEOUT_S)} s")
        cdp.pump(int(wait_ms) / 1000.0)

        http_status = None
        for event in cdp.events:
            if event.get("method") != "Network.responseReceived" or event.get("sessionId") != session:
                continue
            params = event.get("params", {})
            if params.get("type") != "Document":
                continue
            if loader_id and params.get("loaderId") != loader_id:
                continue
            http_status = params.get("response", {}).get("status")

        probe = cdp.call(
            "Runtime.evaluate",
            {"expression": "({title: document.title, href: location.href})", "returnByValue": True},
            session_id=session,
        )
        page = probe.get("result", {}).get("value") or {}
        final_url = str(page.get("href") or "")
        if not final_url:
            raise AstridError("browser did not return a final HTTPS URL")
        final_url = validate_url(final_url)
        if _origin(final_url) != _origin(url):
            raise AstridError(
                f"cross-origin redirect refused: {_origin(url)} -> {_origin(final_url)}",
                recovery_command="use an HTTPS URL whose redirect chain stays on the admitted origin",
            )

        if clip is not None:
            shot_clip: dict[str, Any] | None = {
                "x": clip["x"], "y": clip["y"], "width": clip["w"], "height": clip["h"], "scale": 1,
            }
            beyond = True
        elif full_page:
            metrics = cdp.call("Page.getLayoutMetrics", session_id=session)
            size = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
            shot_clip = {
                "x": 0, "y": 0, "width": float(size.get("width", width)),
                "height": float(size.get("height", height)), "scale": 1,
            }
            beyond = True
        else:
            shot_clip = None
            beyond = False
        screenshot_params: dict[str, Any] = {"format": "png", "captureBeyondViewport": beyond}
        if shot_clip is not None:
            screenshot_params["clip"] = shot_clip
        shot = cdp.call("Page.captureScreenshot", screenshot_params, session_id=session, timeout=60)
        png = base64.b64decode(shot["data"])
        if not png.startswith(b"\x89PNG"):
            raise AstridError("browser returned a non-PNG screenshot")
        (out_dir / "screenshot.png").write_bytes(png)
        image_width, image_height = _png_size(png)
        receipt = {
            "url": url,
            "final_url": final_url,
            "title": str(page.get("title") or ""),
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "viewport": {"width": width, "height": height},
            "http_status": http_status,
            "user_agent": user_agent,
            "device_scale": int(device_scale),
            "full_page": bool(full_page),
            "clip": clip,
            "dark_mode": bool(dark_mode),
            "wait_ms": int(wait_ms),
            "image": {"width_px": image_width, "height_px": image_height},
            "browser": {
                "source": "explicit_config" if (chrome or os.environ.get(CHROME_ENV, "").strip()) else "remotion_provisioned",
                "resource": str(REMOTION_BROWSER_RELATIVE),
            },
            "network": _network_receipt(url, admission),
        }
        (out_dir / "receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return receipt
    finally:
        if proc is not None and cdp is not None:
            _shutdown(proc, cdp)
        shutil.rmtree(profile, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capture.web_page", description="Capture an HTTPS web page to PNG plus receipt.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", type=Path, required=True, help="Output directory.")
    parser.add_argument("--viewport", default="1600x1000")
    parser.add_argument("--device-scale", type=int, default=DEFAULT_DEVICE_SCALE)
    parser.add_argument("--full-page", type=_boolean, default=False)
    parser.add_argument("--wait-ms", type=int, default=DEFAULT_WAIT_MS)
    parser.add_argument("--clip", default=None, help="Optional {x,y,w,h} in CSS px of the viewport.")
    parser.add_argument("--dark-mode", type=_boolean, default=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    def _run() -> int:
        args = build_parser().parse_args(argv)
        out_dir = args.out.expanduser().resolve()
        parsed_clip = parse_clip(args.clip)
        receipt = capture(
            url=args.url,
            out_dir=out_dir,
            viewport=parse_viewport(args.viewport),
            device_scale=args.device_scale,
            full_page=bool(args.full_page),
            wait_ms=args.wait_ms,
            clip=parsed_clip,
            dark_mode=bool(args.dark_mode),
        )
        manifest = build_manifest(
            kind="web_page_capture",
            inputs={
                "url": args.url,
                "viewport": args.viewport,
                "device_scale": args.device_scale,
                "full_page": bool(args.full_page),
                "wait_ms": args.wait_ms,
                "clip": parsed_clip,
                "dark_mode": bool(args.dark_mode),
            },
            outputs=[
                {"name": "screenshot", "path": "screenshot.png", "type": "file", "artifact_type": "image/png", "role": "result", "is_primary": True},
                {"name": "receipt", "path": "receipt.json", "type": "file", "artifact_type": "capture/provenance", "role": "auxiliary"},
            ],
            created=datetime.now(timezone.utc).isoformat(),
        )
        write_manifest(out_dir / "manifest.json", manifest)
        print(json.dumps(receipt, sort_keys=True))
        return 0

    return run_pack_main("capture.web_page", _run, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
