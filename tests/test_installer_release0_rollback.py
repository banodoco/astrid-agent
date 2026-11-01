"""Release-0 rollback keeps the retained Runtime startable."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer.app_update import locked
from astrid.installer.core import (
    Installer,
    Version,
    current,
    recover_transaction,
    switch,
    write_json,
)
from astrid.installer.custody import stopped_custody
from astrid.installer.service import activation_lock_held
from tests.test_installer_self_contained_launchers import (
    LEGACY_VERSION,
    RELEASE0_MANIFEST,
    _legacy_installation,
    _release0_environment,
)

RELEASE0_FIXTURES = Path(__file__).parent / "fixtures" / "release0"
RELEASE0_SERVICES = (
    "services/launchd.plist",
    "services/service-env.json",
    "services/systemd.service",
    "services/task.xml",
)


def _frozen_release0_service_bytes() -> dict[str, bytes]:
    """Read Q's service evidence and verify its checked-in hashes before use."""
    expected = {
        row.split(maxsplit=1)[1]: row.split(maxsplit=1)[0]
        for row in (RELEASE0_FIXTURES / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    }
    frozen = {name: (RELEASE0_FIXTURES / name).read_bytes() for name in RELEASE0_SERVICES}
    assert all(hashlib.sha256(value).hexdigest() == expected[name] for name, value in frozen.items())
    return frozen


def _candidate(root: Path) -> Version:
    folder = root / "versions" / "candidate"
    folder.mkdir(parents=True)
    profile = folder / "profile.json"
    profile.write_text("{}\n", encoding="utf-8")
    # A retained transaction must name a self-contained, managed candidate.
    # It is not run here, but the generated release-0 launcher validates its
    # retained metadata before it will admit the terminal rollback.
    version = Version(
        folder,
        folder / "py" / "bin" / "python",
        profile,
        {},
        release=1,
        managed_by="pinokio",
    )
    write_json(folder / "version.json", version.document())
    return version


def _runtime_environment(tmp_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update({
        "HOME": str(tmp_path / "home"),
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
    })
    return environment


def _start_release0_runtime(root: Path, marker: Path, environment: dict[str, str]) -> None:
    launched = subprocess.run(
        [str(root / "run"), "--hosted-origin", "off"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert launched.returncode == 0, launched.stderr
    call = json.loads(marker.read_text(encoding="utf-8"))
    assert call["argv"][-2:] == ["--hosted-origin", "off"]
    assert call["release"] == "0"
    assert call["managed_by"] == "pinokio"


def _journal(root: Path, data: Path, old: Version, candidate: Version, step: str) -> dict:
    return {
        "format": "astrid-install-update/v1",
        "operation_id": "f" * 32,
        "transaction_kind": "app",
        "step": step,
        "data_root": str(data),
        "old": old.document(),
        "candidate": candidate.document(),
        "realm": {"realm_id": "release0", "realm_root": str(data / "realm")},
        "backup": None,
    }


def _recover_with_stopped_custody(
    root: Path, data: Path, tmp_path: Path, backend, stopped: list[Version],
    select_old, marker: Path,
) -> None:
    """Mirror recovery's post-stop custody and selected-version ticket handoff."""
    backend.service = SimpleNamespace(home=tmp_path / "home", system=sys.platform)
    environment = _runtime_environment(tmp_path)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("XDG_DATA_HOME", environment["XDG_DATA_HOME"])
        with locked(root / ".app-update.lock"), contextlib.ExitStack() as authority:
            def stop_recovery(version: Version, _data: Path) -> None:
                stopped.append(version)
                custody = authority.enter_context(stopped_custody(root, backend))
                assert custody is not None
                backend._pinokio_custody = custody

            def activate_old(version: Version, workspace: Path, _realm: dict) -> None:
                assert current(root) == version
                assert activation_lock_held(root, version)
                ticket = backend._pinokio_custody.arm(version, workspace)
                assert ticket["binding"]["version"] == version.document()
                assert ticket["binding"]["step"] == "rollback_selected"
                environment["ASTRID_INSTALL_CUSTODY"] = json.dumps(ticket)
                _start_release0_runtime(root, marker, environment)

            recover_transaction(
                root, data, backend, stop=stop_recovery, switch_old=select_old,
                activate_old=activate_old, restore_workspace=False,
                say=lambda _message: None,
            )


@pytest.mark.posix_native
def test_rollback_selects_and_starts_authentic_release0_runtime(tmp_path: Path) -> None:
    """A failed candidate uses the immutable release-0 package after rollback."""
    python, _site_packages = _release0_environment(
        tmp_path / "program" / "versions" / LEGACY_VERSION / "py"
    )
    root, data, marker = _legacy_installation(tmp_path, python)
    old = current(root)
    assert old is not None and old.release == 0 and old.managed_by == "pinokio"
    candidate = _candidate(root)
    switch(root, candidate)
    frozen_services = _frozen_release0_service_bytes()
    immutable = {
        root / "install.json": (root / "install.json").read_bytes(),
        old.root / "manifest.json": RELEASE0_MANIFEST.read_bytes(),
        old.root / "version.json": (old.root / "version.json").read_bytes(),
    }
    transaction = _journal(root, data, old, candidate, "switched")
    write_json(root / "update-transaction.json", transaction)
    stopped: list[Version] = []
    selected: list[Version] = []

    def select_old(version: Version, _candidate: Version | None) -> None:
        selected.append(version)
        switch(root, version)

    # Pinokio recovery owns the operation lock and acquires custody after stop.
    _recover_with_stopped_custody(
        root, data, tmp_path, SimpleNamespace(), stopped, select_old, marker,
    )

    recovered = json.loads((root / "update-transaction.json").read_text(encoding="utf-8"))
    assert stopped == [candidate]
    assert selected == [old]
    assert recovered["operation_id"] == transaction["operation_id"]
    assert recovered["step"] == "rolled_back"
    assert recovered["old"] == old.document()
    assert current(root) == old
    assert {path: path.read_bytes() for path in immutable} == immutable
    assert {name: (RELEASE0_FIXTURES / name).read_bytes() for name in RELEASE0_SERVICES} == frozen_services


@pytest.mark.posix_native
def test_pre_stop_interruption_recovers_and_starts_authentic_release0_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after stop intent preserves old state and replays its Runtime."""
    python, _site_packages = _release0_environment(
        tmp_path / "program" / "versions" / LEGACY_VERSION / "py"
    )
    root, data, marker = _legacy_installation(tmp_path, python)
    old = current(root)
    assert old is not None and old.release == 0 and old.managed_by == "pinokio"
    candidate = _candidate(root)
    frozen_services = _frozen_release0_service_bytes()
    before = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }
    backend = SimpleNamespace()
    installer = Installer(root, data, backend, say=lambda _message: None, managed_by="pinokio")
    original_save = installer._save_update

    class PowerLoss(BaseException):
        pass

    def save_then_interrupt(transaction: dict, step: str, **details: object) -> None:
        original_save(transaction, step, **details)
        if step == "stopping":
            raise PowerLoss("simulated interruption before stop")

    monkeypatch.setattr(installer, "_save_update", save_then_interrupt)
    with pytest.raises(PowerLoss, match="before stop"):
        installer._run_update_transaction(
            old,
            {"realm_id": "release0", "realm_root": str(data / "realm")},
            prepare=lambda: candidate,
            stop=lambda *_args: pytest.fail("pre-stop interruption called stop"),
            switch_candidate=lambda *_args: pytest.fail("pre-stop interruption selected candidate"),
            switch_old=lambda *_args: pytest.fail("pre-stop interruption rolled back"),
            activate=lambda *_args: pytest.fail("pre-stop interruption activated candidate"),
            activate_old=lambda *_args: pytest.fail("pre-stop interruption activated old Runtime"),
            restore_workspace=False,
            kind="app",
        )

    pending = json.loads((root / "update-transaction.json").read_text(encoding="utf-8"))
    after = {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*") if path.is_file()
    }
    assert pending["step"] == "stopping"
    assert pending["backup"] is None
    assert "stop_authority" not in pending and "launch_intent" not in pending
    assert current(root) == old
    assert set(after) - set(before) == {
        Path("run"), Path("run.cmd"), Path("run_runtime.py"), Path("update-transaction.json"),
    }
    assert {path: after[path] for path in before} == before
    assert {name: (RELEASE0_FIXTURES / name).read_bytes() for name in RELEASE0_SERVICES} == frozen_services

    stopped: list[Version] = []
    selected: list[Version] = []

    def select_old(version: Version, _candidate: Version | None) -> None:
        selected.append(version)
        assert current(root) == version

    # A restarted recovery reacquires the operation lock and stopped custody.
    _recover_with_stopped_custody(
        root, data, tmp_path, backend, stopped, select_old, marker,
    )

    recovered = json.loads((root / "update-transaction.json").read_text(encoding="utf-8"))
    assert stopped == [old]
    assert selected == [old]
    assert recovered["operation_id"] == pending["operation_id"]
    assert recovered["step"] == "rolled_back"
    assert recovered["old"] == old.document()
    assert current(root) == old
