from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from astrid.core.execution.generic_host import (
    GenericPackHost,
    HostError,
    _prepare_vibecomfy_execution_identity,
)
from astrid.packs.vibecomfy import production_engine
from astrid.packs.vibecomfy.shared.bundle_inputs import staged_workflow_path
from tests.test_generic_host import FakeRuntime


class RecordingFakeRuntime(FakeRuntime):
    """The generic-host fake runtime with byte-level object observations."""

    def __init__(self, objects: dict[str, bytes]):
        super().__init__()
        self.objects = dict(objects)
        self.fetched_inputs: list[str] = []
        self.uploaded_outputs: list[dict[str, object]] = []

    def get_object(self, digest: str) -> bytes:
        normalized = digest.removeprefix("sha256:")
        self.fetched_inputs.append(normalized)
        return self.objects[normalized]

    def upload_object(self, path, *, project_id, media_type, filename=None):
        data = Path(path).read_bytes()
        row = super().upload_object(
            path,
            project_id=project_id,
            media_type=media_type,
            filename=filename,
        )
        self.uploaded_outputs.append(
            {
                "filename": filename,
                "media_type": media_type,
                "project_id": project_id,
                "data": data,
                "digest": row.digest,
                "size": row.size,
            }
        )
        return row


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("value", ["/Users/caller/request.json", "relative.json", {"path": "/tmp/request.json"}])
def test_host_rejects_unmanaged_declared_file_before_fetch(tmp_path: Path, value):
    runtime = RecordingFakeRuntime({})
    host = GenericPackHost(pack_roots=[], client=runtime)
    with pytest.raises(HostError, match="client.media.import_file"):
        host._materialize_inputs({"inputs": {"request": value}}, tmp_path / "attempt",
                                 file_input_names=frozenset({"request"}))
    assert runtime.fetched_inputs == []
    assert not (tmp_path / "attempt").exists()


def test_host_materializes_prefixed_digest_and_preserves_literal_json(tmp_path: Path):
    payload = b'{}'
    digest = "sha256:" + _sha256(payload)
    runtime = RecordingFakeRuntime({_sha256(payload): payload})
    host = GenericPackHost(pack_roots=[], client=runtime)
    settings = {"literal": "/Users/caller/example"}
    values = host._materialize_inputs(
        {"inputs": {"request": digest, "settings": settings}, "input_object_ids": [digest]},
        tmp_path / "attempt", file_input_names=frozenset({"request"}),
    )
    assert Path(values["request"]).read_bytes() == payload
    assert values["settings"] == settings


def test_materialization_does_not_treat_revision_hash_as_file_digest(
    tmp_path: Path,
) -> None:
    source_bytes = b"canonical Python workflow\n"
    source_digest = _sha256(source_bytes)
    revision_id = "a" * 64
    runtime = RecordingFakeRuntime({source_digest: source_bytes})
    host = GenericPackHost(pack_roots=[], client=runtime)

    values = host._materialize_inputs(
        {
            "input_object_ids": [source_digest],
            "spec": {
                "inputs": {
                    "python": source_digest,
                    "parent_revision": revision_id,
                }
            },
        },
        tmp_path / "attempt",
        authorized_input_object_ids=[source_digest],
        file_input_names=frozenset({"python"}),
    )

    assert Path(values["python"]).read_bytes() == source_bytes
    assert values["parent_revision"] == revision_id
    assert runtime.fetched_inputs == [source_digest]


def test_host_child_rejects_modified_canonical_member_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = {
        "python": tmp_path / "workflow.py",
        "companion": tmp_path / "workflow.vibe.json",
        "source": tmp_path / "source.json",
    }
    members["python"].write_bytes(b"workflow = load('fixture')\n")
    members["companion"].write_bytes(b'{"revision_id":"rev-1"}\n')
    members["source"].write_bytes(b'{"nodes":[],"links":[]}\n')

    def fake_loader(path, _scratch, **_kwargs):
        return production_engine.LoadedWorkflow(
            resolved=object(),
            model_id="vibecomfy",
            template_id="fixture-workflow",
            workflow_identity="fixture-workflow",
            workflow_revision="rev-1",
            workflow_content_digest=production_engine._workflow_content_digest(
                Path(path)
            ),
        )

    monkeypatch.setattr(production_engine, "load_workflow_path", fake_loader)
    inputs = {"workflow": "", **members}
    host_identity, model_id, template_id, session_requirements = _prepare_vibecomfy_execution_identity(
        inputs,
        tmp_path / "host-attempt",
        None,
    )
    assert (model_id, template_id) == ("vibecomfy", "fixture-workflow")
    assert session_requirements["model_id"] == "vibecomfy"

    members["source"].write_bytes(b'{"nodes":[{"id":1}],"links":[]}\n')
    launch = Mock()
    monkeypatch.setattr(production_engine, "_run_profile", launch)
    with staged_workflow_path(
        workflow=None,
        python=members["python"],
        companion=members["companion"],
        source=members["source"],
        scratch=tmp_path / "child-attempt",
    ) as (workflow_path, _authority):
        with pytest.raises(
            production_engine.ProductionEngineError,
            match="execution identity changed before launch",
        ):
            production_engine.run_workflow_path(
                workflow_path,
                tmp_path / "child-attempt" / "outputs",
                task_identity="task-1",
                expected_execution_identity=host_identity,
            )
    launch.assert_not_called()


