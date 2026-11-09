"""JSON-lines adapter for the fully local App's shared installer update.

The local launcher owns the source checkout and editor process. This module
owns the installed Runtime transaction and asks that launcher to perform the
small App-specific steps through a synchronous hook protocol.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from pathlib import Path
from typing import Any, TextIO

from .backend import Backend
from .choices import resolve_choices
from .core import (
    TERMINAL_STEPS,
    Installer,
    RuntimeOperationError,
    Version,
    absolute,
    current,
    install_release,
    is_lifecycle_refusal,
    owned_by_local_update,
    pending_transaction,
    read_json,
    recover_transaction,
    separate_roots,
    switch,
    update_required,
)
from .service import LABEL, default_uv, installer_lock_path
from .tools import validate_manifest

_HOOKS = {
    "stop-editor", "move-checkout", "start-editor", "verify-editor",
    "restore-checkout", "release-app", "pinokio-start",
}
_SHA = re.compile(r"[0-9a-f]{40}")
_APP_PATHS = (
    "cwd", "worktree", "buildDir", "dependenciesDir", "preparationRoot",
)


class HookClient:
    """Request a parent-launcher operation and wait for its matching reply."""

    def __init__(self, app: dict, stdin: TextIO, stdout: TextIO):
        self.app, self.stdin, self.stdout = app, stdin, stdout
        self.next_id = 1

    def call(self, name: str, version: Version | None, realm: dict,
             *, rollback: bool | None = None) -> None:
        if name not in _HOOKS:
            raise ValueError("Unsupported local-update hook.")
        request_id = self.next_id
        self.next_id += 1
        request = {
            "hook": name, "id": request_id, "app": self.app,
            "version": version.document() if version is not None else None,
            "realm": realm,
        }
        if rollback is not None:
            request["rollback"] = rollback
        self.stdout.write(json.dumps(request, separators=(",", ":")) + "\n")
        self.stdout.flush()
        line = self.stdin.readline()
        if not line:
            raise ValueError(f"{name}: launcher closed the update protocol.")
        try:
            reply = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name}: launcher returned invalid JSON.") from exc
        if not isinstance(reply, dict) or reply.get("id") != request_id:
            raise ValueError(f"{name}: launcher reply did not match the request.")
        if reply.get("ok") is True:
            return
        detail = reply.get("error")
        if not isinstance(detail, str) or not detail.strip():
            detail = "launcher refused the operation"
        step, separator, message = detail.partition(":")
        if not separator or not step.strip() or not message.strip():
            step, message = name, detail
        else:
            step, message = step.strip(), message.strip()
        raise LocalUpdateHookError(step, message)


class LocalUpdateHookError(ValueError):
    def __init__(self, step: str, message: str):
        super().__init__(f"{step}: {message}")
        self.step = step


class LocalUpdateStepError(RuntimeError):
    def __init__(self, step: str, message: str):
        super().__init__(f"{step}: {message}")
        self.step = step


class RecoveredEarlierUpdate(ValueError):
    """An update found an interrupted local update and restored the version before it."""


class LocalUpdateRefusal(ValueError):
    """Nothing was changed; ``code`` is an ``outcome.REASONS`` key. Exits 3 (not updated)."""

    def __init__(self, step: str, code: str, message: str):
        super().__init__(message)
        self.step, self.code = step, code


STORAGE_MOVE_REFUSAL = ("Astrid did not update: an earlier storage move on this computer is not "
                        "finished, so nothing was changed. Run the install line once to finish it, "
                        "then update again.")


def _tag_step(exc: Exception, step: str) -> Exception:
    """Add a machine-readable step while retaining the original exception."""
    label = getattr(exc, "step", None) or step
    message = str(exc)
    if not message.startswith(f"{label}:"):
        message = f"{label}: {message}"
    try:
        setattr(exc, "step", label)
        if str(exc) != message:
            exc.args = (message,)
        return exc
    except (AttributeError, TypeError):
        wrapped = LocalUpdateStepError(label, str(exc))
        wrapped.__cause__ = exc
        return wrapped


def _at_step(step: str, operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except Exception as exc:
        tagged = _tag_step(exc, step)
        if tagged is exc:
            raise
        raise tagged from exc


@contextlib.contextmanager
def _backend_step(backend, method: str, step: str):
    original = getattr(backend, method)

    def tagged(*args, **kwargs):
        return _at_step(step, original, *args, **kwargs)

    setattr(backend, method, tagged)
    try:
        yield
    finally:
        setattr(backend, method, original)


def validate_app_context(value: object) -> dict:
    """Validate durable checkout/build recovery authority from the launcher."""
    if not isinstance(value, dict):
        raise ValueError("The local launcher state is invalid.")
    app = dict(value)
    for key in ("oldSha", "targetSha", "mainSha"):
        if not isinstance(app.get(key), str) or not _SHA.fullmatch(app[key]):
            raise ValueError("The local launcher commit identity is invalid.")
    if app["oldSha"] == app["targetSha"]:
        raise ValueError("The local launcher has no App update to apply.")
    release = app.get("release")
    if type(release) is not int or release < 0:
        raise ValueError("The local launcher release is invalid.")
    if type(app.get("launcherChanged")) is not bool:
        raise ValueError("The local launcher update state is invalid.")
    if (type(app.get("oldBuildExisted")) is not bool
            or type(app.get("oldDependenciesExisted")) is not bool):
        raise ValueError("The prior App resource state is invalid.")
    if (not isinstance(app.get("oldAppBuild"), str) or not _SHA.fullmatch(app["oldAppBuild"])
            or type(app.get("oldAppRelease")) is not int or app["oldAppRelease"] < 0
            or app.get("previousMode") not in {"dev", "preview"}):
        raise ValueError("The prior App identity and launch mode are invalid.")
    for key in _APP_PATHS:
        raw = app.get(key)
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"The local launcher {key} is missing.")
        app[key] = str(absolute(raw))
    if app.get("previousPreviewDirectory") is not None:
        app["previousPreviewDirectory"] = str(absolute(app["previousPreviewDirectory"]))
    preparation = Path(app["preparationRoot"])
    checkout = Path(app["cwd"])
    if (not preparation.name.startswith("astrid-app-update-") or preparation == checkout
            or checkout in preparation.parents or preparation in checkout.parents
            or Path(app["worktree"]) != preparation / "checkout"
            or Path(app["buildDir"]) != Path(app["worktree"]) / "dist"
            or Path(app["dependenciesDir"]) != Path(app["worktree"]) / "node_modules"):
        raise ValueError("The prepared App files are outside their owned preparation directory.")
    if not isinstance(app.get("manifest"), dict):
        raise ValueError("The target install manifest is missing.")
    app["manifest"] = dict(app["manifest"])
    if app["release"] != install_release(app["manifest"]):
        raise ValueError("The App and Runtime release identities do not match.")
    return app


def _backend(root: Path) -> Backend:
    from runtime_protocol import platform

    log = platform.log_dir() / "install.log"
    platform.private_directory(log.parent)
    uv = default_uv()
    backend = Backend(root, log, uv, say=lambda message: print(message, file=sys.stderr),
                      managed_by=read_json(root / "install.json").get("managed_by"))
    backend.service.choices = resolve_choices(
        saved=backend.service.recorded().get("choices"), existing_install=True,
    )
    from .installation import adopt_if_handed_off

    # After a finished storage move the live Runtime runs under the R1 label;
    # never operate R0's retained definition from the local update.
    return adopt_if_handed_off(backend, root)


def _finish_storage_move(root: Path, backend: Backend) -> Backend:
    """Normalise a finished R0 to R1 handoff once, as the installer does, or refuse.

    Runs inside the installer lock, before any selection is read: until the
    handoff is normalised the marker and selection still name R0's data root,
    and an update read from them would back up, migrate and start R0's data.
    A handoff that is not finished, or that cannot be normalised safely, is
    refused with nothing changed; the install line finishes it.
    """
    import os

    from .boundary_handoff import normalise_completed
    from .installation import Installation, adopt_if_handed_off, handoff_journal

    service = backend.service
    try:
        state = Installation.resolve(service.home, system=service.system, root=root,
                                     probe=lambda *_args: None)
    except Exception as exc:  # noqa: BLE001 - an unreadable install is refused, never guessed
        if not os.path.lexists(handoff_journal(service.home, service.system)):
            return backend  # no storage move; the selection check reports what is wrong
        raise LocalUpdateRefusal("normalise-handoff", "needs-repair", STORAGE_MOVE_REFUSAL) from exc
    if state.kind not in {"handoff", "handoff-rolled-back"}:
        return backend
    if state.phase != "complete" or backend.managed_by == "pinokio":
        raise LocalUpdateRefusal("normalise-handoff", "needs-repair", STORAGE_MOVE_REFUSAL)
    try:
        normalise_completed(backend)
    except Exception as exc:  # noqa: BLE001 - normalisation validates before it writes
        refusal = LocalUpdateRefusal("normalise-handoff", "needs-repair", STORAGE_MOVE_REFUSAL)
        refusal.add_note(str(exc))
        raise refusal from exc
    # Now a direct install: operate the R1 label, never R0's retained definition.
    return adopt_if_handed_off(backend, root)


def _selection(root: Path, backend: Backend) -> tuple[Version, Path, dict]:
    marker = read_json(root / "install.json")
    recorded = backend.service.recorded()
    if (marker.get("owner") != LABEL or marker.get("data_root") is None
            or recorded.get("owner") != LABEL or recorded.get("program_root") != str(root)
            or recorded.get("uninstalled") or recorded.get("data_root") != marker.get("data_root")
            or recorded.get("managed_by") != marker.get("managed_by")):
        raise ValueError("The selected Astrid installation is invalid.")
    data = absolute(marker["data_root"])
    if not separate_roots(root, data):
        raise ValueError("Program files and the workspace must be separate.")
    old = current(root)
    if (old is None or not old.python.is_file()
            or old.root.parent != root / "versions"):
        raise ValueError("The installed Astrid Runtime is incomplete.")
    if backend.source_checkout_ownership(data, old):
        raise ValueError("This Astrid runs from source checkouts; update those instead.")
    realm = backend.workspace(old, data)
    try:
        recorded_realm_root_value = recorded.get("realm_root")
        active_realm_root_value = realm.get("realm_root")
        if not isinstance(recorded_realm_root_value, str) or not isinstance(active_realm_root_value, str):
            raise ValueError("workspace root is missing")
        recorded_realm_root = absolute(recorded_realm_root_value)
        active_realm_root = absolute(active_realm_root_value)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("The selected workspace record is invalid.") from exc
    if (not recorded.get("realm_id") or not recorded.get("realm_root")
            or recorded["realm_id"] != realm.get("realm_id")
            or recorded_realm_root != active_realm_root):
        raise ValueError("The selected workspace does not match the installation record.")
    identity = {key: recorded.get(key) for key in (
        "owner", "managed_by", "program_root", "data_root", "realm_id", "realm_root",
    )}
    identity["install_marker"] = {key: marker.get(key) for key in ("owner", "managed_by", "data_root")}
    return old, data, identity


def _assert_selection(root: Path, backend: Backend, expected_versions: tuple[Version, ...],
                      data: Path, identity: dict, realm: dict) -> None:
    selected, selected_data, selected_identity = _selection(root, backend)
    if selected not in expected_versions or selected_data != data or selected_identity != identity:
        raise ValueError("The selected installation changed while preparing the local update.")
    current_realm = backend.workspace(selected, selected_data)
    current_root = current_realm.get("realm_root")
    expected_root = realm.get("realm_root")
    if not isinstance(current_root, str) or not isinstance(expected_root, str):
        raise ValueError("The selected workspace changed while preparing the local update.")
    if (current_realm.get("realm_id") != realm.get("realm_id")
            or absolute(current_root) != absolute(expected_root)):
        raise ValueError("The selected workspace changed while preparing the local update.")


def _recover(root: Path, backend: Backend, hooks: HookClient, *, say) -> bool:
    transaction = read_json(root / "update-transaction.json")
    if not transaction or transaction.get("step") in TERMINAL_STEPS:
        return False
    owner = transaction.get("transaction_context")
    if not owned_by_local_update(transaction):
        return False
    if transaction.get("transaction_kind") not in {"app", "migration"}:
        raise LocalUpdateStepError("recover-journal", "The local update journal kind is invalid.")
    if not isinstance(owner, dict):
        raise LocalUpdateStepError("recover-context", "The local update journal context is invalid.")
    app = _at_step("recover-context", validate_app_context, owner.get("app"))
    hooks.app = app
    previous = _at_step("recover-selection", Version.load, transaction["old"])
    _selected, data, identity = _at_step("recover-selection", _selection, root, backend)
    candidate_value = transaction.get("candidate")
    if candidate_value is not None and not isinstance(candidate_value, dict):
        raise LocalUpdateStepError("recover-selection", "The interrupted local update candidate is invalid.")
    candidate = (_at_step("recover-selection", Version.load, candidate_value)
                 if candidate_value is not None else None)
    if current(root) not in (previous, candidate):
        raise LocalUpdateStepError(
            "recover-selection", "The interrupted local update does not match the selected Runtime.")
    realm = transaction.get("realm")
    if not isinstance(realm, dict):
        raise LocalUpdateStepError("recover-selection", "The interrupted local update workspace is invalid.")

    def stop(version: Version, workspace: Path) -> None:
        _at_step("preflight-stop", backend.preflight_stop, workspace)
        hooks.call("stop-editor", version, realm)
        try:
            _at_step("stop-runtime", backend.stop, version, workspace)
        except RuntimeOperationError as exc:
            if is_lifecycle_refusal(exc):
                try:
                    hooks.call("start-editor", version, realm, rollback=True)
                    hooks.call("verify-editor", version, realm, rollback=True)
                except Exception as restore_error:  # noqa: BLE001 - preserve the structured Runtime refusal
                    exc.add_note(f"Restoring the editor after the busy refusal also failed: {restore_error}")
            raise

    def select_old(old_version: Version, new_version: Version | None) -> None:
        selected = current(root)
        if selected not in (old_version, new_version):
            raise LocalUpdateStepError(
                "recover-selection", "Another operation selected a different Runtime; refusing recovery.")
        _at_step("recover-selection", _assert_selection, root, backend,
                 tuple(v for v in (old_version, new_version) if v), data, identity, realm)
        hooks.call("restore-checkout", old_version, realm)
        def publish_old():
            _at_step("recover-selection", _assert_selection, root, backend,
                     tuple(v for v in (old_version, new_version) if v), data, identity, realm)
            if selected == new_version:
                _at_step("select-previous", switch, root, old_version)
            _at_step("select-previous", backend.persist_identity, old_version)
        _at_step("select-previous", _publish_selection, backend, publish_old)

    def activate_old(version: Version, workspace: Path, selected_realm: dict) -> None:
        _at_step("activate-previous-runtime", _activate_backend, backend, version,
                 workspace, selected_realm, hooks)
        hooks.call("start-editor", version, selected_realm, rollback=True)
        hooks.call("verify-editor", version, selected_realm, rollback=True)

    installer = Installer(root, data, backend, say=say)
    with _backend_step(backend, "restore", "restore-workspace"):
        _at_step("recover-transaction", recover_transaction,
                 root, data, backend, stop=stop, switch_old=select_old,
                 activate_old=activate_old,
                 restore_workspace=transaction.get("transaction_kind") == "migration", say=say,
                 save=lambda step, **details: installer._save_update(transaction, step, **details))
    hooks.call("release-app", previous, realm)
    return True


def _verify_selected_identity(backend: Backend, version: Version, data: Path,
                              expected_manifest: dict | None = None) -> None:
    """Bind the live owner to the selected release and its pinned sources.

    Health identifies an installed Runtime by release/manager, while the
    staged version's retained manifest records its exact source pins. Checking
    both ties the ready owner to this candidate without trusting App metadata.
    """
    manifest = read_json(version.root / "manifest.json")
    if not manifest or install_release(manifest) != version.release:
        raise ValueError("The selected Runtime's retained manifest does not match its version record.")
    source_pins = {}
    for component in ("agent", "runtime"):
        source = manifest.get(component)
        sha = source.get("sha") if isinstance(source, dict) else None
        if not isinstance(sha, str) or not _SHA.fullmatch(sha):
            raise ValueError(f"The selected Runtime's {component} source pin is invalid.")
        source_pins[component] = sha
    if expected_manifest is not None:
        if manifest.get("release", 0) != expected_manifest.get("release", 0):
            raise ValueError("The selected Runtime release does not match the target manifest.")
        for component in ("agent", "runtime"):
            expected = expected_manifest.get(component)
            if not isinstance(expected, dict) or source_pins[component] != expected.get("sha"):
                raise ValueError(f"The selected Runtime {component} pin does not match the target manifest.")

    try:
        status = _at_step("verify-runtime-identity", backend.runtime,
                          version, data, "status", timeout=3)
    except Exception as exc:
        raise ValueError(f"The selected Runtime identity could not be read: {exc}") from exc
    health = status.get("health") if isinstance(status, dict) else None
    identity = health.get("identity") if isinstance(health, dict) else None
    if (not isinstance(identity, dict) or set(identity) != {"release", "managed_by"}
            or type(identity.get("release")) is not int or identity["release"] != version.release
            or identity["managed_by"] != backend.managed_by):
        raise ValueError("The ready Runtime identity does not match the selected release and manager.")


def _activate_backend(backend: Backend, version: Version, data: Path, realm: dict,
                      hooks: HookClient, expected_manifest: dict | None = None) -> None:
    _at_step("register-runtime", backend.register, version, data)
    if backend.managed_by == "pinokio":
        hooks.call("pinokio-start", version, realm)
        _at_step("verify-backend", backend.verify, version, data, realm["realm_id"], hosted=False)
    else:
        _at_step("start-runtime", backend.start, version, data)
        _at_step("verify-runtime", backend.verify, version, data, realm["realm_id"])
    _at_step("verify-runtime-identity", _verify_selected_identity,
             backend, version, data, expected_manifest)


def _publish_selection(backend: Backend, operation) -> None:
    """Pinokio's live session owns the broad lock; acquire it only for pointer writes."""
    if backend.managed_by != "pinokio":
        return operation()
    from .app_update import locked
    lock = installer_lock_path(backend.service.home, backend.service.system)
    with locked(lock, timeout=10):
        return operation()


