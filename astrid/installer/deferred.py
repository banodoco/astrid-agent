"""Durable detached-R15 continuation through the existing C2 transaction.

The journal names prepared inputs, never process authority. No background
worker, timer, old CLI stop or PID signal is used. A canonical launch calls the
retained candidate controller; only a fresh quiescent C1 fence admits C2.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from pathlib import Path

ABI = "astrid-deferred-update/v1"


def launch_continuation(root):
    """Stdlib-only bridge, embedded before importing any retained Agent code."""
    import hashlib
    import json
    import os
    import re
    import stat
    import subprocess
    from pathlib import Path

    def safe(path):
        path = Path(path)
        if (not path.is_absolute() or Path(os.path.abspath(path)) != path
                or any(part.is_symlink() for part in (path, *path.parents))):
            raise ValueError("The deferred update path is unsafe.")
        return path

    def document(path):
        path = safe(path)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError("The deferred update input is not privately owned.")
        return json.loads(path.read_text(encoding="utf-8"))

    root = safe(root)
    path = root / "update-transaction.json"
    if not os.path.lexists(path):
        return False
    transaction = document(path)
    binding = transaction.get("deferred_update")
    if not isinstance(binding, dict) or transaction.get("step") in {"complete", "rolled_back"}:
        return False
    if (binding.get("format") != "astrid-deferred-update/v1"
            or transaction.get("format") != "astrid-install-update/v1"
            or not re.fullmatch(r"[0-9a-f]{32}", transaction.get("operation_id", ""))):
        raise ValueError("The deferred update identity is invalid.")
    candidate = transaction["candidate"]
    folder = safe(candidate["root"])
    if (folder.parent != root / "versions" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", folder.name)
            or document(folder / "version.json") != candidate):
        raise ValueError("The deferred candidate changed or is outside this installation.")
    python = Path(candidate["python"])
    # The managed venv's Python may be a legitimate symlink to its interpreter.
    # Its lexical path, parent and retained version must still be exact.
    if (not python.is_absolute() or python.parent != safe(python.parent)
            or not python.is_relative_to(folder) or not python.is_file()):
        raise ValueError("The deferred interpreter is not retained by this installation.")
    for name in ("profile", "controller"):
        item = binding[name]
        source = safe(item["path"])
        if (not source.is_relative_to(folder) or not source.is_file()
                or hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]):
            raise ValueError("The deferred candidate inputs changed.")
    for item in binding["code"].values():
        source = safe(item["path"])
        if (not source.is_relative_to(folder) or not source.is_file()
                or hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]):
            raise ValueError("The retained deferred controller inputs changed.")
    if binding["profile"]["path"] != candidate["profile"]:
        raise ValueError("The deferred source profile does not match its candidate.")
    manifest = json.dumps(binding["manifest"], sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(manifest).hexdigest() != binding["manifest_sha256"]:
        raise ValueError("The deferred manifest changed.")
    env = {key: value for key, value in os.environ.items() if key not in {
        "PYTHONPATH", "PYTHONHOME", "ASTRID_WORKSPACE_ENDPOINT", "ASTRID_WORKSPACE_CREDENTIAL",
        "ASTRID_RUNTIME_SOURCE_MANIFEST", "ASTRID_RUNTIME_DATA_ROOT", "ASTRID_PACKS_PATH",
        "ASTRID_INSTALLER_OPERATION_ID", "ASTRID_INSTALL_CUSTODY",
    }}
    # The module is a fixed entry, never an executable path from the journal.
    # The operation id is a replay binding, never permission to stop an owner.
    result = subprocess.run([str(python), "-B", "-X", "utf8", "-I", "-m", "astrid.installer.deferred",
                             "--resume", str(root), transaction["operation_id"]], env=env, check=False)
    if result.returncode:
        raise ValueError("The deferred update is still pending; no old Runtime was stopped. "
                         "Let its normal lifecycle finish, then start Astrid again.")
    settled = document(path)
    if (settled.get("operation_id") != transaction["operation_id"]
            or settled.get("step") not in {"complete", "rolled_back"}):
        raise ValueError("The deferred controller did not settle its existing transaction.")
    return True


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _file(path):
    from .core import absolute
    from .systemd_stop import _file_snapshot

    return _file_snapshot(absolute(path))


def _retained(root, version, *, strict=False):
    from .core import Version, absolute, read_json, version_id

    folder = absolute(root / "versions" / version_id(version.root.name))
    if (version.root != folder or not folder.is_dir()
            or not version.python.is_absolute() or not version.python.is_relative_to(folder)
            or not version.python.is_file() or not version.profile.is_relative_to(folder)
            or not version.profile.is_file() or Version.load(read_json(folder / "version.json")) != version
            or strict and read_json(folder / "version.json") != version.document()):
        raise ValueError("The deferred version is not exactly retained by this installation.")
    return {folder / "version.json": _file(folder / "version.json"), version.profile: _file(version.profile)}


def _detached(backend, data):
    from .choices import resolve_choices
    from .service import LABEL

    service = backend.service
    record = service.recorded()
    if (record.get("owner") != LABEL or record.get("program_root") != str(backend.root)
            or record.get("data_root") != str(data)
            or os.path.lexists(service.path) or service._systemd_links()
            or (not isinstance(record.get("foreground"), dict) and resolve_choices(
                saved=record.get("choices"), existing_install=True)["autostart"])):
        raise ValueError("The deferred installation is no longer an unambiguous detached Runtime.")


def _inspection(backend, data):
    from astrid_runtime.bootstrap import inspect_upgrade_stop
    from astrid_runtime.paths import RuntimePaths
    from astrid_runtime.runtime_boundary import LocalRuntimeBoundary

    if getattr(backend, "_stop_boundary", None) is None:
        backend._stop_boundary = LocalRuntimeBoundary()
    paths = RuntimePaths.current(data_root=data)
    return paths, inspect_upgrade_stop(paths, backend._stop_boundary)


def execution_inputs():
    import importlib
    from . import backend, core, service, systemd_stop, launcher, choices, tools
    bootstrap = importlib.import_module("astrid_runtime.bootstrap")
    import runtime_protocol.lifecycle as lifecycle

    modules = {"controller": sys.modules[__name__], "backend": backend, "core": core, "service": service,
               "systemd_stop": systemd_stop, "launcher": launcher, "choices": choices, "tools": tools,
               "bootstrap": bootstrap, "lifecycle": lifecycle}
    return {name: {"path": str(Path(module.__file__).absolute()),
                   "sha256": hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()}
            for name, module in modules.items()}


def _validate_execution(binding, candidate):
    if (binding["controller"] != binding["code"]["controller"]
            or Path(sys.executable).absolute() != candidate.python or execution_inputs() != binding["code"]):
        raise ValueError("Continue through the normal launcher using the exact retained candidate controller.")


def _controller(backend, candidate):
    """Check the real installed candidate entry before accepting a request."""
    source = (
        "import json,importlib; "
        "import astrid.installer.deferred as d; b=importlib.import_module('astrid_runtime.bootstrap'); "
        "assert callable(b.admit_deferred_upgrade); "
        "print(json.dumps({'format':d.ABI,'code':d.execution_inputs()}))"
    )
    result = json.loads(backend.run([candidate.python, "-B", "-X", "utf8", "-I", "-c", source], timeout=30))
    code, own = result["code"], execution_inputs()
    if result.get("format") != ABI or set(code) != set(own):
        raise ValueError("The candidate does not contain this verified deferred-update controller.")
    for name, item in code.items():
        path = Path(item["path"])
        if (not path.is_relative_to(candidate.root)
                or hashlib.sha256(_file(path)[1]).hexdigest() != item["sha256"]
                or item["sha256"] != own[name]["sha256"]):
            raise ValueError("The candidate controller inputs differ from the preparing installer.")
    return code


def prepare(installer, old, manifest):
    """Return True only after a usable detached continuation is durable.

    The caller holds C2's installer/app locks. No accepted request exists until
    the final journal write. A failed final fence may leave isolated candidate
    files, exactly as ordinary online preparation does; selection/data/job
    inputs and normal launchers remain unchanged.
    """
    from astrid_runtime.bootstrap import admit_deferred_upgrade
    from .core import RuntimeOperationError, current, install_release, pending_transaction, read_json
    from .launcher import publish_continuation
    from .service import LABEL
    from .tools import validate_manifest

    backend, root, data = installer.backend, installer.root, installer.data
    if not backend._legacy_linux_update(old):
        return False
    try:
        with backend.legacy_update_stop(old, data):
            return False
    except RuntimeOperationError as exc:
        if not isinstance(exc.details, dict) or not exc.details.get("deferred_until_quiescent"):
            raise
    manifest = validate_manifest(manifest)
    if pending_transaction(root):
        raise ValueError("The existing update transaction must be continued before another request.")
    marker = read_json(root / "install.json")
    if (marker.get("owner") != LABEL or marker.get("data_root") != str(data)
            or marker.get("managed_by") not in {None, ""} or old.managed_by is not None):
        raise ValueError("The deferred installation ownership does not match the selected direct Runtime.")
    job = marker.get("update_job")
    if (getattr(backend, "_update_job_operation_id", None)
            or isinstance(job, dict) and job.get("state") not in {"updated", "rolled_back", "failed"}):
        raise ValueError("The existing update job owns this request; detached continuation cannot replace it.")
    paths, expected = _inspection(backend, data)
    if expected.owner is None:
        return False
    realm = {"realm_id": expected.realm_id, "realm_root": str(expected.realm_root)}
    watched = _retained(root, old)
    watched.update({path: _file(path) if os.path.lexists(path) else None for path in (
        root / "current", root / "install.json", root / "update-transaction.json", backend.service.selection,
        paths.catalog_path,
    )})
    publications = {path: path.read_bytes() if os.path.lexists(path) else None
                    for path in (paths.discovery_path, paths.instance_lock_path)}

    def validate(observed):
        _detached(backend, data)
        backend._validate_legacy_foreground(observed)
        if (current(root) != old or observed.realm_id != expected.realm_id
                or observed.realm_root != expected.realm_root or observed.catalog != expected.catalog
                or observed.bootstrap_lock != expected.bootstrap_lock
                or (observed.owner is not None and observed.owner != expected.owner)
                or any((_file(path) if os.path.lexists(path) else None) != value for path, value in watched.items())):
            raise ValueError("The deferred update selection or pending inputs changed before admission.")
        for path, original in publications.items():
            value = path.read_bytes() if os.path.lexists(path) else None
            if value != original and not (observed.owner is None and value is None):
                raise ValueError("The deferred predecessor publication changed before admission.")

    admit_deferred_upgrade(paths, backend._stop_boundary, expected, validate=validate, on_admitted=lambda: None)
    candidate = backend.stage(manifest, root, data)
    backend.validate_candidate(candidate, data, realm)
    if candidate == old or candidate.managed_by is not None or candidate.release != install_release(manifest):
        raise ValueError("The prepared deferred candidate does not match the requested release.")
    watched.update(_retained(root, candidate, strict=True))
    code = _controller(backend, candidate)
    for item in code.values():
        watched[Path(item["path"])] = _file(Path(item["path"]))
    transaction = {
        "format": "astrid-install-update/v1", "operation_id": uuid.uuid4().hex,
        "transaction_kind": "migration", "data_root": str(data), "old": old.document(),
        "candidate": candidate.document(), "realm": realm, "backup": None,
        "deferred_update": {"format": ABI, "manifest": manifest, "manifest_sha256": _digest(manifest),
            "controller": code["controller"], "code": code, "profile": {"path": str(candidate.profile),
                "sha256": hashlib.sha256(_file(candidate.profile)[1]).hexdigest()},
            "selection": backend.service.recorded(), "install": marker,
            "choices": backend.service.choices, "process_control": backend.process_control},
    }

    def commit():
        # Harmless without the journal, so interrupted publication does not
        # block ordinary R15 startup. No service definition is rewritten here.
        publish_continuation(root)
        installer._save_update(transaction, "deferred")

    _paths, final = _inspection(backend, data)
    admit_deferred_upgrade(paths, backend._stop_boundary, final, validate=validate, on_admitted=commit)
    installer.outcomes["startup"] = "update deferred until normal lifecycle restart"
    installer.say("Update prepared. Astrid will continue it on its next normal start after the old Runtime exits. "
                  "Your selected version, workspace and pending work are kept.")
    return True


class Request:
    def __init__(self, backend, transaction):
        from .core import Version, absolute, current, read_json

        self.backend, self.transaction = backend, transaction
        self.root, self.data = backend.root, absolute(transaction["data_root"])
        self.old, self.candidate = Version.load(transaction["old"]), Version.load(transaction["candidate"])
        self.binding = transaction["deferred_update"]
        _validate_execution(self.binding, self.candidate)
        if (transaction.get("format") != "astrid-install-update/v1" or transaction.get("step") not in {"deferred", "staged"}
                or transaction.get("transaction_kind") != "migration" or transaction.get("backup") is not None
                or not re.fullmatch(r"[0-9a-f]{32}", transaction.get("operation_id", ""))
                or self.binding.get("format") != ABI or current(self.root) != self.old
                or not backend._legacy_linux_update(self.old)
                or self.old.managed_by is not None or self.candidate.managed_by is not None
                or _digest(self.binding["manifest"]) != self.binding["manifest_sha256"]
                or read_json(self.root / "update-transaction.json") != transaction
                or read_json(self.root / "install.json") != self.binding["install"]
                or backend.service.recorded() != self.binding["selection"]):
            raise ValueError("The deferred request no longer matches this installation.")
        self.files = {self.root / "update-transaction.json": _file(self.root / "update-transaction.json"),
                      self.root / "install.json": _file(self.root / "install.json"),
                      backend.service.selection: _file(backend.service.selection)}
        self.files.update(_retained(self.root, self.old))
        self.files.update(_retained(self.root, self.candidate, strict=True))
        for name in ("profile", "controller"):
            item = self.binding[name]
            path = absolute(item["path"])
            if (not path.is_relative_to(self.candidate.root)
                    or hashlib.sha256(_file(path)[1]).hexdigest() != item["sha256"]):
                raise ValueError("The deferred candidate inputs changed.")
            self.files[path] = _file(path)
        if self.binding["controller"] != self.binding["code"]["controller"]:
            raise ValueError("The retained deferred controller entry changed.")
        for item in self.binding["code"].values():
            path = absolute(item["path"])
            if not path.is_relative_to(self.candidate.root) or hashlib.sha256(_file(path)[1]).hexdigest() != item["sha256"]:
                raise ValueError("The retained deferred controller inputs changed.")
            self.files[path] = _file(path)
        if self.binding["profile"]["path"] != str(self.candidate.profile):
            raise ValueError("The deferred source profile changed.")
        self.validate()

    def validate(self, inspection=None):
        from .core import current

        _detached(self.backend, self.data)
        if (current(self.root) != self.old
                or any(_file(path) != value for path, value in self.files.items())):
            raise ValueError("The deferred request or retained inputs changed before continuation.")
        if inspection is not None and (inspection.owner is not None
                or inspection.realm_id != self.transaction["realm"]["realm_id"]
                or str(inspection.realm_root) != self.transaction["realm"]["realm_root"]):
            raise ValueError("The deferred update requires independently verified quiescence of its original realm.")
        if inspection is not None:
            self.backend._validate_legacy_foreground(inspection)


def resume_locked(backend, transaction):
    """Consume under the caller's existing installer then app-update locks."""
    from astrid_runtime.bootstrap import admit_deferred_upgrade
    from .core import Installer

    if transaction.get("step") != "deferred":
        raise ValueError("This deferred continuation already entered the existing recovery policy.")
    request = Request(backend, transaction)
    control = request.binding.get("process_control")
    if control not in {None, "strict", "sandbox"}:
        raise ValueError("The deferred process-control choice is invalid.")
    if control is not None:
        from runtime_protocol import platform

        platform.configure_process_control(control)
    backend.process_control = control
    paths, observed = _inspection(backend, request.data)
    admit_deferred_upgrade(paths, backend._stop_boundary, observed, validate=request.validate,
                           on_admitted=lambda: None, require_quiescent=True)
    from .choices import resolve_choices
    backend.service.choices = resolve_choices(explicit=request.binding.get("choices"),
                                              saved=request.binding["selection"].get("choices"), existing_install=True)
    installer = Installer(backend.root, request.data, backend, say=backend.say)
    return installer._run_locked("update", request.binding["manifest"], resume=request)


