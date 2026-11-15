from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from astrid.sdk.project_render import open_render
from astrid.sdk.remote import RemoteRuns


class _Runtime:
    def __init__(self, data: bytes = b"video-bytes") -> None:
        self.data = data
        self.digest = "sha256:" + hashlib.sha256(data).hexdigest()
        self.opened: list[list[str]] = []
        self.runs = [
            {"run_id": "R-old", "project_id": "P-1", "capability_id": "rendering.render", "status": "completed", "created_at": "2026-01-01", "task_ids": ["T-old"]},
            {"run_id": "R-failed", "project_id": "P-1", "capability_id": "rendering.render", "status": "failed", "created_at": "2026-01-03", "task_ids": ["T-failed"]},
            {"run_id": "R-new", "project_id": "P-1", "capability_id": "rendering.render", "status": "completed", "created_at": "2026-01-02", "task_ids": ["T-new"]},
        ]

    def get_project(self, ref):
        return {"project_id": "P-1", "slug": ref}

    def current_project(self):
        return {
            "project": {"project_id": "P-1", "slug": "demo"},
            "scope": "workspace",
        }

    def list_project_runs(self, project_id, *, cursor=None, limit=50):
        assert project_id == "P-1"
        return [self.runs, None]

    def get_run(self, run_id):
        return next(run for run in self.runs if run["run_id"] == run_id)

    def get_task(self, task_id):
        return {
            "task_id": task_id,
            "capability_id": "rendering.render",
            "state": "completed",
            "spec": {"spec": {"inputs": {"output_name": "review.mp4"}}},
            "result": {"outputs": [{
                "output_port": "video",
                "managed_object_reference": "O-video",
                "digest": self.digest,
                "actual_filename": "review.mp4",
                "media_type": "video/mp4",
                "size": len(self.data),
                "ordinal": 0,
                "role": "result",
                "task_id": task_id,
            }]},
        }

    def list_project_objects(self, project_id, *, cursor=None, limit=50):
        assert project_id == "P-1"
        return [[{"object_id": "O-video", "digest": self.digest, "size": len(self.data), "filename": "video"}], None]

    def get_object(self, object_id):
        assert object_id == "O-video"
        return {"data": self.data, "status": 200, "headers": {}}

    def get_project_object_location(self, project_id, object_id):
        assert project_id == "P-1" and object_id == "O-video"
        path = Path(self._canonical_path)
        return {
            "object_id": self.digest,
            "digest": self.digest,
            "size": len(self.data),
            "media_type": "video/mp4",
            "filename": "video.mp4",
            "local_path": str(path),
            "storage": "runtime_cas",
            "verified": True,
        }


def test_open_selects_latest_successful_runtime_render(monkeypatch, tmp_path: Path) -> None:
    runtime = _Runtime()
    launched: list[list[str]] = []
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")
    monkeypatch.setattr("astrid.sdk.project_render.subprocess.run", lambda argv, check: launched.append(argv))

    result = RemoteRuns(runtime).open(cache_root=tmp_path)

    assert result.ok
    assert result.data["run_id"] == "R-new"
    assert result.data["digest"] == runtime.digest
    assert Path(result.data["local_path"]).read_bytes() == runtime.data
    assert Path(result.data["local_path"]).suffix == ".mp4"
    assert launched == [["open", result.data["local_path"]]]