def run_update(root: Path, backend: Backend, manifest: dict, app: dict,
               hooks: HookClient, *, say=print) -> dict:
    old, data, identity = _at_step("validate-selection", _selection, root, backend)
    if not isinstance(manifest, dict):
        raise LocalUpdateStepError("validate-manifest", "The target install manifest is invalid.")
    manifest = _at_step("validate-manifest", validate_manifest, manifest)
    repair = not _at_step("select-operation", update_required,
                          "install", manifest, old, managed_by=backend.managed_by)
    realm = _at_step("validate-selection", backend.workspace, old, data)
    context = {"owner": "local-update", "app": app}
    installer = Installer(root, data, backend, say=say, managed_by=backend.managed_by)
    candidate_holder: dict[str, Version] = {}
    busy_handoff_restored = False

    def prepare() -> Version:
        candidate = old if repair else _at_step("stage-runtime", backend.stage, manifest, root, data)
        _at_step("validate-candidate", backend.validate_candidate, candidate, data, realm)
        _at_step("validate-selection", _assert_selection, root, backend, (old,), data, identity, realm)
        candidate_holder["value"] = candidate
        return candidate

    def stop(version: Version, workspace: Path) -> None:
        nonlocal busy_handoff_restored
        _at_step("preflight-stop", backend.preflight_stop, workspace)
        hooks.call("stop-editor", version, realm)
        try:
            _at_step("stop-runtime", backend.stop, version, workspace)
        except RuntimeOperationError as exc:
            if is_lifecycle_refusal(exc):
                try:
                    hooks.call("start-editor", version, realm, rollback=True)
                    hooks.call("verify-editor", version, realm, rollback=True)
                except Exception as restore_error:  # noqa: BLE001 - preserve the structured Runtime refusal
                    exc.add_note(f"Restoring the editor after the busy refusal also failed: {restore_error}")
                else:
                    busy_handoff_restored = True
            raise

    def select(candidate: Version) -> None:
        _at_step("validate-selection", _assert_selection, root, backend,
                 (old,), data, identity, realm)
        hooks.call("move-checkout", candidate, realm)
        def publish_candidate():
            _at_step("validate-selection", _assert_selection, root, backend,
                     (old,), data, identity, realm)
            _at_step("select-candidate", backend.persist_identity, old)
            _at_step("select-candidate", switch, root, candidate)
            _at_step("select-candidate", backend.persist_identity, candidate)
        _at_step("select-candidate", _publish_selection, backend, publish_candidate)

    def select_old(previous: Version, candidate: Version | None) -> None:
        selected = current(root)
        if selected not in (old, candidate):
            raise LocalUpdateStepError(
                "validate-selection", "Another operation selected a different Runtime; refusing rollback.")
        _at_step("validate-selection", _assert_selection, root, backend,
                 tuple(v for v in (old, candidate) if v), data, identity, realm)
        hooks.call("restore-checkout", previous, realm)
        def publish_old():
            _at_step("validate-selection", _assert_selection, root, backend,
                     tuple(v for v in (old, candidate) if v), data, identity, realm)
            if selected == candidate:
                _at_step("select-previous", switch, root, previous)
            _at_step("select-previous", backend.persist_identity, previous)
        _at_step("select-previous", _publish_selection, backend, publish_old)

    def activate(candidate: Version, workspace: Path, selected_realm: dict) -> None:
        _at_step("activate-runtime", _activate_backend, backend, candidate, workspace,
                 selected_realm, hooks, None if repair else manifest)
        hooks.call("start-editor", candidate, selected_realm)
        hooks.call("verify-editor", candidate, selected_realm)
        try:
            backend.sync_skills(candidate)
        except Exception:  # noqa: BLE001 - C2 keeps skill setup best effort
            say("Couldn't add Astrid's skill to your agents; see the log.")

    def activate_old(previous: Version, workspace: Path, selected_realm: dict) -> None:
        _at_step("activate-previous-runtime", _activate_backend, backend, previous,
                 workspace, selected_realm, hooks)
        hooks.call("start-editor", previous, selected_realm, rollback=True)
        hooks.call("verify-editor", previous, selected_realm, rollback=True)

    def backup(previous, workspace, selected_realm):
        return _at_step("backup-workspace", backend.backup, previous, workspace, selected_realm)

    def migrate(candidate_version, workspace, _realm):
        _at_step("upgrade-runtime", backend.upgrade, candidate_version, workspace)
        _at_step("stop-runtime", backend.stop, candidate_version, workspace)

    try:
        with _backend_step(backend, "restore", "restore-workspace"), \
                _backend_step(backend, "finish", "finish-runtime"):
            _at_step("run-transaction", installer._run_update_transaction,
                     old, realm, prepare=prepare, stop=stop, switch_candidate=select,
                     switch_old=select_old, activate=activate, activate_old=activate_old,
                     backup=backup if not repair else None,
                     migrate=migrate if not repair else None,
                     restore_workspace=not repair, kind="app" if repair else "migration",
                     transaction_context=context)
    except Exception as exc:
        if not getattr(exc, "step", None):
            exc = _tag_step(exc, "run-transaction")
        if is_lifecycle_refusal(exc) and busy_handoff_restored:
            # The shared writer must have durably closed the staged journal
            # before App preparation resources can be released.
            try:
                journal = read_json(root / "update-transaction.json")
            except Exception as journal_error:  # noqa: BLE001 - preserve the structured Runtime refusal
                exc.add_note(f"Could not confirm the busy refusal journal was terminal: {journal_error}")
            else:
                if (isinstance(journal, dict) and journal.get("step") == "complete"
                        and journal.get("outcome") == "stop_refused"):
                    try:
                        hooks.call("release-app", old, realm)
                    except Exception as cleanup_error:  # noqa: BLE001 - preserve the structured Runtime refusal
                        exc.add_note(f"Releasing the prepared App after the busy refusal also failed: {cleanup_error}")
        raise
    candidate = candidate_holder["value"]
    # Retire staged App resources only after the shared journal is terminal.
    hooks.call("release-app", candidate, realm)
    return {"status": "updated", "release": candidate.release,
            "targetSha": app["targetSha"], "launcherChanged": app["launcherChanged"]}


