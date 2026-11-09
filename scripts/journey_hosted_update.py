#!/usr/bin/env python3
"""Drive the hosted page's Update button through the Runtime door in a journey row.

The hosted page sends one request: ``POST /api/runtime/v1/update`` from the
origin ``https://astrid.haus`` with ``Content-Length: 0`` and no ``Authorization``
or ``Cookie`` header. The door adds the product credential (scope
``installer:update``) on its own side. Runtime then fetches
``https://astrid.haus/install-manifest.json`` and the bootstrap script from the
public host, so a journey row can only offer a stand-in release by intercepting
that host on the runner itself:

* a throwaway CA and a leaf certificate for ``astrid.haus`` are generated per
  run (the CA key never leaves a private temporary directory);
* the CA is added to the runner's trust store and ``astrid.haus`` is mapped to
  127.0.0.1 in the hosts file, both for the shortest window that covers the
  request, and both removed again in a ``finally`` block;
* a local HTTPS server on 127.0.0.1:443 serves the stand-in manifest and the
  App checkout's own bootstrap scripts.

On the button row (``journey_matrix.HOSTED_BUTTON_JOB_IDS``) the same intercept also serves the
candidate's ``npm run build:hosted`` output at ``https://astrid.haus``, and ``journey_update_button.mjs``
opens it in Chromium (the leaf pinned by its public key), waits for Settings to offer Update now for the
installed Astrid, and clicks it. The receipt's request is then what the browser saw itself send, and the
page's own reads of the stand-in are kept apart from Runtime's.

Nothing in the product changes and no verification is bypassed: the installed
interpreter verifies the certificate chain exactly as it would for the real host.
This module holds no secret. Privileged steps run through ``sudo -n`` on Linux
and macOS runners and directly on the (Administrator) Windows runner.

Subcommands other than the library calls are internal and run as root:
``hosts``, ``trust`` and ``serve``. ``revert`` is the workflow's belt and braces
cleanup and is safe to run when nothing was changed.
"""
from __future__ import annotations

import argparse
import datetime
import http.client
import http.server
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

HOST = "astrid.haus"
ORIGIN = "https://astrid.haus"
MARK = "# astrid-journey-hosted-update"
CA_NAME = "astrid-journey-hosted-update CA"
TRUST_FILE = "astrid-journey-hosted-update.crt"
PEM_BEGIN = "# BEGIN astrid-journey-hosted-update\n"
PEM_END = "# END astrid-journey-hosted-update\n"
UPDATE_PATH = "/api/runtime/v1/update"
STATUS_PATH = UPDATE_PATH + "/status"
MANIFEST_ROUTE = "/install-manifest.json"
BOOTSTRAP_ROUTES = ("/install", "/install.ps1")
ROOT = Path(__file__).resolve().parents[1]
# The hashed lock already used by the proof lanes; only the certificate tool's closure is read.
CRYPTO_LOCK = ROOT / "requirements" / "proof.lock"
CRYPTO_PACKAGES = ("cryptography", "cffi", "pycparser")
SENT_HEADERS = ("Host", "Origin", "Content-Length")
# The button row's browser appends this to its user agent, so the page's own fetches of the stand-in
# are told apart from Runtime's: only Runtime's count as the release Runtime fetched.
PAGE_AGENT_MARK = "astrid-journey-update-button"
# The workspace route a visitor opens; "/" is the public landing page. Settings opens on its Updates tab.
PAGE_ENTRY_ROUTE = "/tools/video-editor"
PAGE_ENTRY = PAGE_ENTRY_ROUTE + "?settings=updates"
BUTTON_SCRIPT = ROOT / "scripts" / "journey_update_button.mjs"
PROXY_VARIABLES = frozenset({"http_proxy", "https_proxy", "all_proxy", "no_proxy"})
MIME_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
              ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json",
              ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".webp": "image/webp", ".ico": "image/x-icon", ".woff": "font/woff", ".woff2": "font/woff2",
              ".mp4": "video/mp4", ".webm": "video/webm", ".wasm": "application/wasm", ".txt": "text/plain",
              ".webmanifest": "application/manifest+json", ".map": "application/json"}
ACCEPTED_KEYS = {"accepted", "state", "operation_id", "message"}


# -- certificates -----------------------------------------------------------