def close_staged(backend, transaction, close):
    """Even pre-stop replay of this request must fence its journal write."""
    from astrid_runtime.bootstrap import admit_deferred_upgrade

    request = Request(backend, transaction)
    paths, observed = _inspection(backend, request.data)
    return admit_deferred_upgrade(paths, backend._stop_boundary, observed, validate=request.validate,
                                   on_admitted=close, require_quiescent=True)


def main(argv=None):
    from runtime_protocol import platform
    from astrid.installer.app_update import locked
    from astrid.installer.backend import Backend
    from astrid.installer.core import Installer, absolute, read_json
    from astrid.installer.service import installer_lock_path

    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3 or args[0] != "--resume" or not re.fullmatch(r"[0-9a-f]{32}", args[2]):
        raise ValueError("Invalid deferred update invocation.")
    root = absolute(args[1])
    preliminary = read_json(root / "update-transaction.json")
    from astrid.installer.core import Version
    _validate_execution(preliminary["deferred_update"], Version.load(preliminary["candidate"]))
    control = preliminary["deferred_update"].get("process_control")
    if control not in {None, "strict", "sandbox"}:
        raise ValueError("The deferred process-control choice is invalid.")
    lock_path = installer_lock_path(Path.home(), sys.platform)
    with locked(lock_path, timeout=0), locked(root / ".app-update.lock", timeout=0):
        transaction = read_json(root / "update-transaction.json")
        if transaction.get("operation_id") != args[2] or transaction.get("deferred_update", {}).get("format") != ABI:
            raise ValueError("The deferred update request was replaced.")
        if transaction.get("step") in {"complete", "rolled_back"}:
            return 0
        backend = Backend(root, platform.log_dir() / "installer.log", Path(sys.executable), process_control=control)
        backend.service.activation_lock_path = lock_path
        if transaction.get("step") == "deferred":
            resume_locked(backend, transaction)
        else:
            # An accepted continuation that already entered C2 follows the
            # existing recovery policy; never stage a second transaction.
            Installer(root, absolute(transaction["data_root"]), backend)._recover_update()
    return 0


if __name__ == "__main__":
    # Resolve the canonical installed module for its exact source receipt even
    # when Python initially evaluates these bytes as the fixed -m entry.
    from astrid.installer.deferred import main as installed_main
    raise SystemExit(installed_main())