def execute(action: dict, root: Path, *, stdin: TextIO, stdout: TextIO, say=print) -> dict:
    if action.get("action") not in {"recover", "update"}:
        raise LocalUpdateStepError("validate-request", "The local update action is invalid.")
    if action["action"] == "recover":
        pending = pending_transaction(root)
        if pending and not owned_by_local_update(pending):
            raise LocalUpdateStepError(
                "recover-journal", "Use the owning installer to recover the interrupted update first.")
    backend = _backend(root)
    from .app_update import locked
    # Match existing C2 serialization. Installer-managed updates hold the
    # same per-user lock for the full operation; a Pinokio session already
    # owns it and takes it narrowly around pointer publication.
    if backend.managed_by == "pinokio":
        with locked(root / ".app-update.lock"):
            return _execute_locked(action, root, backend, stdin=stdin, stdout=stdout, say=say)
    # The ordinary installer takes this per-user lock before .app-update.lock;
    # keep that order to avoid deadlocking a concurrent CLI invocation.
    lock_path = installer_lock_path(backend.service.home, backend.service.system)
    with locked(lock_path):
        backend.service.activation_lock_path = absolute(lock_path)
        with locked(root / ".app-update.lock"):
            return _execute_locked(action, root, backend, stdin=stdin, stdout=stdout, say=say)


