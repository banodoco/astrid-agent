#!/usr/bin/env python3
"""UR-12 probe: can a runner map astrid.haus to a local HTTPS server it alone trusts?

Throwaway CA and leaf certificate are generated per run.  The probe maps the
host name in the OS hosts file, optionally installs the CA into the runner's
own trust store, serves HTTPS on 127.0.0.1:443 and records what each client
sees.  Everything it changes is reverted in a finally block.  It touches no
product code and holds no secret; the CA private key never leaves the temp dir.

Needs root/Administrator (hosts file, port 443, trust store).  Prints one JSON
result.  Exit 0 only if the mapping and the explicit-CA request both resolve to
the local server; system-store trust is reported per client, not required.
"""
from __future__ import annotations

import datetime
import http.server
import ipaddress
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

HOST = "astrid.haus"
MARK = "# ur-12-probe"
BODY = b"ur-12-local-stand-in"


def make_certs(d: Path) -> tuple[Path, Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    now = datetime.datetime.now(datetime.timezone.utc)

    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca = (x509.CertificateBuilder().subject_name(name("ur-12 probe CA")).issuer_name(name("ur-12 probe CA"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder().subject_name(name(HOST)).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    pem = serialization.Encoding.PEM
    ca_p, leaf_p, key_p = d / "ca.pem", d / "leaf.pem", d / "leaf.key"
    ca_p.write_bytes(ca.public_bytes(pem))
    leaf_p.write_bytes(leaf.public_bytes(pem))
    key_p.write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return ca_p, leaf_p, key_p


def hosts_path() -> Path:
    if os.name == "nt":
        return Path(os.environ["SystemRoot"]) / "System32" / "drivers" / "etc" / "hosts"
    return Path("/etc/hosts")


def run(cmd: list[str]) -> dict:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return {"rc": p.returncode, "out": (p.stdout + p.stderr).strip()[-400:]}


def trust(action: str, ca: Path) -> dict:
    """Install (action='add') or remove the CA in the runner's system store."""
    if sys.platform.startswith("linux"):
        dest = Path("/usr/local/share/ca-certificates/ur-12-probe.crt")
        if action == "add":
            shutil.copy(ca, dest)
        else:
            dest.unlink(missing_ok=True)
        return run(["update-ca-certificates"] + ([] if action == "add" else ["--fresh"]))
    if sys.platform == "darwin":
        if action == "add":
            return run(["security", "add-trusted-cert", "-d", "-r", "trustRoot", "-k", "/Library/Keychains/System.keychain", str(ca)])
        return run(["security", "delete-certificate", "-c", "ur-12 probe CA", "/Library/Keychains/System.keychain"])
    if action == "add":
        return run(["certutil", "-addstore", "-f", "Root", str(ca)])
    return run(["certutil", "-delstore", "Root", "ur-12 probe CA"])


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *a):
        pass


def fetch(ctx: ssl.SSLContext | None) -> dict:
    try:
        with urllib.request.urlopen(f"https://{HOST}/probe", context=ctx, timeout=15) as r:
            return {"ok": r.read() == BODY, "status": r.status}
    except Exception as e:  # report, never raise
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}


def curl_native() -> dict:
    curl = shutil.which("curl.exe" if os.name == "nt" else "curl")
    if not curl:
        return {"ok": False, "error": "no curl"}
    p = subprocess.run([curl, "-sS", "--max-time", "15", f"https://{HOST}/probe"], capture_output=True, text=True)
    return {"ok": p.returncode == 0 and p.stdout == BODY.decode(), "rc": p.returncode, "err": p.stderr.strip()[-200:]}


def main() -> int:
    res: dict = {"platform": sys.platform, "python": sys.version.split()[0], "steps": {}}
    tmp = Path(tempfile.mkdtemp(prefix="ur12-probe-"))
    hp, original = hosts_path(), None
    trusted = False
    srv = None
    try:
        ca, leaf, key = make_certs(tmp)
        original = hp.read_bytes()
        with hp.open("ab") as f:
            f.write(f"\n127.0.0.1 {HOST} {MARK}\n".encode())
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(leaf, key)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 443), Handler)
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        import socket
        res["steps"]["resolves_to"] = socket.gethostbyname(HOST)
        # negative control: untrusted CA must be rejected before any trust change
        res["steps"]["untrusted_default_ctx"] = fetch(None)
        # mapping + a CA given explicitly to the client (no system change)
        res["steps"]["explicit_ca_ctx"] = fetch(ssl.create_default_context(cafile=str(ca)))
        # system-store trust: what an unmodified client would see
        res["steps"]["trust_add"] = trust("add", ca)
        trusted = True
        res["steps"]["system_trust_python_default_ctx"] = fetch(None)
        res["steps"]["system_trust_native_curl"] = curl_native()
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"[:400]
    finally:
        if srv:
            srv.shutdown()
        if trusted:
            res["steps"]["trust_remove"] = trust("remove", tmp / "ca.pem")
        if original is not None:
            hp.write_bytes(original)
        shutil.rmtree(tmp, ignore_errors=True)
    s = res["steps"]
    res["mapping_works"] = s.get("resolves_to") == "127.0.0.1" and s.get("explicit_ca_ctx", {}).get("ok", False)
    res["untrusted_rejected"] = not s.get("untrusted_default_ctx", {}).get("ok", True)
    res["system_trust_works"] = bool(s.get("system_trust_python_default_ctx", {}).get("ok") or s.get("system_trust_native_curl", {}).get("ok"))
    print(json.dumps(res, indent=2))
    return 0 if res["mapping_works"] and "error" not in res else 1


if __name__ == "__main__":
    sys.exit(main())
