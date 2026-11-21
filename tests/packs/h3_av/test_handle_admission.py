"""H3 request source and mask handles are resolved before executor admission."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk.generation import GenerationFacade


def _digest(byte: bytes) -> str:
    return "sha256:" + hashlib.sha256(byte).hexdigest()


class _Media:
    def __init__(self, digests: dict[str, bytes]):
        self.digests = digests
        self.show_calls: list[tuple[str, str]] = []
        self.open_calls: list[tuple[str, str, Path]] = []

    def show(self, project: str, digest: str):
        self.show_calls.append((project, digest))
        if digest not in self.digests:
            return SimpleNamespace(ok=False, error=SimpleNamespace(message="not found"))
        return SimpleNamespace(ok=True, data={
            "digest": digest, "filename": "generated_images", "media_type": "image/png",
            "size": len(self.digests[digest]),
        })

    def open(self, project: str, digest: str, **kwargs):
        root = Path(kwargs["cache_root"])
        root.mkdir(parents=True)
        path = root / "media.png"
        path.write_bytes(self.digests[digest])
        self.open_calls.append((project, digest, path))
        return SimpleNamespace(ok=True, data={"local_path": str(path)})


class _Client:
    def __init__(self, digests: dict[str, bytes]):
        self.media = _Media(digests)
        self.tasks = SimpleNamespace(list_managed_outputs=self._outputs)
        self.runs = SimpleNamespace(show=self._run)

    @staticmethod
    def _run(run_id: str):
        return {"task_ids": ["prior-task"] if run_id == "prior-run" else []}

    @staticmethod
    def _outputs(task_id: str):
        if task_id != "prior-task":
            return []
        return [{"run_id": "prior-run", "task_id": task_id, "output_port": "primary",
                 "ordinal": 0, "digest": _digest(b"prior"), "filename": "prior.png"}]


def _inputs(tmp_path: Path, source: str, mask: str, supplied: dict[str, str] | None = None) -> dict:
    request = {
        "version": 1,
        "operation": "edit",
        "source": {"asset": source, "range": [0, 1]},
        "output": {"duration": 1},
        "content": {"prompt": "Keep the scene."},
        "changes": {"video": [{"during": [0, 1], "area": {"full_frame": True},
                                  "action": "generate", "mask_asset": mask}], "audio": []},
        "references": [],
        "overrides": {},
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request), encoding="utf-8")
    return {"request": str(path), "asset_map": {"existing": "/already/staged.png", **(supplied or {})}}


@pytest.fixture
def admission(monkeypatch):
    """Replace only target selection and the implementation boundary; use real handle resolution."""
    from astrid.sdk import generation

    selected = "selected-project"
    monkeypatch.setattr(generation, "_resolve_invoke_destination",
                        lambda **kwargs: (kwargs.get("out"), selected))
    calls = []

    def impl(self, **kwargs):
        calls.append(kwargs)
        return "admitted"

    monkeypatch.setattr(GenerationFacade, "_generate_on_target_impl", impl)
    return selected, calls, monkeypatch


def test_digest_source_and_prior_run_mask_are_adapted_and_cleaned(admission, tmp_path):
    selected, calls, monkeypatch = admission
    source_digest, mask_digest = _digest(b"source"), _digest(b"prior")
    client = _Client({source_digest: b"source", mask_digest: b"prior"})
    facade = GenerationFacade()
    supplied_path = "/user/assets/keep.png"
    inputs = _inputs(tmp_path, source_digest, "run:prior-run/primary#0", {"user-key": supplied_path})
    observed_paths = []

    def impl(self, **kwargs):
        call = dict(kwargs)
        mapped = call["inputs"]["asset_map"]
        assert mapped["existing"] == "/already/staged.png"
        assert mapped["user-key"] == supplied_path
        assert Path(mapped[source_digest]).is_file()
        assert Path(mapped["run:prior-run/primary#0"]).is_file()
        observed_paths.extend([Path(mapped[source_digest]), Path(mapped["run:prior-run/primary#0"])])
        calls.append(call)
        return "admitted"

    # This override observes files at the admission boundary while retaining the controlled setup.
    monkeypatch.setattr(GenerationFacade, "_generate_on_target_impl", impl)
    result = facade._generate_on_target(model="h3", inputs=inputs, client=client, project="requested",
                                        out=None, project_root=None, idempotency_context={})

    assert result == "admitted"
    assert len(calls) == 1
    call = calls[0]
    assert call["project"] == selected
    assert call["idempotency_context"]["media_handles"] == [
        {"port": "request.asset", "handle": source_digest, "digest": source_digest},
        {"port": "request.asset", "handle": "run:prior-run/primary#0", "digest": mask_digest,
         "output": {"run_id": "prior-run", "task_id": "prior-task", "output_port": "primary", "ordinal": 0}},
    ]
    assert client.media.show_calls == [(selected, source_digest), (selected, mask_digest)]
    assert client.media.open_calls[0][0] == client.media.open_calls[1][0] == selected
    assert all(not path.exists() for path in observed_paths)


@pytest.mark.parametrize("handle_kind", ["wrong_project", "unknown_run"])
def test_unresolvable_handle_refuses_before_executor_and_cleans_temp_assets(admission, tmp_path, handle_kind):
    selected, calls, _ = admission
    source_digest = _digest(b"source")
    client = _Client({source_digest: b"source"})
    mask = _digest(b"belongs-elsewhere") if handle_kind == "wrong_project" else "run:missing-run/primary#0"
    from astrid.sdk.media_handles import MediaHandleError
    with pytest.raises(MediaHandleError):
        GenerationFacade()._generate_on_target(model="h3", inputs=_inputs(tmp_path, source_digest, mask),
                                               client=client, project="requested", out=None,
                                               project_root=None, idempotency_context={})
    assert calls == []
    assert len(client.media.open_calls) == 1
    assert client.media.open_calls[0][:2] == (selected, source_digest)
    assert not client.media.open_calls[0][2].exists()


@pytest.fixture(params=["automatic", "pinned"])
def selected_video(monkeypatch, request):
    """Use the public placement route through real default and contribution selection."""
    from astrid.core.generation import preflight, settings

    pins = {"video.v2v": {"pack": "h3_av", "entry": "h3"}} if request.param == "pinned" else {}
    monkeypatch.setattr(settings, "read", lambda *args, **kwargs: settings.Snapshot({"defaults": pins}, 1, "runtime"))
    monkeypatch.setattr(preflight, "select_generation_target", lambda *args, **kwargs: None)
    monkeypatch.setattr(preflight, "require_generation_target_readiness", lambda *args, **kwargs: None)
    return GenerationFacade().video


@pytest.mark.parametrize("as_document", [False, True])
def test_selected_video_rejects_reserved_handoff(selected_video, monkeypatch, tmp_path, as_document):
    from astrid.core.generation import entries
    from astrid.sdk.exceptions import CapabilityValidationError

    source = _digest(b"source")
    inputs = _inputs(tmp_path, source, "ordinary-mask", {"__astrid_h3_prepared__": "/caller/handoff.json"})
    if as_document:
        document = tmp_path / "assets.json"
        document.write_text(json.dumps(inputs["asset_map"]), encoding="utf-8")
        inputs["asset_map"] = str(document)

    def build(*args, **kwargs):
        pytest.fail("caller-supplied handoff reached build")

    monkeypatch.setattr(entries, "build_entry", build)
    with pytest.raises(CapabilityValidationError, match="handoff is reserved"):
        selected_video(placement="local", mode="v2v", dry_run=True, **inputs)


def test_selected_video_checks_project_custody_before_build(selected_video, monkeypatch, tmp_path):
    from astrid.core.generation import entries
    from astrid.sdk.media_handles import MediaHandleError

    source = _digest(b"another-project")
    client = _Client({})
    inputs = _inputs(tmp_path, source, "ordinary-mask", {source: "/caller/source.mp4"})

    def build(*args, **kwargs):
        pytest.fail("caller path for a handle outside project custody reached build")

    monkeypatch.setattr(entries, "build_entry", build)
    with pytest.raises(MediaHandleError):
        selected_video(placement="local", mode="v2v", dry_run=True,
                       project="selected-project", client=client, **inputs)
    assert client.media.show_calls == [("selected-project", source)]
    assert client.media.open_calls == []


def test_selected_video_stages_authoritative_handle_and_lineage(selected_video, monkeypatch, tmp_path):
    from astrid.core.generation import entries
    from astrid.sdk import generation

    class ObservedPublication(Exception):
        pass

    source = _digest(b"source")
    run_handle = "run:prior-run/primary#0"
    prior = _digest(b"prior")
    client = _Client({source: b"source", prior: b"prior"})
    inputs = _inputs(tmp_path, source, run_handle, {source: "/caller/source.mp4",
                                                   run_handle: "/caller/mask.png"})

    def build(entry, values, job_dir, *, publish_source, **kwargs):
        assert entry["id"] == "h3"
        assert values["asset_map"]["existing"] == "/already/staged.png"
        assert Path(values["asset_map"][source]).read_bytes() == b"source"
        assert Path(values["asset_map"][run_handle]).read_bytes() == b"prior"
        publish_source(job_dir / "prepared.mp4", source.removeprefix("sha256:"))
        pytest.fail("publication stub must stop before execution")

    def import_member(path, controls):
        assert path.name == "prepared.mp4"
        assert controls["project"] == "selected-project"
        assert controls["idempotency_context"]["media_handles"] == [
            {"port": "request.asset", "handle": source, "digest": source},
            {"port": "request.asset", "handle": run_handle, "digest": prior,
             "output": {"run_id": "prior-run", "task_id": "prior-task", "output_port": "primary", "ordinal": 0}},
        ]
        raise ObservedPublication

    monkeypatch.setattr(entries, "build_entry", build)
    monkeypatch.setattr(generation, "_import_generation_member", import_member)
    with pytest.raises(ObservedPublication):
        selected_video(placement="local", mode="v2v", project="selected-project", client=client, **inputs)
    assert client.media.show_calls == [("selected-project", source), ("selected-project", prior)]
    assert len(client.media.open_calls) == 2
    assert all(not path.exists() for _, _, path in client.media.open_calls)


@pytest.mark.parametrize("cli_target", ["local", "runpod"])
def test_invalid_request_preserves_structured_error_in_sdk_and_cli(tmp_path, capsys, cli_target):
    from astrid.core.generation import cli
    from astrid.core.generation.preflight import GenerationReadinessError

    request = tmp_path / "invalid.json"
    request.write_text("{", encoding="utf-8")
    with pytest.raises(GenerationReadinessError) as caught:
        GenerationFacade().video(entry="h3", placement="local", request=str(request), dry_run=True)

    inputs = tmp_path / "inputs.json"
    inputs.write_text(json.dumps({"request": str(request)}), encoding="utf-8")
    options = ["--dry-run"] if cli_target == "local" else []
    assert cli.main(["video", "--entry", "h3", "--placement", cli_target, "--inputs-json", str(inputs),
                     "--json", *options]) == 2
    captured = capsys.readouterr()
    sdk_error = caught.value.to_envelope()
    issue, = sdk_error["issues"]
    assert caught.value.fix == "Correct the request using its v1/v2 schema"
    assert issue["code"] == "h3.request.invalid"
    assert issue["message"].startswith("request is not valid JSON/YAML")
    assert issue["details"] == {"field": "request", "fix": caught.value.fix}
    assert json.loads(captured.out) == sdk_error
    assert "Traceback" not in captured.err


def test_selected_video_build_uses_inspected_documents(selected_video, monkeypatch, tmp_path):
    from astrid.core.generation import entries, preflight

    class ObservedBuild(Exception):
        pass

    inputs = _inputs(tmp_path, "source", "ordinary-mask")
    request = Path(inputs["request"])
    expected = request.read_bytes()
    document = tmp_path / "assets.json"
    document.write_text(json.dumps({"source": "original.mp4"}), encoding="utf-8")
    snapshots = []

    def change_documents(*args, **kwargs):
        edited = json.loads(expected)
        edited["source"]["asset"] = _digest(b"outside-project")
        request.write_text(json.dumps(edited), encoding="utf-8")
        document.write_text(json.dumps({"__astrid_h3_prepared__": "replace.json"}), encoding="utf-8")

    def build(entry, values, job_dir, **kwargs):
        assert values["asset_map"] == {"source": str(tmp_path / "original.mp4")}
        snapshot = Path(values["request"])
        assert snapshot.suffix == request.suffix
        assert snapshot.read_bytes() == expected
        snapshots.append(snapshot)
        raise ObservedBuild

    monkeypatch.setattr(preflight, "require_generation_target_readiness", change_documents)
    monkeypatch.setattr(entries, "build_entry", build)
    with pytest.raises(ObservedBuild):
        selected_video(placement="local", mode="v2v", dry_run=True,
                       request=inputs["request"], asset_map=str(document))
    assert len(snapshots) == 1 and not snapshots[0].exists()


def test_selected_video_reuses_admission_for_remote_live_build(selected_video, monkeypatch, tmp_path):
    from astrid.core.generation import entries, placement
    from astrid.sdk import generation

    class ObservedDryBuild(Exception):
        pass

    class ObservedLiveBuild(Exception):
        pass

    source = _digest(b"source")
    client = _Client({source: b"source"})
    inputs = _inputs(tmp_path, source, "ordinary-mask")
    request = Path(inputs["request"])
    expected = request.read_bytes()
    document = tmp_path / "assets.json"
    document.write_text(json.dumps({"ordinary-mask": "original.png"}), encoding="utf-8")
    target = placement.Placement(kind="runpod", machine="example-machine")
    monkeypatch.setattr(placement, "resolve_placement", lambda *args, **kwargs: target)
    monkeypatch.setattr(placement, "validate_placement", lambda value: value)
    monkeypatch.setattr(generation, "_require_claimed_machine", lambda *args: None)
    monkeypatch.setattr(entries, "runpod_engine", lambda *args: "comfyui")

    original_impl = GenerationFacade._generate_on_target_impl

    def end_dry_preflight(self, **kwargs):
        try:
            return original_impl(self, **kwargs)
        except ObservedDryBuild:
            assert kwargs["dry_run"]
            return {}

    staged = []
    snapshots = []

    def build(entry, values, job_dir, *, publish_source=None, **kwargs):
        mapping = values["asset_map"]
        assert mapping["ordinary-mask"] == str(tmp_path / "original.png")
        staged.append(mapping[source])
        assert Path(staged[-1]).read_bytes() == b"source"
        snapshots.append(Path(values["request"]))
        assert snapshots[-1].read_bytes() == expected
        if publish_source is None:
            request.unlink()
            document.unlink()
            client.media.digests.clear()
            raise ObservedDryBuild
        raise ObservedLiveBuild

    monkeypatch.setattr(GenerationFacade, "_generate_on_target_impl", end_dry_preflight)
    monkeypatch.setattr(entries, "build_entry", build)
    with pytest.raises(ObservedLiveBuild):
        selected_video(placement="runpod", mode="v2v", project="selected-project", client=client,
                       request=inputs["request"], asset_map=str(document))
    assert client.media.show_calls == [("selected-project", source)]
    assert len(staged) == 2 and staged[0] == staged[1]
    assert not Path(staged[0]).exists()
    assert len(snapshots) == 2 and snapshots[0] == snapshots[1]
    assert not snapshots[0].exists()