def _execute_locked(action: dict, root: Path, backend: Backend, *,
                    stdin: TextIO, stdout: TextIO, say=print) -> dict:
    hooks: HookClient
    if action["action"] == "recover":
        pending = read_json(root / "update-transaction.json")
        if (pending and pending.get("step") not in TERMINAL_STEPS
                and not owned_by_local_update(pending)):
            raise LocalUpdateStepError(
                "recover-journal", "Use the owning installer to recover the interrupted update first.")
        context = pending.get("transaction_context", {})
        app_context = _at_step("recover-context", validate_app_context, context["app"]) if (
            owned_by_local_update(pending)
        ) else {}
        hooks = HookClient(app_context, stdin, stdout)
        did_recover = _at_step("recover-transaction", _recover, root, backend, hooks, say=say)
        return {"status": "recovered" if did_recover else "no-recovery"}
    app = _at_step("validate-app-context", validate_app_context, action.get("app"))
    manifest = action.get("manifest")
    if not isinstance(manifest, dict):
        raise LocalUpdateStepError("validate-manifest", "The target install manifest is invalid.")
    if app.get("manifest") != manifest:
        raise LocalUpdateStepError("validate-manifest", "The App and Runtime update manifests do not match.")
    backend = _finish_storage_move(root, backend)
    hooks = HookClient(app, stdin, stdout)
    if pending_transaction(root):
        if _at_step("recover-transaction", _recover, root, backend, hooks, say=say):
            raise RecoveredEarlierUpdate("An interrupted local update was recovered; retry the update.")
        raise ValueError("Use the owning installer to recover the interrupted update first.")
    return run_update(root, backend, manifest, app, hooks, say=say)


