from types import SimpleNamespace
import shlex

import pytest

from scripts import ci_journey as journey


def _service_files(tmp_path, platform):
    root = tmp_path / "program"
    data = tmp_path / "data"
    if platform == "linux":
        directory = data.parent / "home" / ".config" / "systemd" / "user"
        paths = {"r0": directory / "astrid.service",
                 "r1": directory / "astrid-r1.service"}
    else:
        directory = data.parent / "home" / "Library" / "LaunchAgents"
        paths = {"r0": directory / "haus.astrid.runtime.plist",
                 "r1": directory / "haus.astrid.runtime.r1.plist"}
    directory.mkdir(parents=True)
    for path in paths.values():
        path.write_text("owned service definition\n")
    return root, data, paths


def test_linux_r1_ownership_requires_active_r1_and_disabled_inactive_r0(tmp_path, monkeypatch):
    root, data, paths = _service_files(tmp_path, "linux")
    monkeypatch.setattr(journey.sys, "platform", "linux")
    monkeypatch.setattr(journey, "program_root", lambda: tmp_path / "default" / "astrid")
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[0] == "busctl":
            command = [str(root / "bin/runtime"), "run", "--profile", "astrid", "--data-root", str(data) + "-r1"]
            return SimpleNamespace(returncode=0, stdout="a(sasbttttuii) 1 " + shlex.quote(command[0]) + " 6 " + shlex.join(command) + " false 0 0 0 0 0 0 0", stderr="")
        if argv[3] == "astrid-r1.service":
            fields = {"LoadState": "loaded", "FragmentPath": str(paths["r1"]),
                      "ActiveState": "active", "UnitFileState": "enabled", "DropInPaths": ""}
        else:
            fields = {"LoadState": "loaded", "FragmentPath": str(paths["r0"]),
                      "ActiveState": "inactive", "UnitFileState": "disabled"}
        return SimpleNamespace(returncode=0,
                               stdout="".join(f"{key}={value}\n" for key, value in fields.items()),
                               stderr="")

    monkeypatch.setattr(journey.subprocess, "run", run)
    receipt = journey.assert_direct_service_ownership(root, data, release=1)
    assert receipt == {"manager": "systemd-user", "service_path": str(paths["r1"]),
                       "foreground_fallback": False}
    assert [call[3] for call in calls if call[0] == "systemctl"] == ["astrid-r1.service", "astrid.service"]

    def active_r0(argv, **_kwargs):
        if argv[0] == "busctl":
            command = [str(root / "bin/runtime"), "run", "--profile", "astrid", "--data-root", str(data) + "-r1"]
            return SimpleNamespace(returncode=0, stdout="a(sasbttttuii) 1 " + shlex.quote(command[0]) + " 6 " + shlex.join(command) + " false 0 0 0 0 0 0 0", stderr="")
        if argv[3] == "astrid.service":
            return SimpleNamespace(returncode=0, stdout=(
                f"LoadState=loaded\nFragmentPath={paths['r0']}\n"
                "ActiveState=active\nUnitFileState=enabled\nDropInPaths=\n"), stderr="")
        return SimpleNamespace(returncode=0, stdout=(
            f"LoadState=loaded\nFragmentPath={paths['r1']}\n"
            "ActiveState=active\nUnitFileState=enabled\nDropInPaths=\n"), stderr="")

    monkeypatch.setattr(journey.subprocess, "run", active_r0)
    with pytest.raises(RuntimeError, match="R0 service is not owned, disabled and inactive"):
        journey.assert_direct_service_ownership(root, data, release=1)


def test_linux_default_service_assertion_keeps_r0_behavior(tmp_path, monkeypatch):
    root, data, paths = _service_files(tmp_path, "linux")
    monkeypatch.setattr(journey.sys, "platform", "linux")
    monkeypatch.setattr(journey, "program_root", lambda: tmp_path / "default" / "astrid")
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=(
            f"LoadState=loaded\nFragmentPath={paths['r0']}\nActiveState=active\n"), stderr="")

    monkeypatch.setattr(journey.subprocess, "run", run)
    assert journey.assert_direct_service_ownership(root, data)["service_path"] == str(paths["r0"])
    assert len(calls) == 1 and calls[0][3] == "astrid.service"


def test_macos_r1_ownership_checks_r1_running_and_r0_disabled_unloaded(tmp_path, monkeypatch):
    root, data, paths = _service_files(tmp_path, "darwin")
    monkeypatch.setattr(journey.sys, "platform", "darwin")
    monkeypatch.setattr(journey, "program_root", lambda: tmp_path / "default" / "astrid")
    monkeypatch.setattr(journey.os, "getuid", lambda: 501)
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        if argv[1] == "print-disabled":
            return SimpleNamespace(returncode=0,
                                   stdout='"haus.astrid.runtime" => true\n', stderr="")
        if argv[-1].endswith("runtime.r1"):
            target = argv[-1]
            args = [str(root / "bin/runtime"), "run", "--profile", "astrid", "--data-root", str(data) + "-r1"]
            output = (f"{target} = {{\n  path = {paths['r1']}\n  state = running\n  arguments = {{\n" + "".join(f"    {arg}\n" for arg in args) + "  }\n}\n")
            return SimpleNamespace(returncode=0, stdout=output, stderr="")
        return SimpleNamespace(returncode=113, stdout="",
                               stderr='Could not find service "haus.astrid.runtime"\n')

    monkeypatch.setattr(journey.subprocess, "run", run)
    receipt = journey.assert_direct_service_ownership(root, data, release=1)
    assert receipt == {"manager": "launchd", "service_path": str(paths["r1"]),
                       "foreground_fallback": False}
    assert calls == [["launchctl", "print", "gui/501/haus.astrid.runtime.r1"],
                     ["launchctl", "print", "gui/501/haus.astrid.runtime"],
                     ["launchctl", "print-disabled", "gui/501"]]

    def r0_active(argv, **_kwargs):
        if argv[-1].endswith("runtime.r1"):
            args = [str(root / "bin/runtime"), "run", "--profile", "astrid", "--data-root", str(data) + "-r1"]
            return SimpleNamespace(returncode=0, stdout=(
                f"{argv[-1]} = {{\n  path = {paths['r1']}\n  state = running\n  arguments = {{\n" + "".join(f"    {arg}\n" for arg in args) + "  }\n}\n"), stderr="")
        if argv[1] == "print-disabled":
            return SimpleNamespace(returncode=0,
                                   stdout='"haus.astrid.runtime" => false\n', stderr="")
        return SimpleNamespace(returncode=0, stdout="R0 is still loaded", stderr="")

    monkeypatch.setattr(journey.subprocess, "run", r0_active)
    with pytest.raises(RuntimeError, match="R0 LaunchAgent is not disabled and unloaded"):
        journey.assert_direct_service_ownership(root, data, release=1)