def test_generic_host_materializes_named_sibling_inputs_and_settles_four_outputs(
    tmp_path: Path,
) -> None:
    input_bytes = {
        "workflow.py": b"workflow = 'synthetic'\n",
        "workflow.vibe.json": b'{"companion":"sibling"}\n',
        "source.json": b'{"origin":"fixture"}\n',
    }
    input_digests = {name: _sha256(data) for name, data in input_bytes.items()}
    input_ports = dict(zip(input_bytes, ("workflow", "companion", "source")))
    staged_names = {
        name: f"managed-{_sha256(port.encode())[:16]}-{input_digests[name]}{Path(name).suffix}"
        for name, port in input_ports.items()
    }
    outputs = {
        "python": ("workflow.py", "text/x-python", input_bytes["workflow.py"]),
        "companion": (
            "workflow.vibe.json",
            "application/json",
            input_bytes["workflow.vibe.json"],
        ),
        "source": ("source.json", "application/json", input_bytes["source.json"]),
    }
    report = {
        "inputs": {
            name: {
                "basename": staged_names[name],
                "sha256": input_digests[name],
                "text": data.decode("utf-8"),
            }
            for name, data in input_bytes.items()
        }
    }
    outputs["report"] = (
        "edit-report.json",
        "application/json",
        json.dumps(report, sort_keys=True).encode("utf-8"),
    )

    # Bind the host-staged inputs explicitly and preserve canonical output
    # basenames. This child has no VibeComfy import or runtime need.
    command = (
        "from pathlib import Path; import hashlib, json, sys; "
        "out=Path('{out}'); "
        "names=['workflow.py','workflow.vibe.json','source.json']; "
        "paths=dict(zip(names,map(Path,sys.argv[1:]))); "
        "data=dict((name,paths[name].read_bytes()) for name in names); "
        "out.mkdir(parents=True,exist_ok=True); "
        "(out/'workflow.py').write_bytes(data['workflow.py']); "
        "(out/'workflow.vibe.json').write_bytes(data['workflow.vibe.json']); "
        "(out/'source.json').write_bytes(data['source.json']); "
        "receipt=dict(inputs=dict((name,dict(basename=paths[name].name,sha256="
        "hashlib.sha256(data[name]).hexdigest(),text=data[name].decode('utf-8'))) "
        "for name in names)); "
        "(out/'edit-report.json').write_text(json.dumps(receipt,sort_keys=True),encoding='utf-8')"
    )
    capability_root = tmp_path / "fixture"
    capability_root.mkdir()
    manifest = {
        "schema_version": 1,
        "id": "test.canonical_bundle",
        "name": "Canonical Bundle Fixture",
        "kind": "external",
        "version": "1.0",
        "command": {"argv": ["{python_exec}", "-c", command, "{workflow}", "{companion}", "{source}"]},
        "inputs": [{"name": port, "type": "file"} for port in input_ports.values()],
        "outputs": [
            {
                "name": name,
                "type": "file",
                "path_template": "{out}/" + filename,
                "artifact_type": media_type,
            }
            for name, (filename, media_type, _data) in outputs.items()
        ],
        "metadata": {
            "adapter_family": "cpu",
            "resource_keys": ["cpu"],
            "estimated_scratch_bytes": 1,
        },
    }
    (capability_root / "executor.yaml").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    runtime = RecordingFakeRuntime(
        {input_digests[name]: data for name, data in input_bytes.items()}
    )
    attempt_root = tmp_path / "attempt"
    host = GenericPackHost(
        pack_roots=[tmp_path],
        client=runtime,
        attempt_root=attempt_root,
    )
    host.discover()
    task = {
        "task": {
            "id": "canonical-bundle-task",
            "capability": "test.canonical_bundle",
            "project_id": "fixture-project",
            "attempt_id": "canonical-bundle-attempt",
            "fence": 1,
            "input_object_ids": list(input_digests.values()),
            "spec": {
                "spec": {
                    "inputs": {
                        port: {"digest": input_digests[name], "filename": name}
                        for name, port in input_ports.items()
                    },
                }
            },
        }
    }
    runtime.tasks["canonical-bundle-task"] = task

    settled = host.run_task(task, lease_token="fixture-lease")

    assert settled["task"]["status"] == "completed"
    assert runtime.fetched_inputs == [input_digests[name] for name in input_bytes]
    staged = [attempt_root / "inputs" / staged_names[name] for name in input_bytes]
    assert all(path.is_file() for path in staged)
    assert len({path.parent for path in staged}) == 1
    assert [path.name for path in staged] == list(staged_names.values())
    assert [path.read_bytes() for path in staged] == list(input_bytes.values())

    # The report is produced by the child from those exact staged paths and bytes.
    assert (attempt_root / "outputs" / "edit-report.json").read_bytes() == outputs[
        "report"
    ][2]

    product_uploads = [
        item for item in runtime.uploaded_outputs
        if item["filename"] in {output[0] for output in outputs.values()}
    ]
    assert [item["filename"] for item in product_uploads] == [
        item[0] for item in outputs.values()
    ]
    assert [item["data"] for item in product_uploads] == [
        item[2] for item in outputs.values()
    ]
    assert [item["digest"] for item in product_uploads] == [
        _sha256(item[2]) for item in outputs.values()
    ]
    assert all(
        item["project_id"] == "fixture-project" for item in product_uploads
    )

    settled_outputs = runtime.settlements[0][2]["outputs"]
    product_outputs = [item for item in settled_outputs if item.get("role") != "log"]
    log_outputs = [item for item in settled_outputs if item.get("role") == "log"]
    assert {item["filename"] for item in log_outputs} == {"job.stdout.log", "job.stderr.log"}
    assert [item["name"] for item in product_outputs] == list(outputs)
    assert [item["digest"] for item in product_outputs] == [
        _sha256(item[2]) for item in outputs.values()
    ]
    assert [item["size"] for item in product_outputs] == [
        len(item[2]) for item in outputs.values()
    ]
    assert [item["media_type"] for item in product_outputs] == [
        item[1] for item in outputs.values()
    ]
    assert all("path" not in item for item in product_outputs)
