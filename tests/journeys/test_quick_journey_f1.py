from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.core.dev import journey, reference_realm
from astrid.core.dev.journey import JourneyFailure


def _descriptor(tmp_path: Path) -> dict:
    for name in ("data", "source.json", "discovery.json", "product.json", "astrid.json"):
        path = tmp_path / name
        if name == "data":
            path.mkdir()
        else:
            path.write_text("{}")
    return {
        "version": 1, "dataRoot": str(tmp_path / "data"), "sourceManifest": str(tmp_path / "source.json"),
        "discoveryFile": str(tmp_path / "discovery.json"), "productCredentialFile": str(tmp_path / "product.json"),
        "astridCredentialFile": str(tmp_path / "astrid.json"), "projectId": "p", "projectSlug": "journey",
        "timelineId": "main", "headRevisionId": "revision-base", "realmId": "realm-1",
        "runtimeInstanceId": "instance-1", "revisions": {name: str(index) * 40 for index, name in enumerate(("astrid", "runtime", "app"), 1)},
        "resultFile": str(tmp_path / "result.json"),
    }


def test_quick_result_requires_matching_realm_instance_and_revision_chain(tmp_path: Path) -> None:
    descriptor = _descriptor(tmp_path)
    result = {"realmId": "realm-1", "runtimeInstanceId": "instance-1", "baseRevisionId": "revision-base",
              "astridRevisionId": "revision-astrid", "savedRevisionId": "revision-app",
              "astridParentRevisionId": "revision-base", "appParentRevisionId": "revision-astrid", "astridLabel": "Astrid",
              "appLabel": "App", "revisions": descriptor["revisions"]}
    result_path = tmp_path / "result.json"
    result_path.write_text(json.dumps(result))
    assert journey._validate_quick_result(result_path, descriptor) == result
    result["runtimeInstanceId"] = "other-instance"
    result_path.write_text(json.dumps(result))
    with pytest.raises(JourneyFailure, match="different realm or Runtime instance"):
        journey._validate_quick_result(result_path, descriptor)


def test_quick_render_argv_pins_version_profile_and_remotion() -> None:
    argv = journey.render_argv(["py", "-m", "astrid"], "main", "journey", "out.mp4",
                               backend=journey.REMOTION_BACKEND, window=(0, 1), expected_version=7,
                               profile={"width": 320, "height": 180}, timeout=None)
    assert argv == ["py", "-m", "astrid", "timelines", "render", "main", "--project", "journey",
                    "--range", "0", "1", "--expected-version", "7", "--profile", '{"width":320,"height":180}',
                    "--backend", "rendering.remotion", "--output-name", "out.mp4", "--wait", "--json"]


def test_tiny_reference_has_one_second_small_canvas_clip() -> None:
    argv = reference_realm.ffmpeg_clip_argv(Path("clip.mp4"), 0, seconds=reference_realm.TINY_CLIP_SECONDS,
                                            size=reference_realm.TINY_CLIP_SIZE)
    assert "testsrc2=size=320x180:rate=24:duration=1" in argv
    publication = reference_realm.media_only_publication("p", [{"object_id": "id", "digest": "digest"}], seconds=1)
    assert publication["parent_composition"]["clips"][0]["to"] == 1
    assert publication["parent_composition"]["config"]["theme_overrides"]["visual"]["canvas"] == {
        "width": 320, "height": 180, "fps": 24}


def test_cli_quick_branch_defaults_to_generated_one_second_fixture(tmp_path: Path, monkeypatch, capsys) -> None:
    summary = tmp_path / "summary.json"
    seen = {}

    def fake_run(reference, timeline, destination, **kwargs):
        seen.update(reference=reference, timeline=timeline, **kwargs)
        destination.write_text('{"ok": true}')
        return 0

    monkeypatch.setattr(journey, "run", fake_run)
    assert journey.main(["--tier", "quick", "--reference", "generated", "--summary", str(summary),
                         "--app-checkout", str(tmp_path)]) == 0
    capsys.readouterr()
    assert seen["reference"] is None and seen["timeline"] == "journey/main"
    assert (seen["start"], seen["end"]) == (0.0, 1.0)