def test_open_uses_runtime_canonical_path_by_default_without_cache_copy(monkeypatch, tmp_path: Path) -> None:
    runtime = _Runtime()
    digest = runtime.digest.removeprefix("sha256:")
    canonical = tmp_path / "runtime" / "realms" / "realm-1" / "cas" / "sha256" / digest[:2] / digest[2:]
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(runtime.data)
    runtime._canonical_path = canonical
    launched: list[list[str]] = []
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")
    monkeypatch.setattr("astrid.sdk.project_render.subprocess.run", lambda argv, check: launched.append(argv))

    result = RemoteRuns(runtime).open()

    assert result.ok
    presentation = Path(result.data["local_path"])
    assert not presentation.is_symlink()
    assert not os.path.samefile(presentation, canonical)
    assert result.data["canonical_local_path"] == str(canonical)
    assert result.data["presentation_path"] == str(presentation)
    assert result.data["presentation_link"] is False
    assert result.data["presentation_storage"] == "independent copy-on-write view"
    assert presentation.read_bytes() == runtime.data
    assert launched == [["open", "-b", "com.apple.QuickTimePlayerX", str(presentation)]]

    retry = RemoteRuns(runtime).open()

    assert retry.ok
    assert retry.data["local_path"] == str(presentation)
    assert not os.path.samefile(Path(retry.data["local_path"]), canonical)
    assert [item for item in presentation.parent.iterdir() if not item.name.startswith(".")] == [presentation]


@pytest.mark.parametrize("location_state", ["valid", "missing", "unverified", "corrupt", "symlink"])
def test_managed_open_uses_only_verified_canonical_location(monkeypatch, tmp_path, location_state):
    runtime = _Runtime()
    # Resolve /tmp's macOS symlink before testing canonical path validation.
    digest = runtime.digest.removeprefix("sha256:")
    canonical = tmp_path.resolve() / "runtime" / "realms" / "realm-1" / "cas" / "sha256" / digest[:2] / digest[2:]
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(runtime.data)
    runtime._canonical_path = canonical
    original_task = runtime.get_task
    runtime.get_task = lambda task_id: {
        **original_task(task_id),
        "result": {"outputs": [{
            "output_port": "video", "managed_object_reference": "O-video",
            "digest": runtime.digest, "size": len(runtime.data),
            "actual_filename": "review.mp4", "media_type": "video/mp4",
            "ordinal": 0, "role": "result", "task_id": task_id,
        }]},
    }
    def unexpected_download(*args, **kwargs):
        pytest.fail("default opening must not download or create a cache copy")
    runtime.get_object = unexpected_download
    monkeypatch.setattr("astrid.sdk.project_render._materialize", unexpected_download)
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")
    if location_state == "missing":
        runtime.get_project_object_location = None
    elif location_state == "unverified":
        resolver = runtime.get_project_object_location
        runtime.get_project_object_location = lambda *args: {**resolver(*args), "verified": False}
    elif location_state == "corrupt":
        canonical.write_bytes(b"tampered")
    elif location_state == "symlink":
        link = canonical.with_name("link")
        link.symlink_to(canonical)
        runtime._canonical_path = link
    opened = []
    result = open_render(runtime, "P-1", opener=opened.append)
    if location_state == "valid":
        assert result.ok
        presentation = Path(result.data["local_path"])
        assert not presentation.is_symlink()
        assert not os.path.samefile(presentation, canonical)
        assert opened == [presentation]
        assert result.data["managed_object_reference"] == "O-video"
        assert result.data["filename"] == "review.mp4"
        assert result.data["canonical_local_path"] == str(canonical)
        assert result.data["presentation_link"] is False
        assert result.data["open_requested"] is True
        assert result.data["playback_confirmed"] is False
    else:
        assert not result.ok
        assert result.error.code == ("runtime_location_unavailable" if location_state == "missing" else "integrity_error")
        assert opened == []


def test_open_accepts_registry_clip_visual_media_type(monkeypatch, tmp_path: Path) -> None:
    runtime = _Runtime()
    digest = runtime.digest.removeprefix("sha256:")
    canonical = tmp_path / "runtime" / "realms" / "realm-1" / "cas" / "sha256" / digest[:2] / digest[2:]
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(runtime.data)
    runtime._canonical_path = canonical
    runtime.get_task = lambda task_id: {
        "task_id": task_id,
        "capability_id": "rendering.render",
        "state": "completed",
        "result": {"outputs": [{
            "output_port": "video", "managed_object_reference": "O-video",
            "digest": runtime.digest, "size": len(runtime.data),
            "actual_filename": "review.mp4", "media_type": "clip/visual",
            "ordinal": 0, "role": "result", "task_id": task_id,
        }]},
    }
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")
    opened: list[Path] = []

    result = open_render(runtime, "P-1", opener=opened.append)

    assert result.ok
    assert result.data["media_type"] == "clip/visual"
    assert opened == [Path(result.data["local_path"])]


