"""Pinokio lifecycle adapters for the shared installer transaction."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .core import (
    Installer,
    RuntimeOperationError,
    Version,
    absolute,
    current,
    inspect_schema,
    read_json,
    recover_transaction,
    switch,
    update_required,
)
from .launcher import publish_runtime_runner
from .service import LABEL, installer_lock_path

# Run with the Version's interpreter in isolated mode. Backend.stage installs
# Runtime from a local source directory non-editably; PEP 610 records that
# directory even though imports live in this interpreter's site-packages.
_PINOKIO_RUNTIME_QUALIFICATION = """
import importlib
import importlib.metadata
import json
import sys
import sysconfig
from pathlib import Path

def qualified():
    runtime = importlib.import_module('astrid_runtime.bootstrap')
    capability = getattr(runtime, 'PINOKIO_STOP_HANDOFF_VERSION', None)
    if type(capability) is not int or capability != 1:
        return False
    profile = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    checkout = Path(profile['runtime_checkout']).resolve()
    environment = Path(profile['runtime_environment']).resolve()
    if environment != Path(sys.prefix).resolve():
        return False
    module = Path(runtime.__file__).resolve()
    if module.parent.parent == checkout:
        return True
    distribution = importlib.metadata.distribution('astrid-runtime')
    site = Path(distribution.locate_file('')).resolve()
    sites = {Path(sysconfig.get_path(name)).resolve() for name in ('purelib', 'platlib')}
    if site not in sites or not site.is_relative_to(environment):
        return False
    entry = Path('astrid_runtime/bootstrap.py')
    if entry not in (distribution.files or ()):
        return False
    if module != Path(distribution.locate_file(entry)).resolve():
        return False
    origin = json.loads(distribution.read_text('direct_url.json') or '{}')
    return (isinstance(origin, dict) and origin.get('url') == checkout.as_uri()
            and origin.get('dir_info') == {})

try:
    accepted = qualified()
except (ImportError, OSError, ValueError, KeyError, TypeError):
    accepted = False
if not accepted:
    sys.exit('The selected Runtime cannot safely hand off Pinokio Stop. '
             'Update the Runtime through a qualified release first.')
