"""Private stopped-update lock custody shared with its foreground activation.

The custodian takes the kernel lock once. Windows byte locks cannot be handed
from the updater through Node to the foreground process, so both platforms use
the same owner: an isolated Agent child. Its updater pipe and one authenticated
Runtime connection keep that owner alive; neither release nor reacquisition is
part of activation or rollback. No ticket is written to disk.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
from pathlib import Path


def _receive(stream):
    line = stream.readline(65537)
    if not line or len(line) > 65536:
        raise ValueError("The stopped-update lock owner disconnected.")
    return json.loads(line)


def _send(stream, value):
    stream.write(json.dumps(value).encode() + b"\n")
    stream.flush()


def _same_process_identity(expected, observed):
    """Compare the immutable process incarnation rather than mutable topology."""
    return (isinstance(expected, dict) and isinstance(observed, dict)
            and all(expected.get(field) == observed.get(field)
                    for field in ("pid", "uid", "birth_id")))


def _binding(root, version, data):
    from astrid.installer.core import absolute, current, read_json
    from astrid.installer.service import launch_admission

    root, data = absolute(root), absolute(data)
    marker = read_json(root / "install.json")
    if (marker.get("owner") != "haus.astrid.runtime" or marker.get("managed_by") != "pinokio"
            or absolute(marker["data_root"]) != data or current(root) != version
            or not launch_admission(version, data, custody=True)):
        raise ValueError("The stopped update does not authorize this foreground activation.")
    journal = read_json(root / "update-transaction.json")
    # A journal phase alone never authorizes activation. The updater must
    # still own this installation's operation lock as well as its custody.
    from runtime_protocol import platform

    try:
        with (root / ".app-update.lock").open("r+b") as operation_lock:
            try:
                platform.lock_file(operation_lock.fileno(), blocking=False)
            except BlockingIOError:
                pass
            else:
                platform.unlock_file(operation_lock.fileno())
                raise ValueError("The stopped update no longer owns activation admission.")
    except FileNotFoundError:
        raise ValueError("The stopped update no longer owns activation admission.") from None
    return {"root": str(root), "data": str(data), "version": version.document(),
            "operation_id": journal["operation_id"], "step": journal["step"]}


def attach(ticket, root, version, data):
    """Consume the updater's single-use, version/phase-bound Runtime lease."""
    try:
        binding = _binding(root, version, data)
        if binding != ticket["binding"]:
            raise ValueError("The stopped-update activation identity changed.")
        receipt = json.loads((Path(root) / ".app-update-custody.json").read_text(encoding="utf-8"))
        from runtime_protocol import platform

        observed = platform.process_identity(receipt["identity"]["pid"])
        if (receipt["port"] != ticket["port"] or receipt["root"] != str(Path(root).resolve())
                or receipt["lock"] != ticket["lock"]
                or not _same_process_identity(receipt["identity"], observed)):
            raise ValueError("The stopped-update lock owner identity changed.")
        challenge = secrets.token_hex(32)
        connection = socket.create_connection(("127.0.0.1", ticket["port"]), timeout=10)
        try:
            with connection.makefile("rwb", buffering=0) as stream:
                _send(stream, {"token": ticket["token"], "binding": binding, "challenge": challenge})
                reply = _receive(stream)
                proof = hmac.new(bytes.fromhex(receipt["key"]), challenge.encode(), hashlib.sha256).hexdigest()
                if reply.get("accepted") is not True or not hmac.compare_digest(reply.get("proof", ""), proof):
                    raise ValueError("The stopped-update activation ticket was refused.")
            connection.settimeout(None)
            return connection
        except BaseException:
            connection.close()
            raise
    except (OSError, KeyError, TypeError) as exc:
        raise ValueError("The stopped-update activation ticket was refused.") from exc


def keeper(fd):
    """Keep custody only in the actual Runtime; fail closed if its owner dies."""
    original = socket.socket(fileno=fd)
    original.set_inheritable(False)
    connection = original.dup()
    original.detach()
    connection.set_inheritable(False)

    def watch():
        try:
            connection.recv(1)
        finally:
            # A lease is never intentionally closed by its custodian while the
            # Runtime is alive. Do not leave an unlocked foreground owner behind.
            os._exit(1)

    threading.Thread(target=watch, daemon=True, name="astrid-lock-custody").start()
    return connection  # Caller retains it for the inline Windows Runtime too.


class Custody:
    def __init__(self, process, root, lock):
        self.process, self.root, self.lock = process, root, Path(lock).resolve()

    def check(self):
        if self.process.poll() is not None:
            raise ValueError("The stopped-update lock owner disconnected; choose Update to recover.")

    def arm(self, version, data):
        self.check()
        binding = _binding(self.root, version, data)
        token = secrets.token_hex(32)
        if self.process.stdin is None or self.process.stdout is None:
            raise ValueError("The stopped-update lock owner disconnected.")
        _send(self.process.stdin, {"token": token, "binding": binding})
        reply = _receive(self.process.stdout)
        if reply.get("armed") is not True:
            raise ValueError("The previous foreground owner has not released update custody.")
        return {"port": reply["port"], "token": token, "binding": binding,
                "lock": str(self.lock)}

    def close(self):
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()
        # When no Runtime attached, reap promptly (including failed preparation).
        # An attached Runtime retains the custodian for its own lifetime.
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