def test_open_exact_run_rejects_cross_project_before_download(monkeypatch, tmp_path: Path) -> None:
    runtime = _Runtime()
    runtime.runs.append({
        "run_id": "R-other", "project_id": "P-2", "capability_id": "rendering.render",
        "status": "completed", "created_at": "2026-01-04", "task_ids": ["T-other"],
    })
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")

    result = RemoteRuns(runtime).open("R-other", cache_root=tmp_path)

    assert not result.ok
    assert result.error.code == "not_found"
    assert not list(tmp_path.rglob("*.mp4"))


def test_open_fails_closed_on_ambiguous_video_outputs(monkeypatch, tmp_path: Path) -> None:
    runtime = _Runtime()
    original = runtime.get_task

    def ambiguous(task_id):
        task = original(task_id)
        task["result"]["outputs"].append(dict(task["result"]["outputs"][0]))
        return task

    runtime.get_task = ambiguous
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")

    result = RemoteRuns(runtime).open(cache_root=tmp_path)

    assert not result.ok
    assert result.error.code == "validation_error"


def test_open_verifies_downloaded_bytes(monkeypatch, tmp_path: Path) -> None:
    runtime = _Runtime()
    runtime.data = b"tampered-after-settlement"
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")

    result = RemoteRuns(runtime).open(cache_root=tmp_path)

    assert not result.ok
    assert result.error.code == "integrity_error"


def test_open_explicit_project_overrides_current_selection(monkeypatch, tmp_path: Path) -> None:
    runtime = _Runtime()
    runtime.current_project = lambda: (_ for _ in ()).throw(
        AssertionError("current selection must not be read")
    )
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")
    monkeypatch.setattr("astrid.sdk.project_render.subprocess.run", lambda argv, check: None)

    result = RemoteRuns(runtime).open(project="demo", cache_root=tmp_path)

    assert result.ok


def test_open_without_current_project_returns_typed_recovery(monkeypatch, tmp_path: Path) -> None:
    runtime = _Runtime()
    runtime.current_project = lambda: {"project": None, "scope": None}
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")

    result = RemoteRuns(runtime).open(cache_root=tmp_path)

    assert not result.ok
    assert result.error.code == "not_found"
    assert result.error.details["next_action"] == "astrid projects select <project>"


def test_open_default_timeline_uses_only_runs_with_explicit_provenance(monkeypatch, tmp_path: Path) -> None:
    runtime = _Runtime()
    runtime.runs = [
        {"run_id": "R-main-old", "project_id": "P-1", "capability_id": "rendering.render", "status": "completed", "created_at": "2026-01-01", "task_ids": ["T-main-old"], "spec": {"spec": {"timeline_ref": "TL-main"}}},
        {"run_id": "R-other-new", "project_id": "P-1", "capability_id": "rendering.render", "status": "completed", "created_at": "2026-01-04", "task_ids": ["T-other-new"], "spec": {"spec": {"timeline_ref": "TL-other"}}},
    ]
    runtime.get_project = lambda ref: {"project_id": "P-1", "slug": ref, "metadata": {"default_timeline_id": "TL-main"}}
    runtime.list_timelines = lambda project_id, *, cursor=None, limit=50: [[{"timeline_id": "TL-main", "slug": "main"}, {"timeline_id": "TL-other", "slug": "other"}], None]
    original_get_task = runtime.get_task
    # Preserve the producing task's bounded historical filename while this
    # fixture overrides spec only to exercise timeline scope selection.
    runtime.get_task = lambda task_id: {
        **original_get_task(task_id),
        "spec": {"spec": {
            "timeline_ref": "TL-main" if "main" in task_id else "TL-other",
            "inputs": {"output_name": "review.mp4"},
        }},
    }
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")
    monkeypatch.setattr("astrid.sdk.project_render.subprocess.run", lambda argv, check: None)

    result = RemoteRuns(runtime).open(project="demo", default_timeline=True, cache_root=tmp_path)

    assert result.ok
    assert result.data["run_id"] == "R-main-old"
    assert result.data["timeline_ref"] == "TL-main"