def ensure_cryptography() -> None:
    """Import ``cryptography``, installing the hash-locked closure when it is absent."""
    try:
        import cryptography  # noqa: F401
        return
    except ImportError:
        pass
    text = CRYPTO_LOCK.read_text(encoding="utf-8")
    blocks, current = [], None
    for line in text.splitlines():
        if line.strip() == "" or line.startswith("#"):
            continue
        if not line[0].isspace():
            current = [line]
            blocks.append(current)
        elif current is not None:
            current.append(line)
    wanted = [b for b in blocks if re.match(r"(%s)==" % "|".join(CRYPTO_PACKAGES), b[0])]
    if len(wanted) != len(CRYPTO_PACKAGES):
        raise RuntimeError("The hashed lock does not pin the certificate tool closure")
    with tempfile.TemporaryDirectory(prefix="astrid-journey-pip-") as directory:
        requirements = Path(directory) / "requirements.txt"
        requirements.write_text("\n".join("\n".join(b) for b in wanted) + "\n", encoding="utf-8")
        subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                        "--require-hashes", "--no-deps", "-r", str(requirements)], check=True, timeout=600)


def make_certs(directory: Path) -> tuple[Path, Path, Path]:
    """A throwaway CA and an ``astrid.haus`` leaf signed by it; returns (ca, leaf, key) PEM paths."""
    ensure_cryptography()
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    start, end = now - datetime.timedelta(minutes=5), now + datetime.timedelta(days=1)

    def name(common: str):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common)])

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(name(CA_NAME)).issuer_name(name(CA_NAME))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(start).not_valid_after(end)
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(name(HOST)).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(end)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    ca_path, leaf_path, key_path = directory / "ca.pem", directory / "leaf.pem", directory / "leaf.key"
    ca_path.write_bytes(ca.public_bytes(pem))
    leaf_path.write_bytes(leaf.public_bytes(pem))
    key_path.write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return ca_path, leaf_path, key_path


def spki_pin(leaf: Path) -> str:
    """Base64 SHA-256 of the leaf's public key, the form Chromium's SPKI allow list takes."""
    import base64
    import hashlib
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    public = x509.load_pem_x509_certificate(leaf.read_bytes()).public_key()
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(der).digest()).decode("ascii")


# -- privileged runner changes (run as root, or Administrator) --------------

def hosts_path() -> Path:
    if os.name == "nt":
        return Path(os.environ["SystemRoot"]) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


def hosts_add() -> None:
    path = hosts_path()
    hosts_remove()
    raw = path.read_bytes()
    eol = b"\r\n" if b"\r\n" in raw else b"\n"
    if raw and not raw.endswith(b"\n"):
        raw += eol
    path.write_bytes(raw + f"127.0.0.1 {HOST} {MARK}".encode() + eol)
    if os.name == "nt":
        subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=60, check=False)


def hosts_remove() -> None:
    path = hosts_path()
    raw = path.read_bytes()
    kept = b"".join(line for line in raw.splitlines(keepends=True) if MARK.encode() not in line)
    if kept != raw:
        path.write_bytes(kept)
        if os.name == "nt":
            subprocess.run(["ipconfig", "/flushdns"], capture_output=True, timeout=60, check=False)


def _run(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{Path(argv[0]).name} failed ({result.returncode}): "
                           f"{(result.stdout + result.stderr).strip()[-300:]}")
    return result


# macOS keeps its keychain apart from the OpenSSL file the uv-managed interpreter reads.
DARWIN_PEM_FILES = (Path("/etc/ssl/cert.pem"),)


def _strip_pem_block(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="surrogateescape")
    while PEM_BEGIN in text and PEM_END in text:
        head, _, rest = text.partition(PEM_BEGIN)
        _, _, tail = rest.partition(PEM_END)
        text = head + tail
    path.write_text(text, encoding="utf-8", errors="surrogateescape")


def trust_add(ca: Path) -> None:
    if sys.platform.startswith("linux"):
        destination = Path("/usr/local/share/ca-certificates") / TRUST_FILE
        shutil.copyfile(ca, destination)
        destination.chmod(0o644)
        _run(["update-ca-certificates"])
    elif sys.platform == "darwin":
        _run(["security", "add-trusted-cert", "-d", "-r", "trustRoot",
              "-k", "/Library/Keychains/System.keychain", str(ca)])
        block = PEM_BEGIN + ca.read_text(encoding="ascii") + PEM_END
        for bundle in DARWIN_PEM_FILES:
            if bundle.is_file():
                _strip_pem_block(bundle)
                with bundle.open("a", encoding="utf-8") as stream:
                    stream.write(("" if bundle.read_bytes().endswith(b"\n") else "\n") + block)
    else:
        _run(["certutil", "-addstore", "-f", "Root", str(ca)])


