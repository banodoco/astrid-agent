"""Synthetic H3 HTTP/media inputs for the real native engine custody harness.

The temporary startup hook carries the CPU recipe into separate Runtime/pack
host interpreters. It never substitutes engine_status, service responses,
process ownership, or any H3/Runtime orchestration operation.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import sysconfig


def prepare_outer_service(recipe, root: Path, started: Path):
    from astrid.core.generation.engine_contracts import DownloadResult
    from astrid.core.generation.engine_recipe import load_model_lock
    from astrid.packs.h3_av.shared.generation import generation_timing
    from tests.helpers.h3_attached_engine import AttachedEngine, server_code
    from tests.helpers.h3_cpu_runtime import _object_info_from_checkout, _synthetic_model_root_binding
    import vibecomfy

    fixture = root / "cpu-fixture"
    schema = _object_info_from_checkout(Path(vibecomfy.__file__).resolve().parents[1])
    models = load_model_lock(recipe.models_lock).models
    AttachedEngine._list_installed_models(schema, models)
    schema_path = fixture / "object_info.json"
    schema_path.write_text(json.dumps(schema, sort_keys=True))
    comfy = root / "runtime/ComfyUI"
    (comfy / "input").mkdir()
    config = fixture / "http-config.json"
    config.write_text(json.dumps({"input_directory": str(comfy / "input"),
                                  "output_directory": str(comfy / "output")}))
    expected = fixture / "http-expected.json"
    timing = generation_timing(1)
    expected.write_text(json.dumps({"generated_seconds": timing["raw_frames"] / timing["fps"]}))
    server = fixture / "http-server.py"
    server.write_text(
        "import os\nfrom pathlib import Path\n"
        f"with Path({str(started)!r}).open('a') as _f: _f.write(str(os.getpid())+'\\n')\n"
        + server_code())
    argv = (sys.executable, str(server), str(recipe.start.port), str(schema_path),
            str(config), str(expected), str(fixture / "http-evidence.json"))
    recipe = replace(recipe, start=replace(recipe.start, argv=argv))
    locked, results = [], []
    for model in models:
        data = f"synthetic CPU inventory witness: {model.name}\n".encode()
        path = root / "models" / model.destination
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        locked.append({"name": model.name, "repository": None, "revision": None,
                       "path": None, "url": "https://example.invalid/" + model.name,
                       "destination": model.destination, "bytes": len(data), "sha256": digest})
        results.append(DownloadResult(model.name, str(path), "skipped", len(data), digest, True, 0))
    binding = _synthetic_model_root_binding(
        root / "models", tuple(model.destination for model in models))
    inventory = {(Path(row["subdir"]) / row["name"]).as_posix(): row
                 for row in binding["inventory"]}
    for model in locked:
        observed = inventory[model["destination"]]
        assert model["bytes"] == observed["size"]
        assert "sha256:" + model["sha256"] == observed["sha256"]
    return recipe, {"schema": "astrid.models-lock.v1", "models": locked}, results


def observe_requests(original, operations: Path):
    def observed(self, operation, payload, deadline, *, start=True):
        if operation in {"probe", "start", "health", "stop"}:
            # One O_APPEND write also preserves complete records across clients.
            descriptor = os.open(operations, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(descriptor, (json.dumps({"pid": os.getpid(), "operation": "service:" + operation,
                    "identity": self.identity}, sort_keys=True) + "\n").encode())
            finally:
                os.close(descriptor)
        return original(self, operation, payload, deadline, start=start)
    return observed


def activate(bootstrap: str, operations: str) -> None:
    """Carry only fixture recipe inputs to the real separate-process callers."""
    path = Path(bootstrap)
    if not path.is_file():
        return
    namespace = {}
    program = path.read_text()
    exec(compile(program, str(path), "exec"), namespace)
    from astrid.core.generation import engine_custody as custody

    root = path.parent.parent
    os.environ["ASTRID_ENGINE_ROOT"] = str(root)
    supervisor = program + "\nfrom astrid.core.generation.engine_custody import _serve\n_serve(json.loads(sys.argv[1]),recipe,int(sys.argv[2]),sys.argv[4])"
    custody._supervisor_argv = lambda identity, lock_fd, token: [
        sys.executable, "-c", supervisor, json.dumps(identity), str(lock_fd), "--launch-token", token]
    custody.LinuxEngineServiceClient._request = observe_requests(
        custody.LinuxEngineServiceClient._request, Path(operations))


def install_hook(bootstrap: Path, operations: Path) -> Path:
    path = Path(sysconfig.get_paths()["purelib"]) / "astrid_native_h3_cpu.pth"
    if path.exists():
        raise RuntimeError("native H3 CPU startup hook is already installed")
    root = Path(__file__).resolve().parents[2]
    path.write_text(f"import sys; sys.path.insert(0, {str(root)!r}); "
                    f"import tests.helpers.h3_native_engine as _native_h3; "
                    f"_native_h3.activate({str(bootstrap)!r}, {str(operations)!r})\n")
    return path
