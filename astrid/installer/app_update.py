"""Update Pinokio's program without migrating an already-current workspace."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .core import RuntimeOperationError, Version, absolute, current, read_json, switch
from .service import LABEL, installer_lock_path


@contextlib.contextmanager
def locked(path: Path, *, timeout: float = 0):
    from runtime_protocol import platform

    platform.private_directory(path.parent)
    with path.open("a+b") as stream:
        deadline = time.monotonic() + timeout
        while True:
            try:
                platform.lock_file(stream.fileno(), blocking=False)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValueError("Another Astrid operation still owns the installation lock.") from None
                time.sleep(0.1)
        try:
            yield
        finally:
            platform.unlock_file(stream.fileno())


def selection(root: Path, backend) -> tuple[Version, Path, dict]:
    marker, recorded = read_json(root / "install.json"), backend.service.recorded()
    if (marker.get("owner") != LABEL or marker.get("managed_by") != "pinokio"
            or recorded.get("owner") != LABEL or recorded.get("managed_by") != "pinokio"
            or recorded.get("program_root") != str(root) or recorded.get("uninstalled")
            or marker.get("data_root") != recorded.get("data_root")):
        raise ValueError("App updates require the selected, existing Pinokio installation.")
    transaction = read_json(root / "update-transaction.json")
    if transaction and transaction.get("step") not in {"complete", "rolled_back"}:
        raise ValueError("Finish recovering the interrupted migration update before updating the app.")
    old = current(root)
    if old is None or not old.python.is_file():
        raise ValueError("The installed app is incomplete; use the regular installer to repair it.")
    data = absolute(recorded["data_root"])
    if data == root or data in root.parents or root in data.parents:
        raise ValueError("Program files and the workspace must be separate.")
    # Foreground PID changes are expected; workspace and manager changes are not.
    identity = {key: recorded.get(key) for key in (
        "owner", "managed_by", "program_root", "data_root", "realm_id", "realm_root", "identity")}
    return old, data, identity


def inspect_schema(backend, version: Version, realm: dict) -> dict:
    """The selected package validates actual storage layout, using read-only SQLite."""
    script = (
        "import json,sys; from pathlib import Path; "
        "from runtime_protocol.upgrade import inspect_canonical_schema, USER_APP_STATE_TARGET_SCHEMA_VERSION; "
        "value=inspect_canonical_schema(Path(sys.argv[1]), expected_realm_id=sys.argv[2]); "
        "print(json.dumps({'identity':value,'target':USER_APP_STATE_TARGET_SCHEMA_VERSION}))"
    )
    value = json.loads(backend.run(
        [version.python, "-I", "-c", script, realm["realm_root"], realm["realm_id"]],
        extra_env=version.env, timeout=120,
    ))
    if value["identity"]["version"] != value["target"]:
        raise ValueError("This release needs a workspace migration. Use the regular Pinokio update.")
    return value


class PinokioLifecycle:
    def __init__(self, root: Path, backend):
        self.root, self.backend = root, backend
        if root.name != "astrid" or root.parent.name != "app":
            raise ValueError("The app update requires Pinokio's app/astrid installation layout.")
        self.repo = absolute(root.parent.parent)
        self.session = absolute(self.repo / "pinokio" / "session.mjs")
        self.lifecycle = absolute(self.repo / "pinokio" / "lifecycle.mjs")
        entry = root / ("run.cmd" if sys.platform == "win32" else "run")
        if not self.session.is_file() or not self.lifecycle.is_file() or not entry.is_file():
            raise ValueError("The canonical Pinokio launcher is missing; update the launcher first.")

    def node(self, version: Version) -> Path:
        node = absolute(version.env["ASTRID_NODE_EXECUTABLE"])
        if not node.is_relative_to(version.root) or not node.is_file():
            raise ValueError("The selected app has no owned, pinned Node executable.")
        return node

    def validate(self, version: Version) -> None:
        # Exercise the canonical launcher's actual installation validation for
        # each version without changing the on-disk current pointer.
        script = (
            "import fs from 'node:fs'; import path from 'node:path'; "
            "const [module,root,version]=process.argv.slice(1); "
            "const {installation}=await import(module); "
            "installation(root,{filesystem:{...fs,readFileSync(file,...args){"
            "return path.resolve(file)===path.join(root,'current') ? version : fs.readFileSync(file,...args);"
            "}}});"
        )
        self.backend.run([self.node(version), "--input-type=module", "-e", script,
                          self.session.as_uri(), self.root, version.root.name], timeout=30,
                         clean_bun_env=True)

    def stop(self, version: Version, data: Path) -> None:
        # Stop the selected Runtime under its durable fence first. Pinokio's
        # authenticated controller shutdown runs only after that handoff has
        # completed and the Runtime fence/mutex have been released; its session
        # must never issue a nested `down` while the fence is held.
        self.backend.stop(version, data)

        def stop_controller() -> None:
            self.backend.run([self.node(version), self.lifecycle, "stop", self.root],
                             timeout=50, clean_bun_env=True)
            deadline = time.monotonic() + 10
            while (self.root / "pinokio-control.json").exists():
                pid = read_json(self.root / "pinokio-control.json").get("pid")
                if isinstance(pid, int) and pid > 0:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break  # The canonical launcher removes a dead controller record.
                if time.monotonic() >= deadline:
                    raise ValueError("The Pinokio supervisor has not finished stopping.")
                time.sleep(0.1)

        try:
            stop_controller()
        except (Exception, KeyboardInterrupt) as exc:
            # Runtime has already completed its validated stop. Keep that
            # evidence explicit so the caller may recover the old app without
            # mistaking a controller-cleanup failure for a refusal.
            raise RuntimeOperationError(str(exc), stop_refused=False,
                                        stop_completed=True) from exc

    def start(self, version: Version, data: Path, realm_id: str) -> None:
        # The existing session selects current/version.json and passes its exact
        # source profile to the foreground Runtime. It owns its whole process tree.
        env = {key: value for key, value in os.environ.items()
               if key not in {"NODE_OPTIONS", "NODE_PATH", "PYTHONPATH", "PYTHONHOME"}}
        log = self.root / "app-update-session.log"
        with log.open("ab") as stream:
            offset = stream.tell()
            process = subprocess.Popen(
                [str(self.node(version)), str(self.session), "serve", str(self.root),
                 str(self.root.parent / "editor"), "backend"],
                cwd=self.repo, env=env, stdin=subprocess.DEVNULL,
                stdout=stream, stderr=subprocess.STDOUT, start_new_session=True,
            )
        deadline = time.monotonic() + 180
        with log.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            while "ASTRID_PINOKIO_READY" not in stream.readline():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise ValueError(f"The Pinokio supervisor did not become ready; see {log}.")
                time.sleep(0.1)
        self.backend.verify(version, data, realm_id, timeout=180)
        if process.poll() is not None:
            raise ValueError(f"The Pinokio supervisor exited; see {log}.")


def update_app(root: Path, backend, manifest: dict, *, lifecycle=None, say=print) -> None:
    """Stage online; switch programs only when both versions admit this exact realm."""
    lock = installer_lock_path(backend.service.home, backend.service.system)
    with locked(root / ".app-update.lock"):
        old, data, identity = selection(root, backend)
        lifecycle = lifecycle or PinokioLifecycle(root, backend)
        lifecycle.validate(old)  # Rollback must be launchable before anything stops.
        realm = backend.workspace(old, data)
        prior_schema = inspect_schema(backend, old, realm)
        say("Preparing the new app while Astrid keeps running.")
        candidate = backend.stage(manifest, root, data)
        lifecycle.validate(candidate)
        if inspect_schema(backend, candidate, realm) != prior_schema:
            raise ValueError("The storage layout changed; use the regular Pinokio update.")
        if selection(root, backend) != (old, data, identity):
            raise ValueError("The selected installation changed while preparing the update.")
        selected_realm = backend.workspace(old, data)
        if any(selected_realm[key] != realm[key] for key in ("realm_id", "realm_root")):
            raise ValueError("The selected workspace changed while preparing the update.")
        say("Restarting Astrid with the prepared app.")
        stopped = False
        try:
            try:
                lifecycle.stop(old, data)
            except (Exception, KeyboardInterrupt) as stop_failure:
                # RuntimeOperationError carries explicit handoff evidence.
                # A refusal must not trigger status probing, relaunch, or rollback.
                stopped = (isinstance(stop_failure, RuntimeOperationError)
                           and stop_failure.stop_completed)
                raise
            stopped = True
            with locked(lock, timeout=10):
                if selection(root, backend) != (old, data, identity):
                    raise ValueError("The selected installation changed before activation.")
                # Upgrade a legacy selected record under the installer lock,
                # then publish the candidate's identity with its pointer.
                backend.persist_identity(old)
                switch(root, candidate)
                backend.persist_identity(candidate)
            # The foreground launcher needs the lifetime lock itself.
            lifecycle.start(candidate, data, realm["realm_id"])
        except (Exception, KeyboardInterrupt) as failure:
            if isinstance(failure, RuntimeOperationError) and failure.stop_refused:
                raise
            if stopped:
                try:
                    selected = current(root)
                    if selected not in (old, candidate):
                        raise ValueError("Another operation selected a different app; refusing to replace it.")
                    if selected == candidate:
                        lifecycle.stop(candidate, data)
                        with locked(lock, timeout=10):
                            if current(root) != candidate:
                                raise ValueError("The app selection changed during rollback.")
                            switch(root, old)
                            backend.persist_identity(old)
                    lifecycle.start(old, data, realm["realm_id"])
                    say("The new app could not start; the previous app is running again.")
                except (Exception, KeyboardInterrupt) as recovery:
                    if isinstance(recovery, RuntimeOperationError) and recovery.stop_refused:
                        raise recovery
                    raise ValueError(f"App update failed ({failure}); restarting the previous app also failed ({recovery}).") from recovery
            raise
        say("Astrid is updated and running.")