def trust_remove() -> list[str]:
    """Remove every trace of the throwaway CA; returns the failures (empty when clean)."""
    failures = []
    try:
        if sys.platform.startswith("linux"):
            destination = Path("/usr/local/share/ca-certificates") / TRUST_FILE
            existed = destination.exists()
            destination.unlink(missing_ok=True)
            if existed:
                _run(["update-ca-certificates", "--fresh"])
        elif sys.platform == "darwin":
            _run(["security", "delete-certificate", "-c", CA_NAME, "/Library/Keychains/System.keychain"], check=False)
            if _run(["security", "find-certificate", "-c", CA_NAME, "/Library/Keychains/System.keychain"],
                    check=False).returncode == 0:
                failures.append("keychain certificate remains")
            for bundle in DARWIN_PEM_FILES:
                if bundle.is_file():
                    _strip_pem_block(bundle)
                    if PEM_BEGIN in bundle.read_text(encoding="utf-8", errors="surrogateescape"):
                        failures.append(f"{bundle} block remains")
        else:
            _run(["certutil", "-delstore", "Root", CA_NAME], check=False)
            if _run(["certutil", "-store", "Root", CA_NAME], check=False).returncode == 0:
                failures.append("Root store certificate remains")
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}"[:300])
    return failures


def serve(args: argparse.Namespace) -> int:
    """Serve the stand-in release (and, for the button row, the hosted page) over HTTPS until the stop file appears."""
    routes = {path: Path(file) for path, file in json.loads(Path(args.routes).read_text()).items()}
    types = json.loads(Path(args.types).read_text()) if args.types else {}
    log = Path(args.log)
    lock = threading.Lock()

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _respond(self, head_only: bool) -> None:
            path = urlsplit(self.path).path  # the page asks for its entry with a query string
            file = routes.get(path)
            body = file.read_bytes() if file is not None else None
            status = 200 if body is not None else 404
            hit = {"method": self.command, "path": path, "status": status}
            if PAGE_AGENT_MARK in (self.headers.get("User-Agent") or ""):
                hit["page"] = True
            with lock, log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(hit) + "\n")
            payload = body if body is not None else b""
            self.send_response(status)
            if body is not None and path in types:
                self.send_header("Content-Type", types[path])
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            if not head_only:
                self.wfile.write(payload)

        def do_GET(self):  # noqa: N802 - http.server API
            self._respond(False)

        def do_HEAD(self):  # noqa: N802 - http.server API
            self._respond(True)

        def log_message(self, *_args):
            pass

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)
    class Server(http.server.ThreadingHTTPServer):
        def handle_error(self, request, client_address):  # a client that rejects the certificate is not an error here
            pass

    server = Server(("127.0.0.1", args.port), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    Path(args.ready).write_text("ready\n", encoding="utf-8")
    deadline = time.monotonic() + args.max_seconds
    stop = Path(args.stop)
    try:
        while not stop.exists() and time.monotonic() < deadline:
            time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)
    return 0


def _internal(args: argparse.Namespace) -> int:
    if args.command == "hosts":
        (hosts_add if args.action == "add" else hosts_remove)()
    elif args.command == "trust":
        if args.action == "add":
            trust_add(Path(args.ca))
        else:
            failures = trust_remove()
            if failures:
                print("; ".join(failures), file=sys.stderr)
                return 1
    elif args.command == "serve":
        return serve(args)
    return 0


# -- orchestration (unprivileged) -------------------------------------------

def privileged(argv: list[str]) -> list[str]:
    if os.name == "nt" or os.geteuid() == 0:
        return list(argv)
    return ["sudo", "-n", *argv]


def _helper(*argv: str) -> list[str]:
    return privileged([sys.executable, "-I", str(Path(__file__).resolve()), *argv])


def revert() -> list[str]:
    """Undo the hosts mapping and the trust entry; returns what could not be undone."""
    failures = []
    for argv in (("hosts", "remove"), ("trust", "remove")):
        result = subprocess.run(_helper(*argv), capture_output=True, text=True, timeout=300, check=False)
        if result.returncode:
            failures.append(f"{argv[0]} remove: {(result.stdout + result.stderr).strip()[-200:]}")
    if MARK.encode() in hosts_path().read_bytes():
        failures.append("hosts mapping remains")
    return failures


