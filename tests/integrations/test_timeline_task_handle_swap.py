"""Generate, then use it: swap a task output into a timeline and commit, through the public CLI.

A CPU substitute stands in for the image generator. Everything else is real: the Runtime
daemon, the GenericPackHost command boundary, the ``generation.publish_v1`` settlement that
records the outputs as catalog generation variants, and ``python -m astrid timelines edit``
then ``timelines commit`` in child processes, as a user or agent runs them.

The Runtime-verified-inputs rule stays intact: a task output the Runtime published as a
generation variant is admitted through its exact catalog selection, and media the Runtime
never verified as a variant is still refused at commit.
"""
from __future__ import annotations

import json
import os
import shlex
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid_runtime_client import WorkspaceClient

pytestmark = pytest.mark.integration  # needs the Runtime from a second checkout (real_runtime fixture)

ASTRID = Path(__file__).resolve().parents[2]
REFUSAL = "patch references media outside Runtime-verified inputs"
CLI_SCOPES = ["handshake", "projects:read", "projects:write", "objects:read", "objects:write",
              "tasks:read", "tasks:write"]


def _png(rgba: tuple[int, int, int, int], size: int = 4) -> bytes:
    """A small solid PNG without an image-library dependency."""
    raw = b"".join(b"\x00" + bytes(rgba) * size for _ in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _image_pack(root: Path) -> Path:
    """One CPU image generator: two distinct PNGs on ``generated_images``."""
    import yaml

    from astrid.pack_api import VERSION

    pack = root / "swapgen"
    pack.mkdir(parents=True)
    # Same pixels, distinct bytes: two distinct outputs whose Runtime thumbnails coincide (one thumbnail row).
    images = [(_png((200, 40, 40, 255)) + bytes([ordinal])).hex() for ordinal in range(2)]
    runner = pack / "cpu_image_runner.py"
    runner.write_text(f"""
import hashlib, json, sys
from pathlib import Path
out = Path(sys.argv[2])
(out / 'images').mkdir(parents=True, exist_ok=True)
entries = []
for ordinal, data in enumerate({images!r}):
    raw = bytes.fromhex(data)
    (out / 'images' / f'cpu-{{ordinal}}.png').write_bytes(raw)
    entries.append({{'name': 'generated_images', 'path': f'cpu-{{ordinal}}.png', 'ordinal': ordinal,
                    'content_hash': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
                    'is_primary': ordinal == 0}})
(out / 'manifest.json').write_text(json.dumps({{'outputs': [
    {{'name': 'generated_images', 'artifact_type': 'image/png', 'path': 'images', 'entries': entries}}]}}),
    encoding='utf-8')
""", encoding="utf-8")
    action = {
        "description": "Generate Image (CPU substitute)",
        "short_description": "Generate Image (CPU substitute)",
        "name": "Generate Image (CPU substitute)",
        "version": "2.0",
        "inputs": [{"name": "task_spec_json", "required": True, "type": "string"},
                   {"name": "prompt", "required": True, "type": "string"}],
        "outputs": [{"name": "generated_images", "type": "file", "path_template": "{out}/images/cpu-0.png",
                     "artifact_type": "image/png"}],
        "metadata": {"resource_keys": ["cpu"], "estimated_scratch_bytes": 1024 * 1024,
                     "estimated_output_bytes": 4096, "hc04_param_ports": ["prompt"]},
        "invocation": {"kind": "command",
                       "command": {"argv": ["{python_exec}", str(runner), "{task_spec_json}", "{out}", "{prompt}"]}},
    }
    manifest = {"schema_version": 3, "id": "swapgen", "name": "Swap CPU generator", "version": "1.0.0",
                "pack_api": f"{VERSION[0]}.{VERSION[1]}",
                "documentation": {"kind": "none", "reason": "CPU test fixture."},
                "status": "active", "domain": "media", "stability": "experimental", "support": "core",
                "visibility": "visible", "description": "CPU substitute for an image generator.",
                "capabilities": [], "actions": {"generate_image": action}}
    (pack / "pack.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return pack


def _publish_v1(project_id: str, count: int) -> tuple[dict, dict]:
    """The generation intent and settlement effect the SDK composes for an image request."""
    selectors = [{"selector": f"main-{n}", "ordinal": n, "variant_key": "original" if n == 0 else f"variant-{n}"}
                 for n in range(count)]
    intent = {"version": 1, "modality": "image", "partial_success_policy": "reject",
              "groups": [{"group_key": "main", "selectors": selectors}]}
    effect = {"effect_type": "generation.publish_v1", "target_id": project_id, "payload": {
        "version": 1, "modality": "image", "generation_type": "swapgen.generate_image", "metadata": {},
        "partial_success_policy": "reject",
        "groups": [{"group_key": "main", "selectors": [
            {**selector, "output_port": "generated_images"} for selector in selectors]}]}}
    return intent, effect


class World:
    """One real Runtime realm, a CPU generation host, and a CLI environment pointed at it."""

    def __init__(self, tmp_path: Path, real_runtime) -> None:
        self.tmp = tmp_path
        self.daemon = real_runtime(authoring_profile="astrid")
        self.owner = WorkspaceClient(self.daemon.endpoint, self.daemon.token)
        self.owner.handshake("task-handle-swap-test", "0.1.0",
                             ["projects:read", "projects:write", "objects:read", "objects:write",
                              "tasks:read", "tasks:write", "worker:execute"])
        self.project = self.owner.create_project("Task handle swap", slug="task-handle-swap",
                                                 idempotency_key="task-handle-swap-project").project_id
        pack = _image_pack(tmp_path / "packs")
        self.host = GenericPackHost(
            pack_roots=[pack],
            client=RuntimeProtocolClient(self.daemon.endpoint, self._worker_token("swap-cpu-host")),
            executor_id="swap-cpu-host",
        )
        self.record = self.host.discover()[0]
        self.host.preflight()
        assert self.host.capabilities[self.record.id].ready
        self.host.register()
        _, credential = self.daemon.credentials.provision("task-handle-cli", CLI_SCOPES)
        home = tmp_path / "cli-home"
        home.mkdir()
        self.env = {
            **{key: value for key, value in os.environ.items() if not key.startswith("ASTRID_TIMELINE_EVAL_")},
            "HOME": str(home),
            "ASTRID_RUNTIME_DATA_ROOT": str(tmp_path / "astrid-data"),
            "ASTRID_TIMELINE_EVAL_ENDPOINT": self.daemon.endpoint,
            "ASTRID_TIMELINE_EVAL_CREDENTIAL": str(credential),
            "ASTRID_TIMELINE_EVAL_REALM_ID": self.daemon.service.realm["id"],
            "ASTRID_TIMELINE_EVAL_ACTOR_ID": "task-handle-cli",
            "PYTHONPATH": os.pathsep.join([str(ASTRID), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep),
        }
        self.transcript: list[str] = []

    def _worker_token(self, executor_id: str) -> str:
        from runtime_protocol.daemon import WORKER_SCOPES

        token, _path = self.daemon.credentials.provision(executor_id, list(WORKER_SCOPES))
        return token

    def generate(self, key: str, *, publish: bool = True) -> dict:
        """Admit and run one generation task; with ``publish`` the outputs become catalog variants."""
        intent, effect = _publish_v1(self.project, 2)
        options = {"generation_intent": intent, "settlement_effect": effect} if publish else {}
        task = self.owner.admit_task(
            capability_id=self.record.id, capability_digest=self.record.capability_digest,
            input_object_ids=[], project_id=self.project, idempotency_key=key,
            spec={"family": self.record.id, "params": {"prompt": "swap proof"}, "output_policy": {}},
            storage_estimate={"scratch_bytes": 1024 * 1024, "output_bytes": 4096}, **options,
        )
        settled = self.host.run(once=True)
        assert len(settled) == 1 and settled[0].state == "succeeded", settled
        completed = self.owner.get_task(task.task_id)
        assert completed.state == "succeeded"
        return {"task_id": task.task_id, "run_id": completed.run_id,
                "outputs": [row for row in completed.result["outputs"] if row["name"] == "generated_images"]}

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        argv = [sys.executable, "-m", "astrid", *args]
        result = subprocess.run(argv, cwd=ASTRID, env=self.env, capture_output=True, text=True, check=False,
                                timeout=600)
        out = result.stdout if len(result.stdout) < 4000 else result.stdout[:1500] + " ... " + result.stdout[-600:]
        self.transcript.append(f"$ astrid {shlex.join(args)}\n{out}{result.stderr[-3000:]}exit={result.returncode}")
        return result

    def seed(self) -> None:
        """A timeline with one picture clip, made through the CLI from a local file."""
        created = self.cli("timelines", "create", "film", "--project", self.project, "--json")
        assert created.returncode == 0, created.stdout + created.stderr
        seed = self.tmp / "seed.png"
        seed.write_bytes(_png((40, 200, 40, 255)))
        added = self.cli("timelines", "edit", "film", "--project", self.project, "--add-layer", "media",
                         "--layer-asset", str(seed), "--on", "0", "--for", "3", "--track", "V1",
                         "--idempotency-key", "seed-hero", "--json")
        assert added.returncode == 0 and json.loads(added.stdout)["ok"], added.stdout + added.stderr
        clips = json.loads(added.stdout)["data"]["canonical_document"]["parent_revision"]["payload"]["clips"]
        assert len(clips) == 1
        self.clip = clips[0]["id"]

    def swap_and_commit(self, handle: str, key: str) -> subprocess.CompletedProcess:
        """``timelines edit --swap-asset KEY=HANDLE``, export the printed candidate, then ``timelines commit``."""
        edited = self.cli("timelines", "edit", "film", "--project", self.project, "--clip", self.clip,
                          "--swap-asset", f"GEN={handle}")
        assert edited.returncode == 0, edited.stdout + edited.stderr
        export = next(line.split(": ", 1)[1].split(": ", 1)[1] for line in edited.stdout.splitlines()
                      if line.startswith("next: export the complete JSON candidate"))
        argv = shlex.split(export)
        assert argv[0] == "python3"
        exported = subprocess.run([sys.executable, *argv[1:]], cwd=ASTRID, env=self.env, capture_output=True,
                                  text=True, check=False, timeout=120)
        assert exported.returncode == 0, exported.stdout + exported.stderr
        return self.cli("timelines", "commit", "film", "--project", self.project, "--file", argv[-1],
                        "-m", "Use the generated image", "--idempotency-key", key, "--json")


@pytest.fixture
def world(tmp_path, real_runtime):
    built = World(tmp_path, real_runtime)
    try:
        built.seed()
        yield built
    finally:
        print("\n\n".join(built.transcript))
        built.host.shutdown()


def _committed(result: subprocess.CompletedProcess) -> dict:
    assert result.returncode == 0, result.stdout + result.stderr
    reply = json.loads(result.stdout)
    assert reply["ok"] is True, reply
    assert reply["data"]["status"] == "accepted", reply
    return reply


def _registry_entry(reply: dict, key: str, clip_id: str) -> tuple[dict, dict]:
    """The accepted head's registry entry for ``key`` and the clip that plays it."""
    parent = reply["data"]["canonical_document"]["parent_revision"]["payload"]
    assets = {**(parent.get("config", {}).get("registry", {}).get("assets") or {}),
              **(parent.get("registry", {}).get("assets") or {})}
    clips = parent.get("clips") or parent.get("config", {}).get("clips") or []
    return assets[key], next(row for row in clips if row.get("id") == clip_id)


def _variant_of(world: World, generated: dict, ordinal: int) -> dict:
    output = next(row for row in generated["outputs"] if row["ordinal"] == ordinal)
    generations, _ = world.owner.list_generations(world.project)
    for generation in generations:
        variants, _ = world.owner.list_variants(generation.generation_id)
        for variant in variants:
            if variant.object_id == output["digest"] and variant.metadata.get("ordinal") == ordinal:
                return {"generation_id": generation.generation_id, "variant_id": variant.variant_id,
                        "object_id": output["digest"]}
    raise AssertionError(f"no catalog variant for output #{ordinal}")


@pytest.mark.parametrize("kind", ["task", "run"])
def test_generated_output_swaps_into_timeline_and_commits(world, kind):
    generated = world.generate(f"swap-generate-{kind}")
    ident = generated["task_id"] if kind == "task" else generated["run_id"]
    assert ident
    result = world.swap_and_commit(f"{kind}:{ident}/generated_images#1", key=f"swap-commit-{kind}")
    entry, clip = _registry_entry(_committed(result), "GEN", world.clip)
    variant = _variant_of(world, generated, 1)
    assert clip["asset"] == "GEN"
    assert entry["media_id"] == variant["object_id"]
    assert (entry["generationId"], entry["variantId"]) == (variant["generation_id"], variant["variant_id"])


@pytest.mark.parametrize("source", ["unpublished-task-output", "raw-project-object"])
def test_media_the_runtime_did_not_verify_is_still_refused_at_commit(world, source):
    """No catalog variant, no admission: the swap edits the working copy, the commit refuses it."""
    if source == "unpublished-task-output":
        # The Runtime recorded these outputs but published no generation variant for them.
        generated = world.generate("swap-generate-unpublished", publish=False)
        handle = f"task:{generated['task_id']}/generated_images#1"
    else:
        raw = _png((10, 10, 10, 255))
        ingested = world.owner.ingest_project_object(world.project, raw, media_type="image/png",
                                                     filename="raw.png", idempotency_key="swap-raw-object")
        handle = ingested["object_id"]
    result = world.swap_and_commit(handle, key=f"swap-refused-{source}")
    assert result.returncode != 0, result.stdout
    reply = json.loads(result.stdout)
    assert reply["ok"] is False
    assert REFUSAL in json.dumps(reply), reply