RESTORED_AT_START = "Astrid finished an interrupted update from earlier: it went back to {release} and kept your projects."


class _LocalRun:
    """Settle one local recover/update run through the installer's own receipt.

    The same ``outcome.settle`` and ``last-run.json`` as the CLI: the reply's
    ``outcome`` and the process exit code come from the observed live Runtime,
    never from the transaction's own belief. A recover that finds nothing to
    recover changed nothing and leaves the previous receipt alone.
    """

    def __init__(self):
        self.root: Path | None = None
        self.action: dict = {}
        self.before = None
        self.transaction_id = None
        self.target: int | None = None
        self.log: Path | None = None
        self.protected: list[Path] = []
        self.operation_id: str | None = None
        self.started_at: int | None = None

    def _resolve(self):
        from .installation import Installation

        return Installation.resolve(Path.home(), system=sys.platform, root=self.root)

    def observe(self, root: Path, action: dict) -> None:
        import secrets
        import time

        from .outcome import RECORD, clear_record

        self.root, self.action = root, action
        self.operation_id, self.started_at = secrets.token_hex(16), int(time.time())
        manifest = action.get("manifest")
        try:
            self.target = install_release(manifest) if isinstance(manifest, dict) else None
        except (KeyError, TypeError, ValueError):
            self.target = None
        try:
            self.before = self._resolve()
        except Exception:  # noqa: BLE001 - observation only; settle re-resolves
            self.before = None
        pending = read_json(root / "update-transaction.json")
        self.transaction_id = pending.get("operation_id") if isinstance(pending, dict) else None
        try:
            self.protected, self.log = self._log()
        except Exception:  # noqa: BLE001 - no usable log: Details points at this window
            self.protected, self.log = [root], None
        if self.log is not None and (action.get("action") == "update" or pending_transaction(root)):
            # This run can change the installation: the previous receipt must not outlive it.
            clear_record(self.log.with_name(RECORD))

    def _log(self) -> tuple[list[Path], Path | None]:
        """install.log only when it cannot write into a selected root (the installer's rule)."""
        from .cli import _prepare_log

        root = self.root
        assert root is not None
        protected = [root]
        marker = read_json(root / "install.json")
        for key in ("data_root", "r1_data_root", "retained_r0_data_root"):
            if isinstance(marker.get(key), str) and marker[key]:
                protected.append(absolute(marker[key]))
        if isinstance(marker.get("data_root"), str) and marker["data_root"]:
            protected.append(absolute(marker["data_root"] + "-r1"))
        for path in (getattr(self.before, "active_data_root", None),
                     getattr(self.before, "retained_r0_data_root", None)):
            if path is not None:
                protected.append(absolute(path))
        try:
            from runtime_protocol import platform

            log_path = platform.log_dir() / "install.log"
        except Exception:  # noqa: BLE001 - no usable log: Details points at this window
            return protected, None
        return protected, _prepare_log(log_path, *protected)

    def _rolled_back(self) -> bool:
        assert self.root is not None
        pending = read_json(self.root / "update-transaction.json")
        return pending.get("step") == "rolled_back" and pending.get("operation_id") != self.transaction_id

    def finish(self, *, result: dict | None = None, failure: BaseException | None = None):
        """Return ``(outcome, lines)``, or ``(None, [])`` when the run changed nothing."""
        from dataclasses import replace

        from .outcome import RECORD, Request, settle, write_record

        if self.root is None or self.action.get("action") not in {"recover", "update"}:
            return None, []
        if failure is None and (result or {}).get("status") == "no-recovery":
            return None, []
        recovered = (failure is None and (result or {}).get("status") == "recovered") or isinstance(
            failure, RecoveredEarlierUpdate)
        at_start = self.action.get("action") == "recover" and self.action.get("purpose") != "update"
        if isinstance(failure, LocalUpdateRefusal):
            # Refused before any change: not updated, with the refusal's own reason (exit 3).
            request = Request("update", self.target, failure.code, refused=True)
            failure = None
        elif recovered:
            # As the CLI: a recovery during an update is not-updated (exit 3);
            # the launcher's start-time recovery is a repair (exit 0).
            request = Request("repair" if at_start else "update", None if at_start else self.target,
                              "recovered-previous-version")
            failure = None
        else:
            request = Request("update", self.target)
        try:
            rolled_back = failure is not None and self._rolled_back()
        except (OSError, ValueError):
            rolled_back = False
        outcome = settle(request, self.before, self._resolve, failure=failure, rolled_back=rolled_back)
        app_value = self.action.get("app")
        app = app_value if isinstance(app_value, dict) else {}
        old_editor = app.get("oldAppRelease")
        if (outcome.kind == "already-current" and type(old_editor) is int
                and old_editor != outcome.target):
            # The Runtime was already at the target but the editor moved to it.
            outcome = replace(outcome, kind="updated", start=old_editor)
        outcome = replace(outcome, log=str(self.log) if self.log is not None else None)
        if self.log is not None:
            write_record(self.log.with_name(RECORD), outcome, *self.protected,
                         run={"operation_id": self.operation_id, "started_at": self.started_at,
                              "surface": "local-update"})
        lines = outcome.headline()
        if recovered and at_start and outcome.exit_code == 0:
            release = "an unknown release" if outcome.still is None else f"Release {outcome.still}"
            lines = [RESTORED_AT_START.format(release=release)]
        return outcome, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m astrid.installer.local_update")
    parser.add_argument("--root", required=True, type=Path)
    args = parser.parse_args(argv)
    run = _LocalRun()
    reply: dict[str, Any]
    code: int
    failure: Exception | None = None
    try:
        root = absolute(args.root)
        line = sys.stdin.readline()
        if not line:
            raise ValueError("The local update request is missing.")
        action = json.loads(line)
        if not isinstance(action, dict):
            raise ValueError("The local update request must be an object.")
        run.observe(root, action)
        result = execute(action, root, stdin=sys.stdin, stdout=sys.stdout,
                         say=lambda message: print(message, file=sys.stderr))
        reply, code, failure = {"result": result}, 0, None
    except LocalUpdateRefusal as exc:
        # Nothing was changed: the receipt settles it as "not updated" (exit 3) with its reason.
        reply = {"error": str(exc), "step": exc.step, "code": exc.code, "details": None}
        code, failure, result = 3, exc, None
    except Exception as exc:  # noqa: BLE001 - protocol boundary returns every failure as JSON
        reply = {"error": str(exc), "step": getattr(exc, "step", None),
                 "details": getattr(exc, "details", None)}
        code, failure, result = 1, exc, None
    try:
        outcome, lines = run.finish(result=result, failure=failure)
    except Exception as exc:  # noqa: BLE001 - an unsettled run is never reported as success
        print(f"Could not settle the local update: {exc}", file=sys.stderr)
        outcome, lines, code = None, [], 1
    if outcome is not None:
        # The receipt decides the exit code (0 only when the live release is the
        # target); ``lines`` are the terminal's plain lines, Next and Details included.
        reply["outcome"] = outcome.record()
        reply["lines"] = lines
        code = outcome.exit_code
    print(json.dumps(reply, separators=(",", ":")), flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
