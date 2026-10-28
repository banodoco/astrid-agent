"""Target-local ordinary HC03 readiness profile for the canonical installed engine.

This is a producer of the existing ``hc03-worker-readiness.v1`` document, not a
new readiness protocol. Every fact is re-derived from an independently checked
source on the target: the verified retained release, the canonical installed
receipt and status, the installer-owned VibeComfy registry and its live
composite verifier, the supervisor's own health operation, the canonical model
lock with full byte verification, and bounded driver/disk probes.

It never installs, starts, stops, signals or adopts an engine or supervisor,
never reads the original agent bearer (``engine-bearer.token``), never adds
controller claim or workflow-endpoint fields, and never qualifies a GPU or
inference. The installed receipt stays ``qualified=false``.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ._engine_target_files import _FILE_FLAGS, _TargetFiles, _check, _parts
from .engine_contracts import EngineError

SCHEMA = "hc03-worker-readiness.v1"
ENGINE = "vibecomfy-h3"
PLATFORM = "linux-x86_64-cu130"
PROFILE_NAME = "engine-readiness.json"
# Named only so it can be refused as a target. The producer never opens it.
BEARER_NAME = "engine-bearer.token"
PUBLIC_PATH = "/usr/local/bin:/usr/bin:/bin"
NVIDIA_QUERY = ("--query-gpu=driver_version,memory.total", "--format=csv,noheader,nounits")
_PROFILE_LIMIT = 8 * 1024 * 1024
_SMALL_LIMIT = 64 * 1024
_MAX_SECONDS = 7200.0
_DRIVER = re.compile(r"[0-9]{2,4}(?:\.[0-9]{1,4}){1,3}\Z")
_MIB = re.compile(r"[1-9][0-9]{0,9}\Z")
_COMFY_VERSION = re.compile(r'^__version__\s*=\s*"([0-9]+(?:\.[0-9]+){1,3})"\s*$', re.MULTILINE)
_EXACT = frozenset({"interpreter", "runtime_lock", "engine_lock", "model_digest",
                    "custom_node_digest", "driver", "root", "port"})
_MINIMUM = frozenset({"vram_bytes", "scratch_bytes"})


class ReadinessError(ValueError):
    """Sanitized fail-closed refusal; messages are fixed product strings."""


@dataclass(frozen=True)
class ReadinessResult:
    """Locator for the original DeploymentReference readiness_profile_path/hash."""

    path: str
    sha256: str
    size: int
    profile: dict


def _profile_bytes(profile):
    # Same canonical serialization the 52 finalizer uses for its final write.
    return json.dumps(profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


def _digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")).hexdigest()


def verified_facts_digest(facts):
    """Exactly CheckoutServerAdapter.from_host_session's ``_canonical_sha256``.

    That consumer serializes ``{"exact": sorted, "minimum": sorted}`` with
    ``json.dumps(sort_keys=True, separators=(",", ":"))`` (default ASCII escaping).
    """
    value = {"exact": dict(sorted(facts["exact"].items())),
             "minimum": dict(sorted(facts["minimum"].items()))}
    return "sha256:" + hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _normalized_facts(exact, minimum):
    """Mirror GenericHost._normalize_verified_facts; refuse rather than coerce."""
    if set(exact) != _EXACT or set(minimum) != _MINIMUM:
        raise ReadinessError("readiness facts are incomplete")
    for key, value in exact.items():
        if key == "port":
            if type(value) is not int or not 0 < value <= 65535:
                raise ReadinessError("readiness port fact is invalid")
        elif not isinstance(value, str) or not value:
            raise ReadinessError("readiness exact fact is invalid")
    for value in minimum.values():
        if type(value) is not int or not 0 <= value <= (1 << 53) - 1:
            raise ReadinessError("readiness minimum fact is invalid")
    return {"exact": dict(sorted(exact.items())), "minimum": dict(sorted(minimum.items()))}


def _strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError("duplicate field")
            value[key] = item
        return value
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ReadinessError("installed evidence is not strict JSON") from None
    if not isinstance(value, dict):
        raise ReadinessError("installed evidence is not a JSON object")
    return value


def _seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= _MAX_SECONDS:
        raise ReadinessError("readiness deadline must be within 7200 seconds")
    return float(value)


# --- private profile location ------------------------------------------------

def _profile_location(profile_path):
    if not isinstance(profile_path, str):
        raise ReadinessError("readiness profile path is invalid")
    try:
        parts = _parts(profile_path)
    except EngineError:
        raise ReadinessError("readiness profile path must be absolute and normalized") from None
    if parts[-1] != PROFILE_NAME or len(parts) < 2:
        raise ReadinessError("readiness profile must be the private engine-readiness.json")
    return "/" + "/".join(parts[:-1]), parts[:-1]


def _private_directory(fd):
    info = os.fstat(fd)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ReadinessError("readiness profile directory must be the owner's private 0700 state")


def _read_private(fd, name, deadline):
    descriptor = os.open(name, _FILE_FLAGS, dir_fd=fd)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size > _PROFILE_LIMIT):
            raise ReadinessError("readiness profile must be a private 0600 regular file")
        data = bytearray()
        while True:
            _check(deadline)
            block = os.read(descriptor, min(65536, _PROFILE_LIMIT + 1 - len(data)))
            if not block:
                break
            data.extend(block)
            if len(data) > _PROFILE_LIMIT:
                raise ReadinessError("readiness profile exceeds its limit")
        after = os.fstat(descriptor)
        if len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ReadinessError("readiness profile changed during readback")
        return bytes(data)
    finally:
        os.close(descriptor)


def check_profile_location(profile_path, *, deadline):
    """Fail before any engine observation if the selected private state is unsafe."""
    parent, parts = _profile_location(profile_path)
    try:
        with _TargetFiles(parent)._directory(parts, deadline) as fd:
            _private_directory(fd)
    except FileNotFoundError:
        raise ReadinessError("selected private agent state directory is absent") from None
    except EngineError:
        raise ReadinessError("selected private agent state path is unsafe") from None


def write_profile(profile_path, data, *, deadline):
    """Exclusively publish private profile bytes, then read them back.

    The directory is the original agent's existing 0700 state. It is never
    created here. An existing profile is never rewritten: identical bytes are a
    no-op readback, anything else (including a finalized 52 profile) refuses.
    """
    parent, parts = _profile_location(profile_path)
    if not isinstance(data, bytes) or len(data) > _PROFILE_LIMIT:
        raise ReadinessError("readiness profile bytes are invalid")
    expected = "sha256:" + hashlib.sha256(data).hexdigest()
    try:
        with _TargetFiles(parent)._directory(parts, deadline) as fd:
            _private_directory(fd)
            try:
                os.stat(PROFILE_NAME, dir_fd=fd, follow_symlinks=False)
            except FileNotFoundError:
                exists = False
            else:
                exists = True
            if not exists:
                temporary = ".engine-readiness-" + secrets.token_hex(16)
                out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                              0o600, dir_fd=fd)
                try:
                    try:
                        os.fchmod(out, 0o600)
                        view = memoryview(data)
                        while view:
                            _check(deadline)
                            view = view[os.write(out, view):]
                        os.fsync(out)
                    finally:
                        os.close(out)
                    # link() never replaces an existing name: exclusive publication.
                    os.link(temporary, PROFILE_NAME, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                except FileExistsError:
                    raise ReadinessError("an existing readiness profile is retained, not rewritten") from None
                finally:
                    try:
                        os.unlink(temporary, dir_fd=fd)
                    except FileNotFoundError:
                        pass
                os.fsync(fd)
            observed = _read_private(fd, PROFILE_NAME, deadline)
    except FileNotFoundError:
        raise ReadinessError("selected private agent state directory is absent") from None
    except EngineError:
        raise ReadinessError("selected private agent state path is unsafe") from None
    except OSError:
        raise ReadinessError("readiness profile could not be written privately") from None
    if observed != data:
        raise ReadinessError("an existing readiness profile is retained, not rewritten")
    return expected


# --- independently checked sources --------------------------------------------

def _verified_release(release_root):
    """Bind runtime_lock to the verified complete release this code runs from."""
    from .engine_release import ReleaseError, interpreter_identity, verify_tree

    if not isinstance(release_root, (str, Path)) or not os.path.isabs(str(release_root)):
        raise ReadinessError("retained release root must be absolute")
    release = Path(os.path.normpath(str(release_root)))
    try:
        if Path(__file__).resolve() != (release / "site/astrid/core/generation/engine_readiness.py").resolve():
            raise ReadinessError("readiness must run from the verified retained release")
        before = (release / "release.json").read_bytes()
        manifest = verify_tree(release)
        after = (release / "release.json").read_bytes()
    except ReleaseError:
        raise ReadinessError("retained release verification failed") from None
    except OSError:
        raise ReadinessError("retained release is unreadable") from None
    if (before != after or manifest["complete"] is not True or manifest["fixture"] is not False
            or manifest["missing"] or manifest["python"] != interpreter_identity()):
        raise ReadinessError("retained release is incomplete or for another interpreter")
    return "sha256:" + hashlib.sha256(before).hexdigest()


def bind_vibecomfy_checkout(checkout):
    """Resolve ``vibecomfy`` only from the recipe-selected, verified checkout.

    The retained release runs with ``-I -S`` so neither the engine venv site nor
    an ambient path supplies VibeComfy. Append exactly this checkout after the
    product roots (the existing installed-host pattern) and refuse any other
    cached or resolved origin. VibeComfy's own dependencies must come from the
    complete release closure; the engine venv site is not mixed in.
    """
    root = Path(checkout)
    package = root / "vibecomfy"
    expected = package / "__init__.py"
    if not root.is_absolute() or root.resolve() != root or expected.resolve() != expected:
        raise ReadinessError("selected VibeComfy checkout path is not canonical")
    for name, module in tuple(sys.modules.items()):
        if name == "vibecomfy" or name.startswith("vibecomfy."):
            origin = getattr(module, "__file__", None)
            if not origin or not Path(origin).resolve().is_relative_to(package):
                raise ReadinessError("a conflicting VibeComfy origin is already imported")
    products = {name: getattr(importlib.util.find_spec(name), "origin", None)
                for name in ("astrid", "runtime_protocol")}
    spec = importlib.util.find_spec("vibecomfy")
    if spec is None or (spec.loader is None and spec.origin is None):
        if str(root) not in sys.path:
            sys.path.append(str(root))
        importlib.invalidate_caches()
        spec = importlib.util.find_spec("vibecomfy")
    if spec is None or spec.origin != str(expected):
        raise ReadinessError("VibeComfy does not resolve to the verified checkout")
    if {name: getattr(importlib.util.find_spec(name), "origin", None)
            for name in products} != products:
        raise ReadinessError("VibeComfy selection changed a product import origin")
    return root


def probe_hardware(deadline, *, executable=None):
    """Bounded driver/VRAM probe of exactly one visible NVIDIA GPU.

    No value is invented: an absent tool, failure, timeout, ambiguous output or
    other than one GPU refuses. ``executable`` is the explicit test seam.
    """
    if executable is None:
        executable = shutil.which("nvidia-smi", path=PUBLIC_PATH)
        if executable is None:
            raise ReadinessError("NVIDIA driver probe is unavailable")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ReadinessError("readiness deadline exceeded")
    try:
        result = subprocess.run(
            (str(executable), *NVIDIA_QUERY), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env={"PATH": PUBLIC_PATH, "LANG": "C", "LC_ALL": "C"},
            timeout=min(15.0, remaining), check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise ReadinessError("NVIDIA driver probe failed") from None
    if result.returncode != 0 or len(result.stdout) > 4096:
        raise ReadinessError("NVIDIA driver probe failed")
    try:
        rows = [row for row in result.stdout.decode("ascii").splitlines() if row.strip()]
    except UnicodeError:
        raise ReadinessError("NVIDIA driver probe output is invalid") from None
    if len(rows) != 1:
        raise ReadinessError("exactly one visible NVIDIA GPU is required")
    fields = [field.strip() for field in rows[0].split(",")]
    if len(fields) != 2 or not _DRIVER.fullmatch(fields[0]) or not _MIB.fullmatch(fields[1]):
        raise ReadinessError("NVIDIA driver probe output is invalid")
    return {"driver": fields[0], "vram_bytes": int(fields[1]) * 1024 * 1024}


def canonical_status(*, root, deadline):
    """The canonical ``astrid engines status`` operation (same default backend)."""
    from .engine_execution import create_local_backend
    from .engine_runner import engine_status

    context, _ = create_local_backend(engine=ENGINE, platform=PLATFORM, placement="local",
                                      root=root, deadline=deadline)
    return engine_status(ENGINE, platform=PLATFORM, placement="local", root=root,
                         context=context, deadline=deadline)


def _owned_health(root, recipe, adapter, owner_id, deadline):
    """Existing supervisor health; never creates a supervisor or starts an engine."""
    from .engine_custody import LinuxEngineServiceClient, _SCHEMA
    from .engine_recipe import recipe_digest
    from .engine_runner import _sha, helper_manifest

    client = LinuxEngineServiceClient(root=root, engine=ENGINE, platform=PLATFORM,
                                      recipe_sha256=recipe_digest(recipe),
                                      helper_sha256=_sha(helper_manifest(recipe)))
    request = adapter.start_request(recipe, root, deadline=deadline)
    launch = {"argv": list(request.argv), "cwd": request.cwd, "env": dict(request.env)}
    payload = {**client.identity, "schema": _SCHEMA, "launch": launch, "owner_id": owner_id,
               "timeout_seconds": recipe.health.timeout_seconds,
               "poll_interval_seconds": recipe.health.poll_interval_seconds}
    # start=False: an absent supervisor is a refusal, never a new owner.
    facts = client._request("health", payload, deadline, start=False)
    if (not isinstance(facts, dict) or facts.get("state") != "owned" or facts.get("healthy") is not True
            or facts.get("required_nodes_present") is not True or facts.get("owner_id") != owner_id
            or facts.get("launch_sha256") != _sha(launch)
            or any(facts.get(key) != value for key, value in client.identity.items())
            or not isinstance(facts.get("object_info_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", facts["object_info_sha256"])):
        raise ReadinessError("installed engine health or ownership is unverified")
    return facts, launch


def _comfyui_version(transport, root, comfy_root, port, health_path, deadline):
    from .engine_custody import _get_json

    raw = transport._read(root, comfy_root + "/comfyui_version.py", deadline)
    try:
        matches = _COMFY_VERSION.findall(raw.decode("utf-8"))
    except UnicodeError:
        matches = []
    if len(matches) != 1:
        raise ReadinessError("pinned ComfyUI version source is unavailable")
    try:
        stats = _get_json(port, health_path, 1024 * 1024, deadline, lambda: _check(deadline))
    except (OSError, ValueError, EngineError):
        raise ReadinessError("installed engine version observation failed") from None
    system = stats.get("system")
    if not isinstance(system, dict) or system.get("comfyui_version") != matches[0]:
        raise ReadinessError("live ComfyUI version differs from the pinned source")
    return matches[0]


def _model_root_binding(root, recipe, deadline):
    """Canonical model lock inventory, fully byte-verified with no extra files."""
    from . import engine_recipe
    from .model_root import (ModelRootBindingError, canonical_model_inventory_digest,
                             validate_model_root_binding)

    inventory = []
    for model in engine_recipe.load_model_lock(recipe.models_lock).models:
        destination = PurePosixPath(model.destination)
        subdir = str(destination.parent)
        inventory.append({"name": destination.name, "sha256": "sha256:" + model.sha256,
                          "size": model.bytes, "subdir": "" if subdir == "." else subdir})
    inventory.sort(key=lambda entry: (entry["subdir"], entry["name"]))
    binding = {"schema_version": 1, "path": root + "/models", "inventory": inventory,
               "inventory_digest": canonical_model_inventory_digest(inventory),
               "qualification_identity": None}
    _check(deadline)
    try:
        verified = validate_model_root_binding(binding, verify_files=True)
    except ModelRootBindingError:
        raise ReadinessError("installed model root does not match the canonical lock bytes") from None
    _check(deadline)
    return verified.as_dict()


def produce_readiness(*, root, release_root, deadline, status=None, hardware=None):
    """Return the ordinary HC03 base profile for the installed engine at ``root``.

    ``status`` and ``hardware`` are explicit seams; production uses the
    canonical status operation and the bounded ``nvidia-smi`` probe.
    """
    from . import engine_recipe
    from .engine_platforms import LinuxPlatformAdapter
    from .engine_runner import _parse_receipt, _root, plan_install
    from astrid.packs.runpod.installed_session import LocalInstalledVibeComfySessionTransport
    from astrid.packs.runpod.worker_session import ManagedSessionError, ManagedSessionSelection

    _check(deadline)
    try:
        root = _root(root, PLATFORM)
    except EngineError:
        raise ReadinessError("install root must be an absolute normalized path") from None
    runtime_lock = _verified_release(release_root)
    recipe = engine_recipe.load_recipe(ENGINE, PLATFORM)
    adapter = LinuxPlatformAdapter(PLATFORM)
    plan = plan_install(recipe, root=root, placement="local")
    runtime_root = root + "/runtime"
    registry = runtime_root + "/out/sessions/" + ENGINE
    receipt_path = root + "/.astrid-engine/install.json"
    transport = LocalInstalledVibeComfySessionTransport(platform=PLATFORM)
    try:
        # 1. Canonical receipt + installer registry; then the same A2 session live.
        installed = transport.read_installed(runtime_root)
        receipt_bytes = transport._read(root, receipt_path, deadline)
        marker_bytes = transport._read(root, registry + "/launch.json", deadline)
        config_bytes = transport._read(root, registry + "/config.json", deadline)
        receipt = _parse_receipt(_strict_json(receipt_bytes), plan)
        marker = _strict_json(marker_bytes)
        token = marker.get("launch_token")
        if (receipt.status != "installed" or receipt.qualified is not False or not isinstance(token, str)
                or "sha256:" + hashlib.sha256(token.encode()).hexdigest() != installed["launch_token_digest"]
                or "sha256:" + hashlib.sha256(config_bytes).hexdigest() != installed["config_digest"]):
            raise ReadinessError("installed receipt or registry evidence differs")
        try:
            runtime_instance_id = str(uuid.UUID(token))
        except ValueError:
            raise ReadinessError("installed supervisor incarnation is not canonical") from None
        bind_vibecomfy_checkout(installed["vibecomfy_root"])
        comfy_root = next(root + "/" + s.relative_path for s in recipe.sources if s.name == "comfyui")
        selection = ManagedSessionSelection(
            account_ref="target-local", pod_id="target-local", session_ref=ENGINE,
            python_executable=installed["python_executable"], runtime_root=runtime_root,
            vibecomfy_root=installed["vibecomfy_root"], comfy_root=comfy_root,
            model_root=root + "/models", output_root=installed["output_root"],
            port=recipe.start.port, config=installed["config"], config_digest=installed["config_digest"],
        )
        live = transport.status(selection)
        if live.get("ownership_verified") is not True or any(
                live.get(key) != value for key, value in installed.items()):
            raise ReadinessError("installed session changed during live verification")

        # 2. Canonical installed status re-verifies sources, locks, models, ownership, health.
        current = (status or canonical_status)(root=root, deadline=deadline)
        lock = engine_recipe.load_model_lock(recipe.models_lock)
        expected_models = tuple((m.name, root + "/models/" + m.destination, m.bytes, m.sha256)
                                for m in lock.models)
        if (current is None or current.status != "installed" or current.qualified is not False
                or current.receipt_id != receipt.receipt_id or current.root != root
                or current.recipe_sha256 != receipt.recipe_sha256
                or current.models_lock_sha256 != receipt.models_lock_sha256
                or dict(current.requirements_locks) != dict(receipt.requirements_locks)
                or dict(current.source_commits) != {s.name: s.pin.revision for s in recipe.sources}
                or tuple((m.model, m.path, m.bytes, m.sha256) for m in current.models) != expected_models
                or any(m.verified is not True for m in current.models)
                or current.health is None or current.health.status != "healthy"
                or current.health.object_info_sha256 != receipt.health.object_info_sha256):
            raise ReadinessError("canonical installed status differs from the installed receipt")

        # 3. Live owned health through the existing supervisor, bound to the receipt.
        start = next(step.facts for step in receipt.steps if step.kind == "start")
        health, launch = _owned_health(root, recipe, adapter, token, deadline)
        if (health["object_info_sha256"] != receipt.health.object_info_sha256
                or start.get("owner_id") != token or start.get("launch_sha256") != health["launch_sha256"]):
            raise ReadinessError("live engine health differs from the installed receipt")
        interpreter = installed["python_executable"]
        comfy = installed["comfy"]
        try:
            child_exe = os.readlink(f"/proc/{comfy['pid']}/exe")
        except OSError:
            raise ReadinessError("installed engine interpreter is unobservable") from None
        if launch["argv"][0] != interpreter or child_exe != os.path.realpath(interpreter):
            raise ReadinessError("installed engine interpreter differs from the primary environment")
        version = _comfyui_version(transport, root, comfy_root, recipe.start.port,
                                   recipe.health.path, deadline)

        # 4. Canonical model lock, fully byte-verified, strict existing binding.
        model_root = _model_root_binding(root, recipe, deadline)

        # 5. Bounded target hardware and disk probes.
        measured = (hardware or probe_hardware)(deadline)
        if not isinstance(measured, dict) or set(measured) != {"driver", "vram_bytes"}:
            raise ReadinessError("hardware probe result is invalid")
        output_root = installed["output_root"]
        try:
            scratch = shutil.disk_usage(output_root).free
        except OSError:
            raise ReadinessError("installed output volume is unobservable") from None

        # 6. Nothing observed may have changed while facts were collected.
        if (transport.read_installed(runtime_root) != installed
                or transport._read(root, receipt_path, deadline) != receipt_bytes
                or transport._read(root, registry + "/launch.json", deadline) != marker_bytes):
            raise ReadinessError("installed session changed while readiness was produced")
    except ManagedSessionError:
        raise ReadinessError("installed session evidence could not be verified") from None
    except EngineError:
        raise ReadinessError("installed engine evidence could not be verified") from None
    except OSError:
        raise ReadinessError("installed engine evidence is unreadable") from None
    except ImportError:
        # VibeComfy's verifier must import from the checkout plus the complete
        # release closure; a missing dependency is a refusal, not a fallback.
        raise ReadinessError("VibeComfy live verifier is unavailable in the retained release") from None

    primary = next(e for e in recipe.environments if e.name == recipe.primary_environment)
    comfy_source = next(s for s in recipe.sources if s.name == "comfyui")
    custom_nodes = {s.name: s.pin.revision for s in recipe.sources
                    if s.relative_path.startswith(comfy_source.relative_path + "/custom_nodes/")}
    node_lock = primary.node_lock
    facts = _normalized_facts({
        "interpreter": interpreter,
        "runtime_lock": runtime_lock,
        "engine_lock": _digest({"engine": ENGINE, "platform": PLATFORM,
                                "recipe_sha256": receipt.recipe_sha256,
                                "requirements_locks": dict(sorted(receipt.requirements_locks.items())),
                                "source_commits": dict(sorted(receipt.source_commits.items()))}),
        "model_digest": model_root["inventory_digest"],
        "custom_node_digest": _digest({
            "custom_node_sources": dict(sorted(custom_nodes.items())),
            "node_lock": None if node_lock is None else {
                "source": node_lock.source, "path": node_lock.path, "sha256": node_lock.sha256},
            "object_info_sha256": health["object_info_sha256"],
        }),
        "driver": measured["driver"],
        "root": root,
        "port": recipe.start.port,
    }, {"vram_bytes": measured["vram_bytes"], "scratch_bytes": scratch})
    return {
        "schema_version": SCHEMA,
        "status": "ready",
        # Readiness of the installed session only; not GPU/inference/publication qualification.
        "gpu_qualified": False,
        "verified_facts": facts,
        "verified_facts_digest": verified_facts_digest(facts),
        "runtime": {"runtime_instance_id": runtime_instance_id, "comfyui_version": "==" + version},
        "launch": {"output_root": output_root, "model_root": model_root},
        "vibecomfy_session": {
            "session_dir": registry, "server_url": installed["url"],
            "pid": installed["daemon"]["pid"], "process_birth_id": installed["daemon"]["birth_id"],
            "comfy_pid": comfy["pid"], "comfy_process_birth_id": comfy["birth_id"],
            "launch_token": token, "config_digest": installed["config_digest"],
            "source_revision": installed["source_revision"],
            "source_content_digest": installed["source_content_digest"],
        },
    }


def run_readiness(*, root, release_root, profile_path, deadline, status=None, hardware=None):
    """Produce, privately publish and read back the base profile; return its locator."""
    check_profile_location(profile_path, deadline=deadline)
    profile = produce_readiness(root=root, release_root=release_root, deadline=deadline,
                                status=status, hardware=hardware)
    data = _profile_bytes(profile)
    digest = write_profile(profile_path, data, deadline=deadline)
    return ReadinessResult(profile_path, digest, len(data), profile)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "astrid engine readiness: invalid or missing arguments\n")


def main(argv=None, *, release_root=None):
    parser = _Parser(prog="run.py readiness", description=__doc__.splitlines()[0])
    parser.add_argument("--root", required=True, help="canonical install root")
    parser.add_argument("--release", required=True, help="this verified retained release root")
    parser.add_argument("--profile", required=True, help="private <state>/engine-readiness.json")
    parser.add_argument("--seconds", type=float, default=1800.0, help="bounded deadline (<=7200)")
    args = parser.parse_args(argv)
    if release_root is None:
        parser.error("use the verified retained release launcher")
    try:
        if os.path.normpath(os.path.abspath(args.release)) != os.path.normpath(os.path.abspath(str(release_root))):
            raise ReadinessError("readiness release argument differs from the running release")
        deadline = time.monotonic() + _seconds(args.seconds)
        result = run_readiness(root=args.root, release_root=release_root,
                               profile_path=args.profile, deadline=deadline)
    except ReadinessError as exc:
        print(json.dumps({"error": {"code": "readiness_refused", "reason": str(exc)}},
                         sort_keys=True), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error": {"code": "readiness_refused", "reason": "unexpected failure"}},
                         sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps({"schema": "astrid.engine-readiness-locator.v1",
                      "readiness_profile_path": result.path,
                      "readiness_profile_hash": result.sha256,
                      "bytes": result.size, "gpu_qualified": False}, sort_keys=True))
    return 0