def test_open_timeline_rejects_render_without_authoritative_provenance(monkeypatch, tmp_path: Path) -> None:
    runtime = _Runtime()
    runtime.get_project = lambda ref: {"project_id": "P-1", "slug": ref, "metadata": {}}
    runtime.list_timelines = lambda project_id, *, cursor=None, limit=50: [[{"timeline_id": "TL-main", "slug": "main"}], None]
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")

    result = RemoteRuns(runtime).open(timeline="main", cache_root=tmp_path)

    assert not result.ok
    assert result.error.code == "not_found"
    assert "canonical timeline" in result.error.message


def test_open_default_timeline_requires_project_default(monkeypatch, tmp_path: Path) -> None:
    runtime = _Runtime()
    runtime.get_project = lambda ref: {"project_id": "P-1", "slug": ref, "metadata": {}}
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")

    result = RemoteRuns(runtime).open(project="demo", default_timeline=True, cache_root=tmp_path)

    assert not result.ok
    assert result.error.code == "not_found"
    assert "default canonical timeline" in result.error.message


def test_open_rejects_both_timeline_selectors(monkeypatch, tmp_path: Path) -> None:
    runtime = _Runtime()
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")

    result = RemoteRuns(runtime).open(
        project="demo", timeline="main", default_timeline=True, cache_root=tmp_path
    )

    assert not result.ok
    assert result.error.code == "validation_error"


def test_open_hydrates_lightweight_run_rows_for_provenance(monkeypatch, tmp_path: Path) -> None:
    runtime = _Runtime()
    runtime.get_project = lambda ref: {
        "project_id": "P-1", "slug": ref,
        "metadata": {"default_timeline_id": "TL-main"},
    }
    runtime.list_timelines = lambda project_id, *, cursor=None, limit=50: [
        [{"timeline_id": "TL-main", "slug": "main"}], None
    ]
    full_run = {
        "run_id": "R-hydrated", "project_id": "P-1",
        "capability_id": "rendering.render", "status": "completed",
        "created_at": "2026-01-01", "task_ids": ["T-hydrated"],
    }
    runtime.runs = [full_run]
    runtime.list_project_runs = lambda project_id, *, cursor=None, limit=50: [
        [{key: value for key, value in full_run.items() if key != "task_ids"}], None
    ]
    original_get_run = runtime.get_run
    runtime.get_run = lambda run_id: original_get_run(run_id) | {
        "task_ids": ["T-hydrated"], "spec": {"timeline_ref": "TL-main"}
    }
    original_get_task = runtime.get_task
    runtime.get_task = lambda task_id: original_get_task(task_id) | {
        "spec": {"spec": {"timeline_ref": "TL-main", "inputs": {"output_name": "review.mp4"}}}
    }
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")
    monkeypatch.setattr("astrid.sdk.project_render.subprocess.run", lambda argv, check: None)

    result = RemoteRuns(runtime).open(project="demo", default_timeline=True, cache_root=tmp_path)

    assert result.ok
    assert result.data["run_id"] == "R-hydrated"


def _canonical_runtime(tmp_path: Path) -> _Runtime:
    runtime = _Runtime()
    digest = runtime.digest.removeprefix("sha256:")
    canonical = tmp_path / "runtime" / "realms" / "realm-1" / "cas" / "sha256" / digest[:2] / digest[2:]
    canonical.parent.mkdir(parents=True)
    canonical.write_bytes(runtime.data)
    runtime._canonical_path = canonical
    return runtime