class Intercept:
    """Map ``astrid.haus`` to a local HTTPS server that serves ``routes``, for the length of a ``with``."""

    def __init__(self, routes: dict[str, bytes | Path], *, python: str | os.PathLike | None = None, port: int = 443,
                 max_seconds: int = 3600, types: dict[str, str] | None = None):
        """``routes`` maps a path to its bytes, or to a file served as it is (the hosted page's build)."""
        self.routes, self.python, self.port, self.max_seconds = dict(routes), python, port, max_seconds
        self.types = dict(types or {})
        self.leaf_spki: str | None = None
        self.directory: Path | None = None
        self.process: subprocess.Popen | None = None
        self.original_hosts: bytes | None = None
        self.reverted: dict = {}
        self._started = False

    # context protocol
    def __enter__(self) -> "Intercept":
        try:
            self.start()
        except BaseException:
            self.stop(raise_on_problem=False)
            raise
        return self

    def __exit__(self, exc_type, *_exc) -> None:
        # A revert problem never hides the failure that is already propagating.
        self.stop(raise_on_problem=exc_type is None)

    def start(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="astrid-journey-hosted-"))
        self._started = True
        self.original_hosts = hosts_path().read_bytes()
        if os.name != "nt":
            self.directory.chmod(0o700)
        d = self.directory
        files = {}
        for index, (route, body) in enumerate(self.routes.items()):
            if isinstance(body, Path):
                files[route] = str(body.resolve())
                continue
            (d / f"route-{index}").write_bytes(body)
            files[route] = str(d / f"route-{index}")
        (d / "routes.json").write_text(json.dumps(files), encoding="utf-8")
        (d / "types.json").write_text(json.dumps(self.types), encoding="utf-8")
        (d / "hits.jsonl").write_text("", encoding="utf-8")
        ca, leaf, key = make_certs(d)
        self.leaf_spki = spki_pin(leaf)
        self._must(_helper("trust", "add", "--ca", str(ca)), "trust the stand-in CA")
        self._must(_helper("hosts", "add"), "map astrid.haus to the stand-in")
        self.process = subprocess.Popen(
            _helper("serve", "--cert", str(leaf), "--key", str(key), "--routes", str(d / "routes.json"),
                    "--types", str(d / "types.json"),
                    "--log", str(d / "hits.jsonl"), "--ready", str(d / "ready"), "--stop", str(d / "stop"),
                    "--port", str(self.port), "--max-seconds", str(self.max_seconds)),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        deadline = time.monotonic() + 60
        while not (d / "ready").exists():
            if self.process.poll() is not None:
                raise RuntimeError("The stand-in server exited: " + (self.process.stdout.read() or "")[-400:])
            if time.monotonic() > deadline:
                raise RuntimeError("The stand-in server did not become ready")
            time.sleep(0.2)
        if self.python is not None:
            self.preflight(self.python)

    @staticmethod
    def _must(argv: list[str], what: str) -> None:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
        if result.returncode:
            raise RuntimeError(f"Could not {what}: {(result.stdout + result.stderr).strip()[-300:]}")

    def preflight(self, python: str | os.PathLike) -> None:
        """The interpreter the update helper runs under must verify the stand-in through the system trust."""
        import hashlib
        script = ("import hashlib,sys,urllib.request;"
                  "print(hashlib.sha256(urllib.request.urlopen(sys.argv[1],timeout=30).read()).hexdigest())")
        env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
        result = subprocess.run([str(python), "-I", "-c", script, f"https://{HOST}{MANIFEST_ROUTE}"],
                                capture_output=True, text=True, timeout=120, env=env, check=False)
        manifest = self.routes[MANIFEST_ROUTE]
        expected = hashlib.sha256(manifest.read_bytes() if isinstance(manifest, Path) else manifest).hexdigest()
        if result.returncode or result.stdout.strip() != expected:
            raise RuntimeError("The installed interpreter does not reach the stand-in release over verified HTTPS: "
                               + (result.stderr.strip() or result.stdout.strip())[-300:])

    def hits(self) -> list[dict]:
        path = self.directory / "hits.jsonl" if self.directory else None
        if path is None or not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def stop(self, *, raise_on_problem: bool = True) -> None:
        """Idempotent; always attempts every revert step and records what it could not undo."""
        if not self._started or self.reverted.get("done"):
            return
        problems = []
        if self.directory is not None:
            (self.directory / "stop").write_text("stop\n", encoding="utf-8")
        if self.process is not None:
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    problems.append("server had to be killed")
            if self.process.stdout is not None:
                self.process.stdout.close()
        problems.extend(revert())
        if self.original_hosts is not None:
            now = hosts_path().read_bytes()
            if now.rstrip() != self.original_hosts.rstrip():
                problems.append("hosts file differs from its original")
        self.reverted = {"done": True, "clean": not problems, "problems": problems}
        if self.directory is not None:
            shutil.rmtree(self.directory, ignore_errors=True)
        if problems:
            message = "Hosted update intercept was not fully reverted: " + "; ".join(problems)
            if raise_on_problem:
                raise RuntimeError(message)
            print(message, file=sys.stderr, flush=True)


# -- the page's request -----------------------------------------------------

def door_request(base_url: str, method: str, path: str, *, timeout: float) -> tuple[int, dict | None]:
    """One request exactly as the hosted page sends it: Origin only, no credential, no body."""
    parts = urlsplit(base_url)
    connection = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    try:
        connection.putrequest(method, path, skip_accept_encoding=True)
        connection.putheader("Origin", ORIGIN)
        if method == "POST":
            connection.putheader("Content-Length", "0")
        connection.endheaders()
        response = connection.getresponse()
        raw = response.read(1024 * 1024)
    finally:
        connection.close()
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        value = None
    return response.status, value if isinstance(value, dict) else None


def post_update(base_url: str, *, timeout: float = 400) -> dict:
    status, body = door_request(base_url, "POST", UPDATE_PATH, timeout=timeout)
    excerpt = json.dumps(body)[:400] if body is not None else "<not JSON>"
    if status != 202 or body is None or body.get("accepted") is not True or body.get("state") != "accepted":
        raise RuntimeError(f"The door did not accept the Update request (HTTP {status}): {excerpt}")
    operation = body.get("operation_id")
    if not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation):
        raise RuntimeError(f"The accepted Update has no operation id: {excerpt}")
    return {"status": status, "operation_id": operation}