"""


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


def selection(root: Path, backend, *, allow_transaction: bool = False) -> tuple[Version, Path, dict]:
    marker, recorded = read_json(root / "install.json"), backend.service.recorded()
    if (marker.get("owner") != LABEL or marker.get("managed_by") != "pinokio"
            or recorded.get("owner") != LABEL or recorded.get("managed_by") != "pinokio"
            or recorded.get("program_root") != str(root) or recorded.get("uninstalled")
            or marker.get("data_root") != recorded.get("data_root")):
        raise ValueError("App updates require the selected, existing Pinokio installation.")
    transaction = read_json(root / "update-transaction.json")
    if (transaction and transaction.get("step") not in {"complete", "rolled_back"}
            and not allow_transaction):
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

    def mode(self) -> str:
        if hasattr(self, "_selected_mode"):
            return self._selected_mode
        # Older backend-only installations predate this launcher receipt.
        receipt = read_json(self.root.parent / "install-mode.json")
        if not receipt:
            self._selected_mode = "backend"
            return self._selected_mode
        if receipt.get("schema_version") != 1 or receipt.get("mode") not in {"backend", "local"}:
            raise ValueError("The Pinokio launch choice is invalid; repair the launcher first.")
        self._selected_mode = receipt["mode"]
        return self._selected_mode

    def node(self, version: Version) -> Path:
        node = absolute(version.env["ASTRID_NODE_EXECUTABLE"])
        if not node.is_relative_to(version.root) or not node.is_file():
            raise ValueError("The selected app has no owned, pinned Node executable.")
        return node

    def validate(self, version: Version) -> None:
        # Exercise the canonical launcher's actual installation validation for
        # each version without changing the on-disk current pointer.
        mode = self.mode()
        if mode == "local" and not (self.root.parent / "editor" / "scripts" / "dev-local-workspace.mjs").is_file():
            raise ValueError("Astrid's editor files are missing; repair the Pinokio installation first.")
        script = (
            "import fs from 'node:fs'; import path from 'node:path'; "
            "const [module,root,version,editor,mode]=process.argv.slice(1); "
            "const {installation,commands}=await import(module); "
            "const installed=installation(root,{filesystem:{...fs,readFileSync(file,...args){"
            "return path.resolve(file)===path.join(root,'current') ? version : fs.readFileSync(file,...args);"
            "}}}); commands(root,editor,mode,installed);"
        )
        self.backend.run([self.node(version), "--input-type=module", "-e", script,
                          self.session.as_uri(), self.root, version.root.name,
                          self.root.parent / "editor", mode], timeout=30,
                         clean_bun_env=True)
        # Both activation and rollback must invalidate a completed Stop before
        # launching another owner. Qualify the selected interpreter's actual
        # Runtime, rather than the candidate controller's imported package.
        self.backend.run([version.python, "-I", "-c", _PINOKIO_RUNTIME_QUALIFICATION, version.profile],
                         timeout=30, clean_bun_env=True)

    def stop(self, version: Version, data: Path) -> None:
        # Stop the selected Runtime under its durable fence first. Pinokio's
        # authenticated controller shutdown runs only after that handoff has
        # completed and the Runtime fence/mutex have been released; its session
        # must never issue a nested `down` while the fence is held.
        self.backend.stop(version, data)

        def stop_controller() -> None:
            try:
                self.backend.run([self.node(version), self.lifecycle, "stop", self.root],
                                 timeout=50, clean_bun_env=True)
            except subprocess.CalledProcessError:
                # C1 has already stopped Runtime. Its session can finish and
                # remove the controller between the CLI's read and POST. Only
                # disappearance admits that race; a remaining/replacement
                # controller still requires a successful authenticated stop.
                if (self.root / "pinokio-control.json").exists():
                    raise
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
            # C1 has already completed. Preserve the controller failure as
            # downstream context so its details cannot masquerade as a
            # pre-destruction lifecycle refusal.
            raise RuntimeOperationError(str(exc), details={
                "cause_details": getattr(exc, "details", None),
            }) from exc

    def start(self, version: Version, data: Path, realm_id: str) -> None:
        # The existing session selects current/version.json and passes its exact
        # source profile to the foreground Runtime. It owns its whole process tree.
        env = {key: value for key, value in os.environ.items()
               if key not in {"NODE_OPTIONS", "NODE_PATH", "PYTHONPATH", "PYTHONHOME"}}
        mode = self.mode()
        log = self.root / "app-update-session.log"
        session_script = (
            f"import {{installation,runSession}} from {json.dumps(self.session.as_uri())};\n"
            f"const root={json.dumps(str(self.root))};\n"
            f"const editor={json.dumps(str(self.root.parent / 'editor'))};\n"
            "const installed=installation(root);\n"
        )
        session_script += (
            f"runSession({{action:'serve',root,editor,mode:{json.dumps(mode)},installed,"
            f"prepareEditor:{str(mode == 'local').lower()}}})"
            ".catch(error=>{console.error(`Astrid: ${error.message}`);process.exitCode=1;});\n"
        )
        with log.open("ab") as stream:
            offset = stream.tell()
            process = subprocess.Popen(
                [str(self.node(version)), "--input-type=module", "-e", session_script],
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
        if mode == "local":
            self.backend.verify(version, data, realm_id, timeout=180, hosted=False)
        else:
            self.backend.verify(version, data, realm_id, timeout=180)
        if process.poll() is not None:
            raise ValueError(f"The Pinokio supervisor exited; see {log}.")


def _recover_app_locked(root: Path, backend, *, lifecycle=None, say=print) -> bool:
    """Recover Pinokio's shared journal while its operation lock is held."""
    pending = read_json(root / "update-transaction.json")
    if not pending or pending.get("step") in {"complete", "rolled_back"}:
        return False
    context = pending.get("transaction_context")
    if isinstance(context, dict) and context.get("owner") == "local-update":
        raise ValueError("Use Astrid's local launcher to recover the interrupted app update.")
    lock = installer_lock_path(backend.service.home, backend.service.system)
    selected, data, _identity = selection(root, backend, allow_transaction=True)
    if absolute(pending["data_root"]) != data:
        raise ValueError("The interrupted app update workspace does not match the selected installation.")
    previous = Version.load(pending["old"])
    candidate = Version.load(pending["candidate"]) if pending.get("candidate") else None
    if selected not in (previous, candidate):
        raise ValueError("The interrupted app update does not match the selected app version.")
    lifecycle = lifecycle or PinokioLifecycle(root, backend)
    lifecycle.validate(previous)  # Recovery must be able to relaunch the retained app.
    # Interrupted older controllers may have left pre-transaction admission.
    # Publish before recovery's first C1 stop, while the operation lock is held.
    publish_runtime_runner(root)
    installer = Installer(root, data, backend, say=say, managed_by="pinokio")

    def recover_selection(previous: Version, candidate: Version | None) -> None:
        with locked(lock, timeout=10):
            selected = current(root)
            if selected == candidate:
                switch(root, previous)
            elif selected != previous:
                raise ValueError("Another operation selected a different app; refusing recovery.")
            # A prior recovery may have published the old pointer and
            # crashed before updating its selection identity.
            backend.persist_identity(previous)

    recover_transaction(
        root, data, backend,
        stop=lifecycle.stop, switch_old=recover_selection,
        activate_old=lambda previous, workspace, realm: lifecycle.start(
            previous, workspace, realm["realm_id"]),
        restore_workspace=pending.get("transaction_kind") != "app", say=say,
        save=lambda step, **details: installer._save_update(
            pending, step, **details),
    )
    return True


