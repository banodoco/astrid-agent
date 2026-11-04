"""Stopped, read-only source admission and copy update recovery.

The Runtime must explicitly support read-only, non-starting adoption. No fallback
opens an R0 database or restores an image over either workspace.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

from runtime_protocol import platform

from .core import RuntimeOperationError, absolute, current, linked, read_json, separate_roots, switch

JOURNAL = "update-journal.r1.json"
QUIT = "Quit Astrid to finish updating"


def selected_data(record: dict):
    return record.get("r1_data_root") or record.get("data_root")


def data_fields(record: dict, data: Path) -> dict:
    """A legacy root is permanent, even when the active selection moves."""
    legacy = record.get("data_root")
    return {"data_root": legacy or str(data), **({"r1_data_root": str(data)} if legacy and str(data) != legacy else {})}


def _identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


def fingerprint(root: Path) -> dict:
    """Hash a stable, link-free tree using read-only, no-follow descriptors.

    atime is deliberately excluded. Replacements and mutations seen during the
    walk refuse; a second walk around adoption catches later changes.
    """
    root = absolute(root)
    entries = []
    def visit(path, relative):
        before = path.lstat()
        if linked(path) or not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)):
            raise ValueError(f"Unsafe source entry: {path}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if stat.S_ISDIR(before.st_mode):
            flags |= getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            if _identity(os.fstat(descriptor)) != _identity(before):
                raise ValueError("Previous workspace changed while being inspected.")
            if stat.S_ISDIR(before.st_mode):
                names = sorted(os.listdir(descriptor))
                entries.append((relative, "dir", _identity(before)))
                for name in names:
                    visit(path / name, f"{relative}/{name}")
                if sorted(os.listdir(descriptor)) != names:
                    raise ValueError("Previous workspace changed while being inspected.")
            else:
                digest = hashlib.sha256()
                while chunk := os.read(descriptor, 1024 * 1024):
                    digest.update(chunk)
                entries.append((relative, "file", _identity(before), digest.hexdigest()))
            if (_identity(os.fstat(descriptor)) != _identity(before)
                    or _identity(path.lstat()) != _identity(before)):
                raise ValueError("Previous workspace changed while being inspected.")
        finally:
            os.close(descriptor)
    visit(root, ".")
    return {"algorithm": "sha256-tree-v1", "sha256": hashlib.sha256(
        json.dumps(entries, separators=(",", ":")).encode()).hexdigest()}


@contextmanager
def stopped_source(backend, data: Path, realm: dict):
    """Observe ownership with the existing lock; never chmod, create or signal."""
    realm_root = absolute(realm["realm_root"])
    if not realm_root.is_relative_to(data):
        raise ValueError("The previous workspace is outside its data root.")
    lock = absolute(realm_root / "owner.lock")
    descriptor = os.open(lock, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    locked = False
    try:
        held = os.fstat(descriptor)
        if not stat.S_ISREG(held.st_mode) or _identity(held) != _identity(lock.lstat()):
            raise ValueError("The previous workspace owner lock changed.")
        try:
            platform.lock_file(descriptor, blocking=False)
            locked = True
        except OSError as exc:
            raise RuntimeOperationError(QUIT, details={"safe": False}) from exc
        for name in ("discovery.json", "instance.lock"):
            marker = read_json(absolute(data / "runtime" / name))
            if marker and backend._discovery_owner_state(marker) != "stale":
                raise RuntimeOperationError(QUIT, details={"safe": False})
        backend.service.require_stopped_for_copy(data)
        yield
        if _identity(os.fstat(descriptor)) != _identity(lock.lstat()):
            raise ValueError("The previous workspace owner lock changed.")
    finally:
        if locked:
            platform.unlock_file(descriptor)
        os.close(descriptor)


def source_workspace(backend, data: Path) -> dict:
    """Read selection identity without executing R0 or opening its SQLite DB."""
    catalog = read_json(absolute(data / "runtime" / "catalog.json"))
    realms = catalog.get("realms")
    if not isinstance(realms, list) or len(realms) != 1 or not isinstance(realms[0], dict):
        raise ValueError("The previous workspace has no unambiguous identity.")
    selected = realms[0]
    realm_id = selected.get("realm_id")
    if not isinstance(realm_id, str) or catalog.get("selected_realm_id") != realm_id:
        raise ValueError("The previous workspace selection is inconsistent.")
    realm_root = absolute(selected["data_root"])
    if realm_root == data or not realm_root.is_relative_to(data):
        raise ValueError("The previous realm must be inside the previous data root.")
    record = backend.service.recorded()
    if (record.get("owner") != "haus.astrid.runtime" or record.get("uninstalled")
            or record.get("program_root") != str(backend.root)
            or selected_data(record) != str(data)
            or record.get("realm_id", realm_id) != realm_id
            or record.get("realm_root", str(realm_root)) != str(realm_root)):
        raise ValueError("The previous service selection does not match its workspace.")
    return {"realm_id": realm_id, "realm_root": str(realm_root)}


def _saved_paths(backend):
    return (backend.service.path, backend.service.selection, backend.service.env_file,
            *(backend.root / name for name in ("install.json", "run", "run.cmd", "run_runtime.py", "run-astrid", "run-astrid.cmd", "run_astrid.py", "astrid", "astrid.cmd")))


def save_service(backend) -> list[dict]:
    saved = []
    for path in _saved_paths(backend):
        path = absolute(path)
        if path.exists():
            if not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError("The retained service or launcher is not a regular file.")
            payload = path.read_bytes()
            saved.append({"path": str(path), "bytes": base64.b64encode(payload).decode(),
                          "sha256": hashlib.sha256(payload).hexdigest(),
                          "mode": stat.S_IMODE(path.stat().st_mode)})
        else:
            saved.append({"path": str(path), "absent": True})
    return saved


def restore_service(backend, saved: list[dict]) -> None:
    """Restore only allowlisted service/launcher bytes; never auto-start R0."""
    expected = {str(absolute(path)) for path in _saved_paths(backend)}
    if (not isinstance(saved, list) or len(saved) != len(expected)
            or {entry.get("path") for entry in saved} != expected):
        raise ValueError("The retained service snapshot does not match this installation.")
    decoded = []
    for entry in saved:
        path = absolute(entry["path"])
        if entry.get("absent") is True:
            decoded.append((path, None, None))
        else:
            payload = base64.b64decode(entry["bytes"], validate=True)
            if hashlib.sha256(payload).hexdigest() != entry["sha256"]:
                raise ValueError("The retained service snapshot is corrupt.")
            decoded.append((path, payload, entry["mode"]))
    for path, payload, mode in decoded:
        if payload is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = platform.private_temporary(path.parent, ".r1-rollback-")
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, mode)
                platform.durable_replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)


def prepare_candidate_files(backend, candidate, new_data, saved):
    """Retain exact operation-owned bytes before publishing any mutable file."""
    from .core import install_launcher
    from .service import render
    staged = candidate.root / "copy-launchers"
    staged.mkdir()
    install_launcher(staged)
    expected = []
    for entry in saved:
        path = Path(entry["path"])
        replacement = dict(entry)
        if path == backend.service.path:
            payload = render(backend.service.system, candidate, new_data, backend.service.log,
                             env_file=backend.service.env_file).encode()
        elif path == backend.root / "install.json":
            marker = json.loads(base64.b64decode(entry["bytes"]))
            payload = (json.dumps({**marker, "r1_data_root": str(new_data)}, indent=2, sort_keys=True) + "\n").encode()
        elif path == backend.service.env_file and entry.get("absent"):
            payload = b"{}\n"
        elif path.parent == backend.root and (staged / path.name).is_file():
            payload = (staged / path.name).read_bytes()
        else:
            expected.append(replacement)
            continue
        replacement.update(bytes=base64.b64encode(payload).decode(), sha256=hashlib.sha256(payload).hexdigest())
        replacement.pop("absent", None)
        expected.append(replacement)
    return expected


def candidate_selections(backend, candidate, new_data, realm, saved, before):
    from .backend import Backend
    original = json.loads(base64.b64decode(next(e for e in saved if e["path"] == str(backend.service.selection))["bytes"]))
    basic = {**original, **data_fields(original, new_data), "owner": "haus.astrid.runtime",
             "program_root": str(backend.root), "realm_id": realm["realm_id"],
             "realm_root": str(new_data / "runtime" / "realms" / realm["realm_id"]),
             "identity": candidate.document()["identity"]}
    settings = read_json(backend.service.env_file)
    configured = {**basic, "service_settings": settings}
    cwd, options = Backend._prior_chat_settings(candidate)
    chat = {**configured, "chat_settings": ({"ASTRID_ACP_CWD": str(cwd), **options} if cwd else {})}
    evidence = {**chat, "r0_source": {"path": original["data_root"], "fingerprint": before}}
    return [original, basic, configured, chat, evidence]


def publish_prepared_launchers(backend, saved, expected):
    validate_restore_targets(backend, saved, expected)
    launchers = {"run", "run.cmd", "run_runtime.py", "run-astrid", "run-astrid.cmd", "run_astrid.py"}
    for entry in expected:
        path = Path(entry["path"])
        if path.parent != backend.root or path.name not in launchers or entry.get("absent"):
            continue
        payload = base64.b64decode(entry["bytes"])
        mode = 0o755 if path.name in {"run", "run-astrid"} else 0o600
        descriptor, temporary = platform.private_temporary(path.parent, ".r1-launcher-")
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            platform.durable_replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def validate_restore_targets(backend, saved, expected):
    """Refuse to clobber external edits to any service, marker or launcher."""
    allowed = {entry["path"]: entry for entry in expected}
    if len(allowed) != len(saved) or set(allowed) != {entry["path"] for entry in saved}:
        raise ValueError("The candidate service snapshot is incomplete.")
    for entry in saved:
        path = absolute(entry["path"])
        if path == backend.service.selection:
            continue  # field-aware selection validation happens before this
        previous = None if entry.get("absent") else base64.b64decode(entry["bytes"])
        candidate = allowed[str(path)]
        written = None if candidate.get("absent") else base64.b64decode(candidate["bytes"])
        actual = path.read_bytes() if path.exists() else None
        if actual not in (previous, written):
            raise ValueError(f"A retained service or launcher changed; refusing to overwrite {path}.")


def require_candidate_stopped(backend, new_data, realm_id):
    """Recovery re-observes ownership even after an earlier stop receipt."""
    realm = {"realm_root": str(new_data / "runtime" / "realms" / realm_id)}
    with stopped_source(backend, new_data, realm):
        pass


def validate_transaction(root, data, transaction):
    old_data = absolute(transaction["source_data_root"])
    new_data = absolute(transaction["data_root"])
    if (new_data != old_data.with_name(old_data.name + "-r1") or data not in (old_data, new_data)
            or not separate_roots(root, old_data) or not separate_roots(root, new_data)):
        raise ValueError("The interrupted copy update roots do not match this installation.")
    old = transaction["old"]
    candidate = transaction["candidate"]
    from .core import Version
    previous, selected = Version.load(old), Version.load(candidate)
    if selected.root.parent != root / "versions" or current(root) not in (None, previous, selected):
        raise ValueError("The interrupted copy update version selection changed.")
    return old_data, new_data, previous, selected


def rollback(root, data, backend, transaction, save):
    old_data, new_data, old, candidate = validate_transaction(root, data, transaction)
    selected = current(root)
    if not transaction.get("activation_intent"):
        # Staging/adoption changed no selected service/launcher bytes. Never
        # restore over an external edit that caused the update to refuse.
        if selected == candidate:
            if old.root.parent == root / "versions":
                switch(root, old)
            else:
                (root / "current").unlink(missing_ok=True)
        save("rolled_back")
        return
    record = backend.service.recorded()
    saved_selection = next(entry for entry in transaction["saved_service"]
                           if entry["path"] == str(backend.service.selection))
    old_record = json.loads(base64.b64decode(saved_selection["bytes"]))
    candidate_owned = (record.get("owner") == "haus.astrid.runtime"
                       and record.get("program_root") == str(root)
                       and record.get("r1_data_root") == str(new_data)
                       and record.get("realm_id") == transaction["realm"]["realm_id"]
                       and record.get("realm_root") == str(new_data / "runtime" / "realms" / transaction["realm"]["realm_id"]))
    records = transaction["candidate_selections"]
    # Backend's identity-checked stop owns foreground retirement. Every other
    # field must be an exact variant recorded before the operation started.
    without_foreground = lambda value: {k: v for k, v in value.items() if k != "foreground"}
    if not any(without_foreground(record) == without_foreground(expected) for expected in records):
        raise ValueError("The selected service changed; refusing to overwrite it during rollback.")
    if not transaction.get("candidate_stopped"):
        if candidate_owned:
            backend.stop(candidate, new_data)  # fresh Runtime/process/manager identity checks
        else:
            # A crash before register published the selection cannot have
            # started Runtime via this policy. Unexpected publication refuses.
            if any((new_data / "runtime" / name).exists() for name in ("discovery.json", "instance.lock")):
                raise ValueError("An unselected candidate may be running; refusing rollback.")
            candidate_realm = {"realm_root": str(new_data / "runtime" / "realms" / transaction["realm"]["realm_id"])}
            lock = absolute(Path(candidate_realm["realm_root"]) / "owner.lock")
            with lock.open("rb") as stream:
                platform.lock_file(stream.fileno(), blocking=False)
                platform.unlock_file(stream.fileno())
        save("recovering", candidate_stopped=True)
    require_candidate_stopped(backend, new_data, transaction["realm"]["realm_id"])
    validate_restore_targets(backend, transaction["saved_service"], transaction["candidate_service"])
    restore_service(backend, transaction["saved_service"])
    if backend.service.system == "linux" and backend.service.systemd():
        backend.service.run(["systemctl", "--user", "daemon-reload"])
    if old.root.parent == root / "versions":
        switch(root, old)
    else:
        (root / "current").unlink(missing_ok=True)
    save("rolled_back")


def warn_divergence(backend, say):
    record = backend.service.recorded()
    evidence = record.get("r0_source")
    if not isinstance(evidence, dict):
        return
    source = evidence.get("path")
    try:
        changed = fingerprint(absolute(source)) != evidence.get("fingerprint")
    except (OSError, ValueError, TypeError):
        changed = True
    if changed:
        say(f"Previous workspace changed after the update; it is kept at {source}")