@pytest.mark.parametrize("leftover", [False, True])
@pytest.mark.parametrize("host_stop", ["normal", "error", "forced"])
def test_quick_run_branches_through_browser_render_and_strict_teardown(
    tmp_path: Path, monkeypatch, leftover: bool, host_stop: str,
) -> None:
    from astrid.core.rendering import node_pin

    app = tmp_path / "app"
    app.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("ASTRID_RUNTIME_CHECKOUT", str(runtime))
    reference = tmp_path / "reference"
    (reference / "runtime" / "source-profiles").mkdir(parents=True)
    (reference / "runtime" / "source-profiles" / "astrid.json").write_text("{}")
    summary = tmp_path / "summary.json"
    phases: list[str] = []
    sha = "a" * 40

    monkeypatch.setattr(node_pin, "resolve_pinned_node", lambda *a, **k: node_pin.NodeResolution(
        Path("/usr/bin/node"), None, "v20.19.4", "path", None))
    monkeypatch.setattr(journey, "renderer_install_problem", lambda _project: None)
    monkeypatch.setattr(journey, "disk_preflight", lambda *a: {"path": "/tmp"})
    monkeypatch.setattr(journey, "runtime_checkout_path", lambda: runtime)
    monkeypatch.setattr(journey, "collect_revisions", lambda *a: {
        name: {"path": str(path), "head": sha, "dirty": False} for name, path in
        (("astrid", tmp_path), ("runtime", runtime), ("app", app))})
    monkeypatch.setattr(reference_realm, "build_reference", lambda *a, **k: reference)
    monkeypatch.setattr(journey, "select_reference_realm", lambda _reference: ("realm-1", tmp_path / "source-realm"))
    def browser(*_a, **_k):
        phases.append("quick_browser")
        return ({"projectSlug": "journey", "timelineId": "main", "projectId": "p"},
                {"realmId": "realm-1", "runtimeInstanceId": "instance-1", "baseRevisionId": "base",
                 "astridRevisionId": "astrid", "savedRevisionId": "app", "astridLabel": "A", "appLabel": "B"})
    monkeypatch.setattr(journey, "_quick_browser_exchange", browser)

    from astrid.sdk import upgrade
    def stop_host(data_root):
        assert data_root.is_absolute() and data_root.name == "data"
        phases.append("quick_host_down")
        if host_stop == "error":
            raise upgrade.UpgradeError("host shutdown refused")
        return {"host_status": "stopped", **({"forced": True} if host_stop == "forced" else {})}
    monkeypatch.setattr(upgrade, "stop_pack_host", stop_host)

    def render(self, _descriptor, _result):
        phases.append("render")
        self.r.details["quick_render"] = {"task_id": "task-1"}
    monkeypatch.setattr(journey.Journey, "saved_revision_render", render)

    def command(self, phase, argv, _env, **kwargs):
        phases.append(phase)
        if phase == "first_open":
            packhome = Path(_env["HOME"])
            assert packhome.is_dir()
            assert packhome == Path(_env["ASTRID_RUNTIME_DATA_ROOT"]) / "packhome"
            assert Path(_env["ASTRID_SOURCE_DATA"]).is_relative_to(packhome)
            assert Path(_env["ASTRID_SOURCE_STATE"]).is_relative_to(packhome)
        if phase == "doctor":
            return {"healthy": True, "renderer_node": {"status": "ok"}}
        return {"ok": True}
    monkeypatch.setattr(journey.Runner, "command", command)
    monkeypatch.setattr(journey, "_processes", lambda _root: ([{"pid": 99, "ppid": 1, "pgid": 99, "command": "leftover"}] if leftover else []))
    monkeypatch.setattr(journey, "_ps_rows", lambda: [])
    monkeypatch.setattr(journey, "_reap_owned_groups", lambda *_a, **_k: [])

    status = journey.run(None, "journey/main", summary, tier="quick", start=0, end=1,
                         backend=journey.REMOTION_BACKEND, app_checkout=app)
    report = json.loads(summary.read_text())
    assert "adopt" in phases and "up" in phases and "first_open" in phases and "doctor" in phases
    assert phases.index("up") < phases.index("quick_browser") < phases.index("render")
    assert phases.index("render") < phases.index("quick_host_down") < phases.index("down")
    expected_ok = not leftover and host_stop == "normal"
    assert report["ok"] is expected_ok
    if leftover:
        assert status == 1 and "forced process cleanup" in report["error"]
    if host_stop == "error":
        assert status == 1 and "host shutdown refused" in report["error"]
        assert "down" in phases
    elif host_stop == "forced":
        assert status == 1 and "host required forced SIGKILL cleanup" in report["error"]
    elif not leftover:
        assert status == 0 and report["task_ids"] == ["task-1"]

