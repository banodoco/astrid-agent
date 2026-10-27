"""Local Linux target operations backed by the installation supervisor."""
from __future__ import annotations

import json
import time

from ._engine_target_files import _TargetFiles, _check
from .engine_contracts import (CommandResult, IntegrityError, RecipeValidationError,
                               UnsafePathError, UnsupportedPlacement, UnsupportedPlatform)

_SERVICE_PREFIX = ("python3", "-I", "-m", "astrid.core.generation.engine_execution", "--engine-service")
_SERVICE_SCHEMA = "astrid.engine-service.v1"


class LocalLinuxExecutionContext:
    placement = "local"

    def __init__(self, *, engine, platform, root, adapter, service_client):
        if adapter.key != platform or not platform.startswith("linux-"):
            raise UnsupportedPlatform("context and adapter platform must match Linux")
        if adapter.validate_root(root) != root:
            raise UnsafePathError("adapter changed the install root")
        self.engine, self.platform, self.root = engine, platform, root
        self.adapter, self.service_client = adapter, service_client
        self._files = _TargetFiles(root)

    def run(self, request):
        _check(request.deadline)
        argv = request.argv
        if tuple(argv[:5]) != _SERVICE_PREFIX:
            return self.service_client.run(request)
        if len(argv) != 7 or argv[5] not in {"probe", "start", "health"} or request.cwd != self.root:
            raise RecipeValidationError("invalid service helper request")
        try:
            payload = json.loads(argv[6])
        except (ValueError, TypeError):
            raise RecipeValidationError("invalid service helper payload") from None
        from .engine_recipe import load_recipe, recipe_digest
        from .engine_runner import _sha, helper_manifest
        recipe = load_recipe(self.engine, self.platform)
        expected = {"schema": _SERVICE_SCHEMA, "engine": self.engine, "platform": self.platform,
                    "root": self.root, "recipe_sha256": recipe_digest(recipe),
                    "helper_sha256": _sha(helper_manifest(recipe))}
        keys = set(expected) | {"launch", "owner_id", "timeout_seconds", "poll_interval_seconds"}
        if not isinstance(payload, dict) or set(payload) != keys or any(payload[k] != v for k, v in expected.items()):
            raise IntegrityError("service helper identity differs")
        started = time.monotonic()
        facts = self.service_client.call(argv[5], payload, deadline=request.deadline)
        result = json.dumps({"schema": _SERVICE_SCHEMA, "operation": argv[5], "facts": facts},
                            sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if len(result.encode("utf-8")) > request.max_output_bytes:
            raise IntegrityError("service response exceeds output limit")
        return CommandResult(0, result, "", False, time.monotonic() - started, None, False, False)

    def ensure_dir(self, path, *, deadline):
        return self._files.ensure_dir(path, deadline=deadline)

    def read_json(self, path, *, deadline):
        return self._files.read_json(path, deadline=deadline)

    def write_json_atomic(self, path, value, *, deadline):
        return self._files.write_json_atomic(path, value, deadline=deadline)

    def file_fact(self, path, *, hash_bytes, deadline):
        return self._files.file_fact(path, hash_bytes=hash_bytes, deadline=deadline)

    def download_model(self, model, *, models_root, deadline):
        from .model_downloader import download_model
        self._files._relative(models_root)
        _check(deadline)
        return download_model(model, models_root=models_root, deadline=deadline)

    def acquire_install_lock(self, root, *, deadline):
        if root != self.root:
            raise UnsafePathError("install lock root differs")
        return self.adapter.local_install_lock(root, deadline=deadline)


def create_local_backend(*, engine, platform, placement, root, deadline):
    if placement != "local":
        raise UnsupportedPlacement("RunPod execution awaits P2-SELF orchestration")
    _check(deadline)
    from .engine_platforms import platform_adapter
    from .engine_recipe import load_recipe, recipe_digest
    from .engine_runner import _sha, helper_manifest
    adapter = platform_adapter(platform)
    adapter.validate_root(root)
    recipe = load_recipe(engine, platform)
    try:
        from .engine_custody import LinuxEngineServiceClient
    except ModuleNotFoundError as exc:
        if exc.name != __package__ + ".engine_custody":
            raise
        raise UnsupportedPlatform("local engine supervisor is not installed") from exc
    client = LinuxEngineServiceClient(root=root, engine=engine, platform=platform,
                                      recipe_sha256=recipe_digest(recipe),
                                      helper_sha256=_sha(helper_manifest(recipe)))
    return LocalLinuxExecutionContext(engine=engine, platform=platform, root=root,
                                      adapter=adapter, service_client=client), adapter