def wait_terminal(base_url: str, operation_id: str, *, timeout: float, interval: float = 3.0,
                  recovery_grace: float = 90.0, sleep=time.sleep, clock=time.monotonic, tick=None,
                  tick_every: float = 60.0) -> dict:
    """Poll the page's status route until this operation ends; success only if it was ``updated``.

    ``tick(body)``, when given, observes the status about every ``tick_every`` seconds while the update runs.
    """
    deadline, recovering_since, last = clock() + timeout, None, None
    next_tick = clock()
    while clock() < deadline:
        try:
            status, body = door_request(base_url, "GET", STATUS_PATH, timeout=30)
        except (OSError, http.client.HTTPException):
            status, body = None, None  # The Runtime is restarting.
        if tick is not None and clock() >= next_tick:
            next_tick = clock() + tick_every
            tick(body if status == 200 and body is not None and body.get("operation_id") == operation_id else None)
        if status == 200 and body is not None and body.get("operation_id") == operation_id:
            last = body
            if body.get("state") == "terminal":
                if body.get("outcome") != "updated":
                    raise RuntimeError(f"The Update ended as {body.get('outcome')!r}: {json.dumps(body)[:500]}")
                return body
            if body.get("state") == "recovery-needed":
                recovering_since = recovering_since if recovering_since is not None else clock()
                if clock() - recovering_since > recovery_grace:
                    raise RuntimeError(f"The Update needs recovery: {json.dumps(body)[:500]}")
            else:
                recovering_since = None
        sleep(interval)
    raise RuntimeError(f"The Update did not finish within {int(timeout)}s; last status: {json.dumps(last)[:500]}")


def _tail(path: Path, limit: int = 1024 * 1024) -> bytes:
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - limit))
        return stream.read(limit)