@contextlib.contextmanager
def stopped_custody(root, backend):
    """Acquire, never probe/release, stopped admission; None means running."""
    from astrid.installer.service import installer_lock_path

    path = installer_lock_path(backend.service.home, backend.service.system)
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", str(Path(__file__).resolve()), str(path), str(root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    custody = Custody(process, root, path)
    try:
        if process.stdout is None:
            raise ValueError("The stopped-update lock owner disconnected.")
        acquired = _receive(process.stdout)
        yield custody if acquired == {"acquired": True} else None
    finally:
        custody.close()


@contextlib.contextmanager
def operation(root, backend):
    """Share one custody admission across CLI manifest work and the adapter."""
    if hasattr(backend, "_pinokio_custody"):
        yield backend._pinokio_custody
        return
    with stopped_custody(root, backend) as custody:
        if custody is None:
            from astrid.installer.app_update import qualify_running, selection

            _old, data, _identity = selection(root, backend, allow_transaction=True)
            # Recovery retains its existing phase-specific qualification after
            # stopping the selected owner; fresh updates refuse incapable owners
            # here before CLI manifest writes, even if Start won the first race.
            from astrid.installer.core import pending_transaction

            if pending_transaction(root) is None:
                qualify_running(root, backend, data)
        backend._pinokio_custody = custody
        try:
            yield custody
        finally:
            del backend._pinokio_custody


def _serve(path, root):
    # This entry point is also usable with a retained interpreter: it depends
    # only on the pinned Runtime's platform module and Python's standard library.
    from runtime_protocol import platform

    if not path.parent.exists():
        platform.private_directory(path.parent)
    with path.open("r+b" if path.exists() else "a+b") as lock:
        try:
            platform.lock_file(lock.fileno(), blocking=False)
        except BlockingIOError:
            _send(sys.stdout.buffer, {"acquired": False})
            return
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        listener.settimeout(0.2)
        receipt_path = root / ".app-update-custody.json"
        key = secrets.token_bytes(32)
        receipt = json.dumps({"root": str(root.resolve()), "lock": str(path.resolve()),
                              "port": listener.getsockname()[1], "key": key.hex(),
                              "identity": platform.process_identity(os.getpid())}).encode()
        fd, temporary = platform.private_temporary(root, ".app-update-custody.")
        temporary = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(receipt)
                stream.flush()
                os.fsync(stream.fileno())
            platform.durable_replace(temporary, receipt_path)
        finally:
            temporary.unlink(missing_ok=True)
        condition = threading.Condition()
        state = {"ticket": None, "active": False, "updater": True}

        def accept():
            while True:
                with condition:
                    if not state["updater"] and not state["active"]:
                        return
                try:
                    client, _address = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with client:
                    client.settimeout(5)
                    accepted = False
                    try:
                        with client.makefile("rwb", buffering=0) as stream:
                            request = _receive(stream)
                            with condition:
                                ticket = state["ticket"]
                                challenge = request.pop("challenge", "")
                                if (state["updater"] and not state["active"] and ticket is not None
                                        and request == ticket and isinstance(challenge, str)
                                        and len(challenge) == 64):
                                    state.update(ticket=None, active=True)
                                    accepted = True
                                proof = hmac.new(key, challenge.encode(), hashlib.sha256).hexdigest() if accepted else ""
                                _send(stream, {"accepted": accepted, "proof": proof})
                        if accepted:
                            client.settimeout(None)
                            client.recv(1)  # EOF is the actual Runtime's lifetime boundary.
                    except (OSError, ValueError):
                        pass
                    finally:
                        if accepted:
                            with condition:
                                state["active"] = False
                                condition.notify_all()

        server = threading.Thread(target=accept, daemon=True)
        server.start()
        try:
            _send(sys.stdout.buffer, {"acquired": True})
            while True:
                try:
                    ticket = _receive(sys.stdin.buffer)
                except ValueError:
                    break
                with condition:
                    # C1 may have observed process exit just before the socket
                    # reader. Bound this wait; never supersede a live lease.
                    condition.wait_for(lambda: not state["active"], timeout=10)
                    armed = not state["active"]
                    if armed:
                        state["ticket"] = ticket
                _send(sys.stdout.buffer, {"armed": armed, "port": listener.getsockname()[1]})
        finally:
            with condition:
                state.update(updater=False, ticket=None)
                condition.wait_for(lambda: not state["active"])
            listener.close()
            server.join(timeout=1)
            if receipt_path.read_bytes() == receipt:
                receipt_path.unlink()
            platform.unlock_file(lock.fileno())


if __name__ == "__main__":
    _serve(Path(sys.argv[1]), Path(sys.argv[2]))
