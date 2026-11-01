"""Local target persistence, migration and selection; no engine installation."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

import pytest
from filelock import FileLock

from astrid.core.generation import targets
from astrid.core.generation.preflight import select_generation_target


def _write(text):
    path = targets.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _human(**kwargs):
    return targets.TargetRecord(endpoint="http://localhost:8199/", **kwargs)


def test_explicit_save_roundtrips_human_and_preserves_nested_toml(tmp_path):
    path = _write('''title = "library config"
when = 2026-10-31T12:00:00Z
[library]
models = "/models"
[library.extra]
"key.with.dots" = [1, { nested = { enabled = true } }]
[[unrelated.rows]]
name = "preserved"
''')
    previous = targets.read_config()
    root, python = str(tmp_path / "ComfyUI"), str(tmp_path / "venv/bin/python")
    targets.save_target(_human(root=root, python=python, layout="git",
        verified_at="2026-10-31T12:00:00Z", facts={
            "versions": {"comfyui": "0.3.64"}, "user_dir": str(tmp_path / "user"),
            "launch_argv": [python, "main.py", "--name=hello\nworld"], "unknown": None,
        }), primary=True)
    document = targets.read_config()
    assert {k: document[k] for k in previous} == previous
    assert document["engines"] == {"schema_version": 1, "primary": "comfyui"}
    assert document["comfyui"]["url"] == "http://127.0.0.1:8199"
    assert "endpoint" not in document["comfyui"]
    assert "unknown" not in document["comfyui"]["facts"]
    assert document["comfyui"]["comfyui_version"] == "0.3.64"
    assert document["comfyui"]["user_dir"] == str(tmp_path / "user")
    record = targets.read_target()
    assert (record.engine, record.location, record.owner) == ("comfyui", "local", "human")
    assert (record.root, record.python) == (root, python)
    assert record.facts["launch_argv"][2] == "--name=hello\nworld"
    assert path.stat().st_mode & 0o777 == 0o600


def test_saved_python_keeps_the_selected_virtual_environment(tmp_path):
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    targets.save_target(_human(python=str(python)))
    assert targets.read_target().python == str(python)


def test_unknown_versions_are_omitted_and_legacy_observations_can_fill_them():
    targets.save_target(_human(facts={"versions": None}))
    assert "versions" not in targets.read_config()["comfyui"]["facts"]
    observed = targets.normalize_target({
        "url": "http://localhost:8199", "facts": {"versions": None},
        "comfyui_version": "0.3.64",
    })
    targets.save_target(observed)
    assert targets.read_target().facts["versions"] == {"comfyui": "0.3.64"}


def test_saved_target_preserves_standalone_vibe_library_resolution(tmp_path):
    from vibecomfy.local_library import Slot, SlotState, resolve

    models = tmp_path / "models"
    _write(f'[library]\nmodels = "{models}"\ncustom_nodes = "off"\n')
    targets.save_target(_human())
    path = targets.config_path()
    original, modified = path.read_bytes(), path.stat().st_mtime_ns
    resolved = resolve(Slot.models)
    assert (resolved.state, resolved.path, resolved.source) == (SlotState.SET, models, "global")
    assert resolve(Slot.custom_nodes).state == SlotState.DISABLED
    assert path.read_bytes() == original and path.stat().st_mtime_ns == modified


@pytest.mark.parametrize("alias", ["comfyui", "local", "vibecomfy", "vibecomfy-h3"])
def test_legacy_migration_is_lazy_and_saves_canonical_wire(alias, tmp_path):
    path = _write(f'''[engines]
schema_version = 1
primary = "{alias}"
[{alias}]
url = "http://localhost:8188/"
kind = "portable"
root = "{tmp_path}"
python = "{sys.executable}"
output_dir = "{tmp_path}/output"
comfyui_version = "0.3.64"
launch_argv = ["python", "main.py"]
prefer = "attached"
''')
    original = path.read_bytes()
    record = targets.read_target(engine=alias)
    assert record.engine == "comfyui" and record.layout == "portable"
    assert record.facts["versions"] == {"comfyui": "0.3.64"}
    assert record.facts["output_dir"] == str(tmp_path / "output")
    assert record.prefer == "attached"
    assert path.read_bytes() == original and not targets.config_lock_path().exists()
    targets.save_target(record)
    wire = targets.read_config()
    assert wire["engines"]["primary"] == "comfyui"
    assert wire["comfyui"]["layout"] == "portable"
    assert wire["comfyui"]["url"] == "http://127.0.0.1:8188"
    assert "kind" not in wire["comfyui"]
    assert not (set(wire) & (set(targets._COMFY_TABLES) - {"comfyui"}))


@pytest.mark.parametrize("text", [
    "[broken", '[engines]\nschema_version = 2', '[engines]\nschema_version = true',
    '[engines]\nschema_version = 1\nprimary = ""',
    'library = "bad"', 'comfyui = "bad"', '[comfyui]\nowner = "guess"',
    '[comfyui]\nowner = "guess"\nprefer = "managed"',
    '[comfyui]\nowner = []\nprefer = "managed"',
    '[comfyui]\nkind = "pod"', '[comfyui]\nlocation = "runpod"',
    '[comfyui]\nurl = "http://remote.example:8188"',
    '[comfyui]\nverified_at = "yesterday"',
    '[comfyui]\nverified_at = "2026-10-31 12:00:00+00:00"',
    '[comfyui]\nengine = []', '[comfyui]\nowner = []',
    '[comfyui]\nlayout = []', '[comfyui]\nkind = []',
    '[comfyui]\nfacts = { versions = [] }',
    '[comfyui]\nurl = "http://localhost:8188"\n[local]\nkind = "attached"',
])
def test_corrupt_or_unsupported_config_is_never_overwritten(text):
    path = _write(text)
    original = path.read_bytes()
    with pytest.raises(targets.TargetConfigError):
        targets.read_target()
    with pytest.raises(targets.TargetConfigError):
        targets.save_target(_human())
    with pytest.raises(targets.TargetConfigError):
        targets.update_config(lambda doc: doc.update(extra={"ok": True}))
    assert path.read_bytes() == original


@pytest.mark.parametrize("facts", [{"bad": object()}, {"bad": float("nan")}, {1: "bad"},
                                   {"bad": [None]}])
def test_unserializable_save_leaves_config_unchanged(facts):
    path = _write('[library]\nmodels = "/models"\n')
    original = path.read_bytes()
    with pytest.raises(targets.TargetConfigError):
        targets.save_target(_human(facts=facts))
    assert path.read_bytes() == original


def test_save_replaces_selection_and_retains_library():
    path = _write('[library]\nmodels = "/models"\n[comfyui]\nprefer = "attached"\nurl = "http://localhost:8188"\nroot = "/old"\n')
    targets.save_target(_human())
    assert targets.read_target().root is None
    assert targets.read_config()["comfyui"]["prefer"] == "attached"
    assert targets.read_config()["library"] == {"models": "/models"}
    assert path.exists()


def test_generic_writer_preserves_selection_and_rereads_after_sibling_lock(tmp_path):
    """A standalone library writer and Astrid use the same lock protocol."""
    targets.save_target(_human(), primary=True)
    path = targets.config_path()
    code = '''
import sys
from astrid.core.generation.targets import update_config
print("ready", flush=True)
update_config(lambda doc: doc.setdefault("library", {}).update(models="/new-models"), path=sys.argv[1])
'''
    child = None
    try:
        with FileLock(str(path) + ".lock"):
            child = subprocess.Popen([sys.executable, "-c", code, str(path)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert child.stdout.readline().strip() == "ready"
            # Commit new state while the child waits. Its update must reread it.
            document = targets.read_config()
            document["extra"] = {"nested": {"kept": [1, 2, 3]}}
            from astrid.core.foundation.atomic_io import write_text_atomic
            write_text_atomic(path, targets._format_toml(document))
            assert child.poll() is None
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, (stdout, stderr)
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
    document = targets.read_config()
    assert document["extra"] == {"nested": {"kept": [1, 2, 3]}}
    assert document["library"] == {"models": "/new-models"}
    assert document["comfyui"]["url"] == "http://127.0.0.1:8199"
    assert document["engines"]["primary"] == "comfyui"


def test_concurrent_library_target_and_future_section_updates_do_not_lose_data():
    path = _write('[library]\nmodels = "/models"\n[unrelated]\nnested = { rows = [{ name = "kept" }] }\n')
    barrier = threading.Barrier(3)
    def target_writer():
        barrier.wait(timeout=5)
        for _ in range(6):
            targets.save_target(_human())
    def section_writer(section):
        barrier.wait(timeout=5)
        for i in range(6):
            targets.update_config(lambda doc: doc.setdefault(section, {}).update({str(i): {"ok": True}}))
    with ThreadPoolExecutor(max_workers=3) as workers:
        futures = [workers.submit(target_writer), workers.submit(section_writer, "library"),
                   workers.submit(section_writer, "future_preferences")]
        for future in futures:
            future.result(timeout=15)
    doc = tomllib.loads(path.read_text())
    assert doc["unrelated"]["nested"]["rows"] == [{"name": "kept"}]
    assert doc["comfyui"]["url"] == "http://127.0.0.1:8199"
    assert doc["library"]["models"] == "/models"
    for i in range(6):
        assert doc["library"][str(i)] == doc["future_preferences"][str(i)] == {"ok": True}


def test_failed_replace_preserves_original_and_cleans_private_temporary(monkeypatch):
    from astrid.core.foundation import atomic_io
    path = _write('[library]\nmodels = "/models"\n')
    original = path.read_bytes()
    def fail(temporary, destination):
        assert Path(temporary).parent == path.parent and Path(destination) == path
        assert Path(temporary).stat().st_mode & 0o777 == 0o600
        tomllib.loads(Path(temporary).read_text())
        raise OSError("simulated interrupted replace")
    monkeypatch.setattr(atomic_io.platform, "durable_replace", fail)
    with pytest.raises(OSError, match="interrupted"):
        targets.save_target(_human())
    assert path.read_bytes() == original
    assert not list(path.parent.glob("*.tmp"))


def test_failed_extension_callback_preserves_original():
    path = _write('[library]\nmodels = "/models"\n')
    original = path.read_bytes()

    def fail(document):
        document["future_preferences"] = {"enabled": True}
        raise RuntimeError("extension failed")

    with pytest.raises(RuntimeError, match="extension failed"):
        targets.update_config(fail)
    assert path.read_bytes() == original


@pytest.mark.parametrize("patch", [
    {1: {"enabled": True}},
    {"future_preferences": {1: True}},
    {"future_preferences": {"nested": {1: True}}},
])
def test_extension_keys_are_not_silently_coerced_into_toml(patch):
    path = _write('[library]\nmodels = "/models"\n')
    original = path.read_bytes()
    with pytest.raises(targets.TargetConfigError):
        targets.update_config(lambda document: document.update(patch))
    assert path.read_bytes() == original


def test_selection_uses_one_snapshot_during_concurrent_replacement(monkeypatch):
    from astrid.core.foundation.atomic_io import write_text_atomic

    path = _write('[engines]\nschema_version = 1\nprimary = "comfyui"\n'
                  '[comfyui]\nurl = "http://localhost:8199"\n')
    read = targets.read_config

    def read_then_replace(candidate):
        document = read(candidate)
        write_text_atomic(path, '[engines]\nschema_version = 1\nprimary = "cloud"\n')
        return document

    monkeypatch.setattr(targets, "read_config", read_then_replace)
    selection = targets.resolve_target(environ={})
    assert selection.source == "global" and selection.bound
    assert selection.record.endpoint == "http://127.0.0.1:8199"
    assert read(path)["engines"]["primary"] == "cloud"


def _managed_metadata(tmp_path, *, status="installed"):
    from astrid.core.generation.engine_contracts import DownloadResult, HealthEvidence, InstallReceipt, StepReceipt
    from astrid.core.generation.engine_local_endpoint import record_engine_root
    from astrid.core.generation.engine_recipe import load_model_lock, load_recipe
    from astrid.core.generation.engine_runner import plan_install
    root = tmp_path / "managed"
    recipe = load_recipe("vibecomfy-h3", "linux-x86_64-cu130")
    plan = plan_install(recipe, root=str(root), placement="local")
    models = tuple(DownloadResult(m.name, str(root / "models" / m.destination),
        "skipped", m.bytes, m.sha256, True, 0) for m in load_model_lock(recipe.models_lock).models)
    steps = tuple(StepReceipt(s.id, s.kind, "executed", "passed", {}) for s in plan.steps)
    health = HealthEvidence("healthy", "2026-10-31T12:00:01Z", "a" * 64, True)
    receipt = InstallReceipt("astrid.engine-install.v1", "owned-install", plan.engine,
        plan.platform, plan.placement, plan.root, status, False, plan.recipe_sha256,
        plan.models_lock_sha256, {e.name: "b" * 64 for e in recipe.environments},
        {s.name: s.pin.revision for s in recipe.sources}, models, steps, health,
        "2026-10-31T12:00:00Z", "2026-10-31T12:00:01Z", None)
    record_engine_root("vibecomfy-h3", str(root))
    receipt_path = root / ".astrid-engine/install.json"
    receipt_path.parent.mkdir(parents=True)
    receipt_path.write_text(json.dumps(asdict(receipt)))
    return root, receipt_path


def test_managed_paths_come_only_from_receipt_and_never_persist(tmp_path):
    root, receipt = _managed_metadata(tmp_path)
    path = _write('''[comfyui]
kind = "managed"
root = "/legacy-ignored"
python = "/legacy-python"
output_dir = "/legacy-output"
facts = { nested = { root = "/nested-managed-root" } }
prefer = "managed"
''')
    original = path.read_bytes()
    record = targets.read_target()
    assert record.owner == "astrid"
    assert record.root == str(root)
    assert record.python == str(root / "runtime/venv/bin/python")
    assert path.read_bytes() == original
    targets.save_target(record)
    assert targets.read_config()["comfyui"] == {
        "location": "local", "owner": "astrid", "layout": "unknown", "prefer": "managed"}
    assert str(root) not in path.read_text() and "legacy" not in path.read_text()
    assert "nested-managed-root" not in path.read_text()
    assert targets.read_target().root == str(root)
    assert receipt.exists()


@pytest.mark.parametrize("ownership", ['kind = "managed"', 'owner = "astrid"'])
def test_attached_preference_never_silently_selects_managed(ownership, tmp_path):
    from astrid.core.contracts.errors import AstridError

    _managed_metadata(tmp_path)
    path = _write(f'[comfyui]\n{ownership}\nprefer = "attached"\n'
                  'url = "http://localhost:8199"\n')
    original = path.read_bytes(), path.stat().st_mtime_ns
    with pytest.raises(AstridError) as error:
        select_generation_target("auto")
    assert error.value.code == "input_rejected"
    with pytest.raises(targets.TargetConfigError, match="preference conflicts"):
        targets.save_target(_human())
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original


def test_managed_preference_uses_receipt_authority_instead_of_human_paths(tmp_path):
    root, _ = _managed_metadata(tmp_path)
    path = _write('[comfyui]\nkind = "attached"\nprefer = "managed"\n'
                  'url = "http://localhost:8199"\nroot = "/old-human-root"\n'
                  'python = "/old-human-python"\n')
    original = path.read_bytes()
    record = targets.read_target()
    assert record.owner == "astrid" and record.root == str(root)
    assert record.python == str(root / "runtime/venv/bin/python")
    assert path.read_bytes() == original
    targets.save_target(record)
    assert targets.read_config()["comfyui"] == {
        "location": "local", "owner": "astrid", "layout": "unknown", "prefer": "managed"}


def test_managed_ownership_requires_metadata_and_rejects_wrong_receipt(tmp_path):
    with pytest.raises(targets.TargetConfigError, match="recorded engine root"):
        targets.save_target(targets.TargetRecord(owner="astrid"))
    root, receipt = _managed_metadata(tmp_path)
    raw = json.loads(receipt.read_text())
    raw["root"] = "/wrong-root"
    receipt.write_text(json.dumps(raw))
    with pytest.raises(targets.TargetConfigError, match="metadata"):
        targets.save_target(targets.TargetRecord(owner="astrid"))
    assert not targets.config_path().exists()


@pytest.mark.parametrize("invalid", ["array", "symlink", "incomplete"])
def test_invalid_managed_receipt_never_grants_ownership(invalid, tmp_path):
    root, receipt = _managed_metadata(tmp_path)
    if invalid == "array":
        receipt.write_text("[]")
    elif invalid == "symlink":
        copied = root / "receipt-copy.json"
        receipt.rename(copied)
        receipt.symlink_to(copied)
    else:
        raw = json.loads(receipt.read_text())
        raw["status"] = "failed"
        receipt.write_text(json.dumps(raw))
    with pytest.raises(targets.TargetConfigError):
        targets.save_target(targets.TargetRecord(owner="astrid"))
    assert not targets.config_path().exists()


def test_selection_precedence_and_transient_override_invalidate_cached_identity(tmp_path):
    targets.save_target(_human(root=str(tmp_path), python=sys.executable,
        facts={"launch_argv": ["python", "main.py"]}))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "vibecomfy.toml").write_text('[comfyui]\nurl = "http://localhost:8200"\n')
    path = targets.config_path()
    original = path.read_bytes()
    env = {"VIBECOMFY_COMFYUI_URL": "http://localhost:8201"}
    assert targets.resolve_target("http://localhost:8202", repo=repo, environ=env).record.endpoint.endswith(":8202")
    assert targets.resolve_target("managed", repo=repo, environ=env).record is None
    override = targets.resolve_target(repo=repo, environ=env)
    assert override.source == "env" and override.bound
    assert override.record.endpoint.endswith(":8201")
    assert (override.record.root, override.record.python, override.record.owner, override.record.facts) == (None, None, "human", {})
    assert targets.resolve_target(repo=repo, environ={}).record.endpoint.endswith(":8200")
    assert targets.resolve_target(environ={}).record.endpoint.endswith(":8199")
    assert path.read_bytes() == original


def test_discovery_read_does_not_create_any_config_or_lock():
    assert targets.read_target() is None
    selection = targets.resolve_target(environ={})
    assert not selection.bound and selection.source == "discovery"
    assert not targets.config_path().parent.exists()


@pytest.mark.parametrize("saved", [False, True])
def test_successful_probe_is_read_only(saved, monkeypatch):
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient

    path = targets.config_path()
    original = None
    if saved:
        targets.save_target(_human(facts={"versions": {"comfyui": "cached"}}))
        original = path.read_bytes(), path.stat().st_mtime_ns
        targets.config_lock_path().unlink()
    monkeypatch.setattr(ComfyTargetClient, "request_json",
                        lambda *a, **k: {"system": {"comfyui_version": "observed"}})
    client = select_generation_target("auto")
    assert client.target.kind == "attached"
    assert client.target.facts["system"]["comfyui_version"] == "observed"
    if saved:
        assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    else:
        assert not path.parent.exists()
    assert not targets.config_lock_path().exists()


@pytest.mark.parametrize("source", ["global", "repo", "env", "request"])
def test_bound_unreachable_human_never_silently_switches(source, tmp_path, monkeypatch):
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient
    targets.save_target(_human())
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "vibecomfy.toml").write_text('[comfyui]\nurl = "http://localhost:8200"\n')
    if source == "env":
        monkeypatch.setenv("VIBECOMFY_COMFYUI_URL", "http://localhost:8201")
    def offline(*args, **kwargs):
        raise OSError("offline fixture")
    monkeypatch.setattr(ComfyTargetClient, "request_json", offline)
    path = targets.config_path()
    original = path.read_bytes()
    with pytest.raises(AstridError) as error:
        select_generation_target("http://localhost:8202" if source == "request" else "auto",
                                 repo=repo if source == "repo" else None)
    assert error.value.code == "target_unreachable"
    assert path.read_bytes() == original


def test_unbound_offline_discovery_may_use_existing_managed_lifecycle(monkeypatch):
    from astrid.core.generation.backends.comfy_target import ComfyTargetClient
    def offline(*args, **kwargs):
        raise OSError("offline fixture")
    monkeypatch.setattr(ComfyTargetClient, "request_json", offline)
    assert select_generation_target("auto") is None
    assert not targets.config_path().parent.exists()


def test_human_paths_without_endpoint_request_launch_and_never_managed(tmp_path):
    from astrid.core.contracts.errors import AstridError
    targets.save_target(targets.TargetRecord(root=str(tmp_path), python=sys.executable))
    with pytest.raises(AstridError) as error:
        select_generation_target("auto")
    assert error.value.code == "target_unreachable"
    assert "launch" in str(error.value).lower()


def test_unsupported_bound_primary_does_not_switch():
    from astrid.core.contracts.errors import AstridError
    _write('[engines]\nschema_version = 1\nprimary = "cloud"\n')
    with pytest.raises(AstridError) as error:
        select_generation_target("auto")
    assert error.value.code == "input_rejected"
    assert targets.canonical_engine("cloud") == "fal"