def capture_update_evidence(root: Path, evidence: Path, *, failed: bool) -> None:
    """Keep the update's own records under allowlisted evidence names (best effort, never raises).

    The worker log is kept always; on failure also the job record, the C2 journal and, on Linux,
    the transient unit's journal, so the artifact explains the failure even when the row fails.
    """
    record = None
    try:
        install = json.loads((root / "install.json").read_text(encoding="utf-8"))
        record = install.get("update_job") if isinstance(install, dict) else None
        record = record if isinstance(record, dict) else None
    except (OSError, ValueError):
        pass
    try:
        if record is not None:
            (evidence / "update-bootstrap.log").write_bytes(_tail(Path(record["log_path"])))
    except (OSError, KeyError, TypeError):
        pass
    if not failed:
        return
    try:
        if record is not None:
            (evidence / "update-job-record.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                                                             encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass
    try:
        (evidence / "update-transaction.json").write_bytes(_tail(root / "update-transaction.json"))
    except OSError:
        pass
    operation = record.get("operation_id") if record else None
    if sys.platform == "linux" and isinstance(operation, str) and re.fullmatch(r"[0-9a-f]{32}", operation):
        try:
            result = subprocess.run(["journalctl", "--user", "-u", f"astrid-update-{operation}.service",
                                     "--no-pager", "-o", "short-iso"], capture_output=True, text=True,
                                    timeout=60, check=False)
            (evidence / "update-job-journal.log").write_text(
                (result.stdout + result.stderr)[-1024 * 1024:], encoding="utf-8")
        except (OSError, subprocess.SubprocessError):
            pass


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def release_routes(app: Path, manifest: bytes) -> dict[str, bytes]:
    """The stand-in manifest plus the App checkout's own bootstrap scripts, under the public paths."""
    routes = {MANIFEST_ROUTE: manifest}
    needed = "/install.ps1" if sys.platform == "win32" else "/install"
    for route in BOOTSTRAP_ROUTES:
        source = app / "public" / route.lstrip("/")
        if source.is_file():
            routes[route] = source.read_bytes()
    if needed not in routes:
        raise RuntimeError(f"The App checkout has no public{needed} to serve")
    return routes


def page_routes(dist: Path) -> tuple[dict[str, Path], dict[str, str]]:
    """The candidate's hosted build under its public paths, plus the workspace entry; and their content types.

    The stand-in manifest and the bootstrap scripts are laid over these by the caller, so the page reads the
    same ``/install-manifest.json`` Runtime fetches.
    """
    index = dist / "index.html"
    build = dist / "build.json"
    if not index.is_file() or not build.is_file():
        raise RuntimeError(f"{dist} is not a hosted App build (index.html and build.json are required)")
    routes, types = {}, {}
    for file in sorted(dist.rglob("*")):
        if not file.is_file() or file.is_symlink():
            continue
        route = "/" + file.relative_to(dist).as_posix()
        routes[route] = file
        if file.suffix.lower() in MIME_TYPES:
            types[route] = MIME_TYPES[file.suffix.lower()]
    for route in ("/", PAGE_ENTRY_ROUTE):
        routes[route], types[route] = index, MIME_TYPES[".html"]
    return routes, types


def door_origin(door_base: str) -> str:
    parts = urlsplit(door_base)
    return f"{parts.scheme}://{parts.hostname}:{parts.port}"


# The open page must show the finished update ("Astrid updated: Release N → Release M") this soon after the
# status route reports it terminal, with no reload and no click from the journey (UR-15k).
LIVE_RESULT_SECONDS = 30


class LiveResult:
    """The browser still open on the page after the click, waiting for it to show the update's result."""

    def __init__(self, process: subprocess.Popen, directory: Path, result_path: Path, evidence: Path, timeout: float):
        self.process, self.directory, self.result_path = process, directory, result_path
        self.evidence, self.timeout = evidence, timeout

    def wait(self) -> dict:
        """The browser's final result, once it saw the page's live result or gave up."""
        try:
            self.process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        try:
            result = json.loads(self.result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            result = {"error": "the browser wrote no final result"}
        _keep_button_evidence(self.evidence, result)
        self.close()
        return result

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.wait()
        shutil.rmtree(self.directory, ignore_errors=True)


def _keep_button_evidence(evidence: Path, result: dict) -> None:
    try:
        (evidence / "update-button.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n",
                                                     encoding="utf-8")
    except OSError:
        pass


def click_update_now(*, app: Path, door_base: str, spki: str, evidence: Path, timeout: float = 300,
                     node: str | None = None, live_timeout: float = 0) -> dict:
    """Open the hosted page at ``https://astrid.haus`` in Chromium and click its own Update now.

    Returns what the browser saw: the POST it sent to the door and the door's answer. With ``live_timeout``
    the browser stays open on the page after the click, and the result carries ``"live"``: a
    :class:`LiveResult` whose ``wait()`` gives the moment the page showed the update's result by itself.
    """
    directory = Path(tempfile.mkdtemp(prefix="astrid-journey-button-"))
    config_path, result_path = directory / "config.json", directory / "result.json"
    clicked_path = directory / "clicked.json"
    config = {"app": str(app), "origin": ORIGIN, "entry": PAGE_ENTRY, "spki": spki,
              "door": door_origin(door_base), "updatePath": UPDATE_PATH, "agentMark": PAGE_AGENT_MARK,
              "timeoutMs": int(timeout * 1000), "result": str(result_path),
              "screenshot": str(evidence / "update-button.png")}
    if live_timeout:
        config.update(clicked=str(clicked_path), liveTimeoutMs=int(live_timeout * 1000))
    config_path.write_text(json.dumps(config), encoding="utf-8")
    node = node or os.environ.get("ASTRID_CI_NODE") or shutil.which("node") or "node"
    # The browser must reach only the intercepted host and the loopback door, never a proxy.
    env = {key: value for key, value in os.environ.items() if key.lower() not in PROXY_VARIABLES}
    output = (directory / "node.log").open("w", encoding="utf-8")
    process = subprocess.Popen([node, str(BUTTON_SCRIPT), str(config_path)], stdout=output, stderr=subprocess.STDOUT,
                               text=True, env=env)
    output.close()
    deadline = time.monotonic() + timeout + 180
    while process.poll() is None and not clicked_path.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    waiting = process.poll() is None and clicked_path.exists()
    if not waiting and process.poll() is None:
        process.kill()
    if not waiting:
        process.wait()
    try:
        result = json.loads((clicked_path if waiting else result_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        try:
            log = (directory / "node.log").read_text(encoding="utf-8")
        except OSError:
            log = ""
        result = {"error": "the browser wrote no result: " + log.strip()[-600:]}
    _keep_button_evidence(evidence, result)
    if (process.returncode if not waiting else 0) or result.get("error"):
        if waiting:
            process.kill()
            process.wait()
        shutil.rmtree(directory, ignore_errors=True)
        raise RuntimeError(f"The page's Update now was not clicked through (stage {result.get('stage')!r}): "
                           f"{result.get('error') or process.returncode}")
    if waiting:
        result["live"] = LiveResult(process, directory, result_path, evidence, live_timeout + 120)
    else:
        shutil.rmtree(directory, ignore_errors=True)
    return result


def live_result(live: "LiveResult", terminal_at: float) -> dict:
    """The page showed the finished update by itself within LIVE_RESULT_SECONDS of the terminal status, or raise."""
    final = live.wait()
    seen = final.get("live") if isinstance(final.get("live"), dict) else None
    if not seen or not isinstance(seen.get("seen_at"), (int, float)):
        raise RuntimeError("The open page never showed the update's result by itself: "
                           + str(final.get("live_error") or final.get("error") or "no result")[:400])
    after = seen["seen_at"] / 1000 - terminal_at
    if after > LIVE_RESULT_SECONDS:
        raise RuntimeError(f"The open page showed the update's result {after:.0f}s after it finished, "
                           f"not within {LIVE_RESULT_SECONDS}s")
    return {"text": seen.get("text"), "seconds_after_terminal": round(after, 1),
            "journey_reloads": 0, "page_navigations": seen.get("navigations")}


def accepted_click(result: dict) -> dict:
    """The receipt's request from the browser's own observation; raises unless the door accepted exactly one POST."""
    request = result.get("request") if isinstance(result.get("request"), dict) else {}
    body = (result.get("response") or {}).get("body")
    body = body if isinstance(body, dict) else {}
    excerpt = json.dumps(result.get("response"))[:400]
    if (request.get("method") != "POST" or request.get("path") != UPDATE_PATH or request.get("status") != 202
            or body.get("accepted") is not True or body.get("state") != "accepted"):
        raise RuntimeError(f"The door did not accept the page's Update request: {excerpt}")
    operation = body.get("operation_id")
    if not isinstance(operation, str) or not re.fullmatch(r"[0-9a-f]{32}", operation):
        raise RuntimeError(f"The page's accepted Update has no operation id: {excerpt}")
    if result.get("posts") != 1:
        raise RuntimeError(f"The page sent {result.get('posts')!r} Update requests, not one")
    return {"method": "POST", "path": UPDATE_PATH, "origin": request.get("origin"),
            "content_length": request.get("content_length"), "headers": list(request.get("headers") or []),
            "authorization": request.get("authorization"), "cookie": request.get("cookie"),
            "status": request["status"], "operation_id": operation}


def run_hosted_update(*, python: str | os.PathLike, app: Path, manifest: bytes, door_base: str, root: Path,
                      evidence: Path, timeout: float | None = None, intercept=Intercept, tick=None,
                      dist: Path | None = None, click=click_update_now) -> dict:
    """Send the hosted page's Update request and wait for it to finish; returns the evidence record.

    With ``dist`` (the candidate's ``npm run build:hosted`` output) the intercept also serves the page at
    ``https://astrid.haus`` and a browser clicks its own Update now; without it the journey sends the
    page's request itself. Raises when the door refuses, the stand-in is not what Runtime fetched, the
    update does not end as ``updated``, or the runner cannot be restored.
    """
    timeout = float(os.environ.get("ASTRID_CI_HOSTED_UPDATE_TIMEOUT", "1800")) if timeout is None else timeout
    routes, types = ({}, {}) if dist is None else page_routes(dist)
    routes.update(release_routes(app, manifest))
    bootstrap = "/install.ps1" if sys.platform == "win32" else "/install"
    if dist is None:
        record: dict = {"route": "hosted-button", "host": HOST, "driver": "door-request",
                        "request": {"method": "POST", "path": UPDATE_PATH, "origin": ORIGIN, "content_length": 0,
                                    "headers": list(SENT_HEADERS), "authorization": False, "cookie": False}}
        stand_in = intercept(routes, python=python)
    else:
        record = {"route": "hosted-button", "host": HOST, "driver": "app-button", "request": {}}
        stand_in = intercept(routes, python=python, types=types)
    failed = True
    opened: list = []
    try:
        with stand_in:
            mark = len(stand_in.hits())
            try:
                if dist is None:
                    accepted = post_update(door_base)
                    record["request"].update(status=accepted["status"], operation_id=accepted["operation_id"])
                else:
                    seen = click(app=app, door_base=door_base, spki=stand_in.leaf_spki, evidence=evidence,
                                 live_timeout=timeout)
                    live = seen.pop("live", None)
                    if live is not None:
                        opened.append(live)
                    record["request"] = accepted_click(seen)
                    record["page"] = {"entry": PAGE_ENTRY, "offered": True, "clicked": True,
                                      "progress_shown": seen.get("page_progress") is True,
                                      "build_sha256": _sha256(dist / "build.json")}
                observed = {} if tick is None else {"tick": tick}
                try:
                    record["final"] = wait_terminal(door_base, record["request"]["operation_id"], timeout=timeout,
                                                    **observed)
                    terminal_at = time.time()
                    if opened:
                        # The page that sent Update now, still open and never reloaded by the journey.
                        record["page"]["live_result"] = live_result(opened.pop(), terminal_at)
                except RuntimeError:
                    if tick is not None:
                        tick(None, final=True)  # Keep the last observation of the wait before the failure.
                    raise
            finally:
                hits = [hit for hit in stand_in.hits()[mark:] if hit["status"] == 200]
                served = [hit for hit in hits if not hit.get("page")]
                watched = (MANIFEST_ROUTE, *BOOTSTRAP_ROUTES)
                record["served"] = {route: sum(1 for hit in served if hit["path"] == route)
                                    for route in routes if route in watched}
                if dist is not None:
                    record.setdefault("page", {})["served_manifest"] = sum(
                        1 for hit in hits if hit.get("page") and hit["path"] == MANIFEST_ROUTE)
            if not record["served"][MANIFEST_ROUTE] or not record["served"][bootstrap]:
                raise RuntimeError("Runtime did not fetch the stand-in release from the intercepted host: "
                                   + json.dumps(record["served"]))
        failed = False
    finally:
        for live in opened:
            live.close()
        record["intercept_reverted"] = bool(stand_in.reverted.get("clean"))
        capture_update_evidence(root, evidence, failed=failed)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    hosts = sub.add_parser("hosts")
    hosts.add_argument("action", choices=("add", "remove"))
    trust = sub.add_parser("trust")
    trust.add_argument("action", choices=("add", "remove"))
    trust.add_argument("--ca")
    server = sub.add_parser("serve")
    for flag in ("cert", "key", "routes", "log", "ready", "stop"):
        server.add_argument("--" + flag, required=True)
    server.add_argument("--types")
    server.add_argument("--port", type=int, default=443)
    server.add_argument("--max-seconds", type=int, default=3600)
    sub.add_parser("revert", help="remove the hosts mapping and CA trust if a run left them behind")
    args = parser.parse_args(argv)
    if args.command == "revert":
        failures = revert()
        print(json.dumps({"clean": not failures, "problems": failures}))
        return 1 if failures else 0
    return _internal(args)


if __name__ == "__main__":
    sys.exit(main())