def recover_app(root: Path, backend, *, lifecycle=None, say=print) -> bool:
    """Recover Pinokio's journal without requiring a manifest or uv executable."""
    with locked(root / ".app-update.lock"):
        return _recover_app_locked(root, backend, lifecycle=lifecycle, say=say)


def update_app(root: Path, backend, manifest: dict, *, lifecycle=None, say=print) -> None:
    """Backward-compatible strict no-migration adapter to the C2 transaction."""
    return update_pinokio(root, backend, manifest, lifecycle=lifecycle, say=say,
                          force=True, no_migration=True)


def update_pinokio(root: Path, backend, manifest: dict, *, lifecycle=None, say=print,
                   force: bool = False, no_migration: bool = False) -> None:
    """Apply C2 release dispatch and transaction using the retained session owner."""
    lock = installer_lock_path(backend.service.home, backend.service.system)
    with locked(root / ".app-update.lock"):
        _recover_app_locked(root, backend, lifecycle=lifecycle, say=say)
        old, data, identity = selection(root, backend)
        lifecycle = lifecycle or PinokioLifecycle(root, backend)
        lifecycle.validate(old)  # The rollback version must remain launchable.
        realm = backend.workspace(old, data)
        prior_schema = inspect_schema(backend, old, realm) if no_migration else None
        # C2b treats equal/older input as repair until manifest trust lands.
        repair = not update_required("update" if force else "install", manifest, old,
                                     managed_by="pinokio")
        say("Preparing the new app while Astrid keeps running.")

        def prepare() -> Version:
            candidate = old if repair else backend.stage(manifest, root, data)
            backend.validate_candidate(candidate, data, realm)
            lifecycle.validate(candidate)
            if no_migration and inspect_schema(backend, candidate, realm) != prior_schema:
                raise ValueError("The storage layout changed. Choose Pinokio's Update button to migrate it.")
            if selection(root, backend) != (old, data, identity):
                raise ValueError("The selected installation changed while preparing the update.")
            selected_realm = backend.workspace(old, data)
            if any(selected_realm[key] != realm[key] for key in ("realm_id", "realm_root")):
                raise ValueError("The selected workspace changed while preparing the update.")
            # Ordinary Start must enforce this transaction even if interrupted
            # after migration, with the retained version still selected.
            publish_runtime_runner(root)
            say("Restarting Astrid with the prepared app.")
            return candidate

        def select(candidate: Version) -> None:
            with locked(lock, timeout=10):
                if selection(root, backend, allow_transaction=True) != (old, data, identity):
                    raise ValueError("The selected installation changed before activation.")
                backend.persist_identity(old)
                switch(root, candidate)
                backend.persist_identity(candidate)

        def select_old(previous: Version, candidate: Version | None) -> None:
            selected = current(root)
            if selected not in (old, candidate):
                raise ValueError("Another operation selected a different app; refusing to replace it.")
            if selected == candidate:
                with locked(lock, timeout=10):
                    if current(root) != candidate:
                        raise ValueError("The app selection changed during rollback.")
                    switch(root, previous)
                    backend.persist_identity(previous)

        def activate(candidate: Version, workspace: Path, selected_realm: dict) -> None:
            if not no_migration:
                backend.register(candidate, workspace)
            lifecycle.start(candidate, workspace, selected_realm["realm_id"])
            if not no_migration:
                try:
                    backend.sync_skills(candidate)
                except Exception:  # noqa: BLE001 - same best-effort setup as C2
                    say("Couldn't add Astrid's skill to your agents; see the log.")

        def migrate(candidate: Version, workspace: Path, _realm: dict) -> None:
            backend.upgrade(candidate, workspace)
            # Runtime upgrade owns a temporary daemon, stopped by C1 before
            # the retained foreground session is admitted.
            backend.stop(candidate, workspace)

        installer = Installer(root, data, backend, say=say, managed_by="pinokio")
        installer._run_update_transaction(
            old, realm, prepare=prepare, stop=lifecycle.stop,
            switch_candidate=select, switch_old=select_old,
            activate=activate,
            activate_old=lambda previous, workspace, selected_realm: lifecycle.start(
                previous, workspace, selected_realm["realm_id"]),
            backup=backend.backup if not no_migration and not repair else None,
            migrate=migrate if not no_migration and not repair else None,
            restore_workspace=not no_migration and not repair,
            kind="app" if no_migration or repair else "migration",
        )
        say("Astrid is updated and running.")
