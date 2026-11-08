"""Install states that exist in the field, built from the hermetic handoff case.

Each builder takes the ``case`` namespace from ``tests.test_boundary_handoff``
(real Runtime adoption, persistent systemctl simulation) and leaves the
machine in one observed field state. Every field failure adds a builder here,
so it becomes the regression ledger the field-failure review asks for.

``handed_off_release1_r0_live`` is Hannah's Mac on 8 Nov 2026: a finished
copy-only R0 to R1 storage move that landed on Release 1, R1 active, R0's data
retained at the R0 root, and a second (R0-era) Runtime publishing discovery
for the same realm from the retained root.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from astrid.installer import boundary_registration as registration
from astrid.installer.core import Version, current, read_json, write_json

R0_INSTANCE = "r0-era-runtime-disk-full"


def _extend_manager(shim: Path) -> None:
    """The ordinary update path also starts, stops and inspects the R1 unit."""
    body = shim.read_text()
    if "show-environment" in body:
        return
    body = body.replace(
        'elif args == ["--user", "daemon-reload"]:',
        'elif args == ["--user", "show-environment"]:\n'
        '    pass\n'
        'elif args == ["--user", "start", "astrid-r1.service"]:\n'
        '    assert not state["running"], "two service owners"\n'
        '    state["r1"] = "active"\n'
        '    path.write_text(json.dumps(state))\n'
        'elif args == ["--user", "stop", "astrid-r1.service"]:\n'
        '    state["r1"] = "inactive"\n'
        '    path.write_text(json.dumps(state))\n'
        'elif args == ["--user", "daemon-reload"]:', 1)
    shim.write_text(body)


def stage_release(c, release: int):
    """A staged direct version for ``release`` built the way the case stages Release 1."""
    from astrid_runtime.bootstrap import SourceProfile
    from astrid_runtime import workspace_copy

    runtime_root = Path(workspace_copy.__file__).parent.parent
    agent_root = Path(__file__).resolve().parents[2]

    def stage(manifest, selected_root, target, *, copy_from=None):
        assert copy_from is None and selected_root == c.root and target == c.destination
        folder = c.root / "versions" / f"release{manifest['release']}"
        folder.mkdir()
        profile = SourceProfile("astrid", str(runtime_root), str(agent_root))
        write_json(folder / "profile.json", profile.as_dict())
        version = Version(folder, Path(sys.executable), folder / "profile.json",
                      {"ASTRID_RUNTIME_DATA_ROOT": str(target)}, release=manifest["release"])
        write_json(folder / "version.json", version.document())
        write_json(folder / "manifest.json", manifest)
        registration.stage_runtime_launcher(version)
        c.staged.append(version)
        return version

    return stage


def _as_completed_by_earlier_installer(c) -> None:
    """The field state: a ``complete`` journal and the handoff's own records, never normalised.

    Installers before UR-15 left the handoff journal authoritative forever. A
    current installer normalises at the end of the handoff, so undo that here.
    """
    journal = c.tx.journal
    archive = journal.with_name("astrid-r1-handoff.completed.json")
    if not archive.exists():
        return
    value = read_json(archive)
    marker, selection, _pointer = c.tx._records(value)
    (c.root / "install.json").write_text(marker)
    c.service.selection.write_text(selection)
    os.replace(archive, journal)


def handed_off_release1_r0_live(c, monkeypatch):
    """Hannah's state; returns ``c`` with ``manifest_path(release)`` and fake live health."""
    from tests.test_installer_r0_entry import state

    c.tx.run(c.data, lambda: c.manifest, say=lambda _line: None)
    assert current(c.root).release == 1
    _as_completed_by_earlier_installer(c)
    # The R0-era Runtime came back against the retained R0 data (same realm id).
    write_json(c.data / "runtime" / "discovery.json", {
        "pid": os.getpid(), "runtime_instance_id": R0_INSTANCE, "active_realm": "r0",
        "identity": {"release": 0, "managed_by": None},
        "door_endpoint": "http://127.0.0.1:17340"})
    _extend_manager(Path(os.environ["PATH"].split(os.pathsep)[0]) / "systemctl")
    c.staged = []
    c.verified_releases = []
    c.state = lambda: state(c.manager)

    def live(_data_root, program_root=None):
        # Health answers only while the R1 unit runs; it reports the selected version.
        if state(c.manager)["r1"] != "active":
            return None
        version = current(c.root)
        return {"release": version.release, "managed_by": None, "instance_id": f"r1-{version.release}"}

    def verify(version, data, realm_id, **_options):
        assert data == c.destination and realm_id == "r0"
        assert state(c.manager)["r1"] == "active" and current(c.root) == version
        c.verified_releases.append(version.release)

    def down(paths, boundary, *, stop_manager=None, stop_foreground=None, restore_selection=None):
        # Runtime's C1 fence is not under test; the validated manager handoff is.
        assert Path(paths.app_support) == c.destination
        stop_manager()
        return {}

    from astrid.installer import core
    import importlib

    bootstrap = importlib.import_module("astrid_runtime.bootstrap")

    try:
        from astrid.installer import installation
    except ImportError:  # installers before UR-15 have no resolver to observe with
        pass
    else:
        monkeypatch.setattr(installation, "probe_live", live)
    monkeypatch.setattr(bootstrap, "down", down)
    monkeypatch.setattr(core, "schema_unchanged", lambda *a, **k: True)
    monkeypatch.setattr(c.backend, "verify", verify)
    monkeypatch.setattr(c.backend, "stage", stage_release(c, 0))
    monkeypatch.setattr(c.backend, "validate_candidate", lambda *a, **k: None)
    monkeypatch.setattr(c.backend, "workspace", lambda version, data: {
        "realm_id": "r0", "realm_root": str(data / "runtime/realms/r0")})
    monkeypatch.setattr(c.backend, "preflight_stop", lambda data: None)
    monkeypatch.setattr(c.backend, "source_checkout_ownership", lambda *a, **k: False)
    monkeypatch.setattr(c.backend, "sync_skills", lambda version: {"ok": True, "destinations": []})
    monkeypatch.setattr(c.backend, "open", lambda: setattr(c.backend, "command_result", None))

    def manifest_path(release: int) -> Path:
        path = c.root.parent / f"manifest-{release}.json"
        path.write_text(json.dumps({"release": release}))
        return path

    c.manifest_path = manifest_path
    return c


def selection(c) -> dict:
    return read_json(c.service.selection)
