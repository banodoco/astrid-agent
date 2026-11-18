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