def test_os_opener_success_reports_opened_true(monkeypatch, tmp_path: Path) -> None:
    runtime = _canonical_runtime(tmp_path)
    launched: list[list[str]] = []
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")
    monkeypatch.setattr("astrid.sdk.project_render.subprocess.run", lambda argv, check: launched.append(argv))

    result = open_render(runtime, "P-1")

    assert result.ok
    assert launched == [["open", "-b", "com.apple.QuickTimePlayerX", result.data["local_path"]]]
    assert result.data["open_requested"] is True
    assert result.data["opened"] is True
    assert result.data["open_error"] is None


def test_os_opener_failure_reports_reason_and_path(monkeypatch, tmp_path: Path) -> None:
    runtime = _canonical_runtime(tmp_path)
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")

    def refuse(argv, check):
        raise subprocess.CalledProcessError(1, argv)

    monkeypatch.setattr("astrid.sdk.project_render.subprocess.run", refuse)

    result = open_render(runtime, "P-1")

    assert result.ok
    assert result.data["opened"] is False
    assert result.data["open_requested"] is True
    assert "could not open with the OS opener" in result.data["open_error"]
    assert "CalledProcessError" in result.data["open_error"]
    assert "open local_path by hand" in result.data["open_error"]
    assert Path(result.data["local_path"]).exists()


def test_linux_without_gui_session_reports_reason(monkeypatch, tmp_path: Path) -> None:
    runtime = _canonical_runtime(tmp_path)
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    launched: list[list[str]] = []
    monkeypatch.setattr("astrid.sdk.project_render.subprocess.run", lambda argv, check: launched.append(argv))

    result = open_render(runtime, "P-1")

    assert result.ok
    assert launched == []
    assert result.data["opened"] is False
    assert "no GUI session" in result.data["open_error"]


def test_linux_opener_uses_xdg_open_when_gui_present(monkeypatch, tmp_path: Path) -> None:
    runtime = _canonical_runtime(tmp_path)
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("astrid.sdk.project_render.shutil.which", lambda name: f"/usr/bin/{name}")
    launched: list[list[str]] = []
    monkeypatch.setattr("astrid.sdk.project_render.subprocess.run", lambda argv, check: launched.append(argv))

    result = open_render(runtime, "P-1")

    assert result.ok
    assert launched == [["xdg-open", result.data["local_path"]]]
    assert result.data["opened"] is True


def test_missing_xdg_open_reports_reason(monkeypatch, tmp_path: Path) -> None:
    runtime = _canonical_runtime(tmp_path)
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Linux")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("astrid.sdk.project_render.shutil.which", lambda name: None)
    monkeypatch.setattr("astrid.sdk.project_render.subprocess.run", lambda argv, check: pytest.fail("must not launch"))

    result = open_render(runtime, "P-1")

    assert result.ok
    assert result.data["opened"] is False
    assert "xdg-open is not installed" in result.data["open_error"]


def test_windows_opener_uses_startfile(monkeypatch, tmp_path: Path) -> None:
    runtime = _canonical_runtime(tmp_path)
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Windows")
    started: list[str] = []
    monkeypatch.setattr("astrid.sdk.project_render.os.startfile", lambda path: started.append(path), raising=False)

    result = open_render(runtime, "P-1")

    assert result.ok
    assert started == [result.data["local_path"]]
    assert result.data["opened"] is True


def test_no_open_materializes_without_launching(monkeypatch, tmp_path: Path) -> None:
    runtime = _canonical_runtime(tmp_path)
    monkeypatch.setattr("astrid.sdk.project_render.platform.system", lambda: "Darwin")
    monkeypatch.setattr("astrid.sdk.project_render.subprocess.run", lambda argv, check: pytest.fail("--no-open must not launch"))
    opened: list[Path] = []

    result = open_render(runtime, "P-1", no_open=True, opener=opened.append)

    assert result.ok
    assert opened == []
    assert Path(result.data["local_path"]).exists()
    assert result.data["open_requested"] is False
    assert result.data["opened"] is False
    assert "--no-open" in result.data["open_error"]
