from __future__ import annotations

import base64
import copy
import hashlib
import importlib.metadata as metadata
import json
import posixpath
import re
import shutil
import subprocess
import sys
import time
import types
import venv
from contextlib import contextmanager
from dataclasses import asdict, fields, replace
from pathlib import Path

import pytest

from astrid.core.generation import engine_recipe as recipes
from astrid.core.generation import engine_runner as runner
from astrid.core.generation import engine_runner_target as target
from astrid.core.generation.engine_contracts import (
    CommandFailed,
    CommandRequest,
    CommandResult,
    CustodyUnresolved,
    DeadlineExceeded,
    DownloadResult,
    EndpointHealthError,
    EndpointOwnershipError,
    FileFact,
    IntegrityError,
    RecipeValidationError,
    UnsafePathError,
    UnsupportedPlacement,
    UnsupportedPlatform,
)

PLATFORM = "linux-x86_64-cu130"
ROOT = "/TARGET_ONLY/engine"


class FakeAdapter:
    key = PLATFORM

    def join(self, root, *relative):
        assert all(not p.startswith("/") and ".." not in p.split("/") for p in relative)
        return posixpath.join(root, *relative)

    def validate_root(self, root):
        return runner._root(root, self.key)

    def venv_python(self, root):
        return self.join(root, "bin/python")

    def uv_executable(self, runtime):
        return self.join(runtime, "bootstrap/bin/uv")

    def environment(self, recipe, root):
        return {"PYTHONDONTWRITEBYTECODE": "1", "UV_CACHE_DIR": self.join(root, "runtime/uv-cache")}

    def environment_commands(self, recipe, environment, root):
        return (("fake-bootstrap", environment.name),)

    def start_request(self, recipe, root, *, deadline, models_root=None):
        primary = next(e for e in recipe.environments if e.name == recipe.primary_environment)
        values = {"install_root": root, "python": self.venv_python(self.join(root, primary.relative_path)),
                  "comfy_root": self.join(root, "runtime/ComfyUI"), "models_root": self.join(root, "models"),
                  "models_config": self.join(root, "runtime/extra_model_paths.yaml"),
                  "host": recipe.start.bind_host, "port": str(recipe.start.port)}
        return CommandRequest(tuple(a.format_map(values) for a in recipe.start.argv),
                              recipe.start.cwd.format_map(values),
                              {k: v.format_map(values) for k, v in recipe.start.env.items()}, deadline)

    def local_install_lock(self, root, *, deadline):
        pytest.fail("runner must hold the context's target lock")


class FakeContext:
    platform = PLATFORM
    placement = "local"

    def __init__(self):
        self.recipe = recipes.load_recipe("comfyui", PLATFORM)
        self.adapter = FakeAdapter()
        self.root = ROOT
        self.events = []
        self.files = {}
        self.directories = set()
        self.sources = {}
        self.environments = {}
        self.server = None
        self.held = False
        self.deadline = None
        self.fail = None
        self.wrong_identity = False
        self.bad_model = False
        self.health_objects = {node: {} for node in self.recipe.health.required_nodes}
        self.health_document = {"system": {"os": "linux"}}
        self.health_owner = None
        self.health_healthy = True
        self.bootstrap_count = 0
        self.models_config_valid = False
        self.models_config_root = object()
        for environment in self.recipe.environments:
            ref = environment.node_lock
            source = next(s for s in self.recipe.sources if s.name == ref.source)
            self.files[self.adapter.join(ROOT, source.relative_path, ref.path)] = FileFact(True, True, False, 200, ref.sha256)

    def record(self, operation, path=None, *, deadline, mutation=False):
        assert deadline <= self.deadline and deadline > time.monotonic()
        if path is not None:
            assert path == self.root or path.startswith(self.root + "/"), path
        if mutation:
            assert self.held, operation
        self.events.append((operation, path, deadline))
        if self.fail and self.fail[0] == operation:
            raise self.fail[1]

    @contextmanager
    def acquire_install_lock(self, root, *, deadline):
        assert not self.held
        self.record("acquire", root, deadline=deadline)
        self.held = True
        try:
            yield
        finally:
            self.events.append(("release", root, deadline))
            self.held = False

    def file_fact(self, path, *, hash_bytes, deadline):
        self.record("hash" if hash_bytes else "fact", path, deadline=deadline)
        if path in self.files:
            value = self.files[path]
            if isinstance(value, FileFact):
                return value
            raw = runner._json(value).encode()
            return FileFact(True, True, False, len(raw), hashlib.sha256(raw).hexdigest() if hash_bytes else None)
        if path in self.directories or path in self.sources or path.removesuffix("/.git") in self.sources and path.endswith("/.git"):
            return FileFact(True, False, False, None, None)
        return FileFact(False, False, False, None, None)

    def ensure_dir(self, path, *, deadline):
        self.record("mkdir", path, deadline=deadline, mutation=True)
        self.directories.add(path)

    def read_json(self, path, *, deadline):
        self.record("read", path, deadline=deadline)
        return copy.deepcopy(self.files[path])

    def write_json_atomic(self, path, value, *, deadline):
        self.record("write", path, deadline=deadline, mutation=True)
        self.files[path] = json.loads(json.dumps(value))

    def download_model(self, model, *, models_root, deadline):
        self.record("download", models_root, deadline=deadline, mutation=True)
        path = self.adapter.join(models_root, model.destination)
        fact = self.file_fact(path, hash_bytes=True, deadline=deadline)
        action = "skipped" if fact.exists else "downloaded"
        if fact.exists and (fact.bytes != model.bytes or fact.sha256 != model.sha256):
            raise IntegrityError("corrupt model")
        self.files[path] = FileFact(True, True, False, model.bytes, model.sha256)
        return DownloadResult(model.name, path, action, model.bytes,
                              "0" * 64 if self.bad_model else model.sha256, True, 0)

    def result(self, stdout="", code=0):
        return CommandResult(code, stdout, "", False, 0.001, "job-1", False, False)

    def run(self, request):
        self.record("run", request.cwd, deadline=request.deadline)
        assert request.max_output_bytes == 262144
        argv = request.argv
        self.events.append(("argv", argv, request.deadline))
        if self.fail and self.fail[0] == argv[0]:
            error = self.fail[1]
            if isinstance(error, CommandResult):
                return error
            raise error
        if argv[0] == "fake-bootstrap":
            assert self.held
            self.bootstrap_count += 1
            return self.result()
        if argv[0] == "git":
            if argv[1] == "init":
                assert self.held
                self.sources[argv[2]] = {"origin": None, "head": None, "dirty": ""}
                return self.result()
            assert argv[1] == "-C"
            path, args = argv[2], argv[3:]
            source = self.sources[path]
            if args == ("remote", "get-url", "origin"):
                return self.result(source["origin"] or "", 0 if source["origin"] else 2)
            if args[:2] == ("remote", "add"):
                assert self.held
                source["origin"] = args[3]
            elif args[0] == "status":
                return self.result(source["dirty"])
            elif args[0] == "rev-parse":
                return self.result(source["head"] or "", 0 if source["head"] else 128)
            elif args[0] == "fetch":
                assert self.held
            elif args[0] == "checkout":
                assert self.held
                source["head"] = args[-1]
            else:
                pytest.fail(str(args))
            return self.result()
        if "engine_runner_target" in " ".join(argv):
            operation, payload = argv[-2], json.loads(argv[-1])
            assert payload["root"] == self.root
            if operation == "identity":
                facts = {"helper_sha256": "0" * 64 if self.wrong_identity else payload["helper_sha256"]}
            elif operation in {"apply-source-patches", "verify-source-patches"}:
                facts = {"changed": False}
            elif operation == "models-source-check":
                facts = {"valid": False}
            elif operation == "node-lock":
                facts = {"valid": True}
            elif operation == "empty-source":
                facts = {"empty": True}
            elif operation == "models-config":
                assert self.held
                self.models_config_valid = True
                self.models_config_root = payload.get("models_root")
                facts = {"written": True}
            elif operation == "models-config-check":
                facts = {"valid": self.models_config_valid and payload.get("models_root") == self.models_config_root}
            else:
                name = payload["environment"]
                directory = self.adapter.join(self.root, ".astrid-engine/environments", name)
                lock_path = directory + "/requirements.lock"
                if operation == "lock-inputs":
                    assert self.held
                    facts = {"lock_exists": lock_path in self.files}
                elif operation == "commit-lock":
                    assert self.held
                    self.files[lock_path] = FileFact(True, True, False, 100, "b" * 64)
                    facts = {"requirements_lock_sha256": "b" * 64}
                elif operation == "install-locks":
                    assert self.held
                    facts = {"requirements_lock_sha256": "b" * 64}
                elif operation == "environment-spec":
                    facts = {"expected": {"fake_environment": name}, "requirements_lock_sha256": "b" * 64}
                else:
                    pytest.fail(operation)
            return self.result(runner._json({"schema": runner._HELPER_SCHEMA, "operation": operation, "facts": facts}))
        if "engine_execution" in " ".join(argv):
            operation, payload = argv[-2], json.loads(argv[-1])
            self.events.append(("service:" + operation, payload, request.deadline))
            if operation == "start":
                assert self.held
                self.server = "owner-incarnation-1"
            state = "absent" if self.server is None else "unowned" if self.server == "alien" else "owned"
            facts = {key: payload[key] for key in ("engine", "platform", "root", "recipe_sha256", "helper_sha256")}
            facts.update(state=state, owner_id=self.server, launch_sha256=runner._sha(payload["launch"]))
            facts.update(target._health_facts(self.health_document if self.health_healthy else None,
                                             self.health_objects, self.recipe.health.required_nodes))
            if operation == "health" and self.health_owner:
                facts["owner_id"] = self.health_owner
            return self.result(runner._json({"schema": runner._SERVICE_SCHEMA, "operation": operation, "facts": facts}))
        if argv[0].endswith("/uv"):
            if argv[1:] == ("--version",):
                return self.result("uv " + self.recipe.environments[0].uv_version)
            assert self.held or "check" in argv
            if "check" in argv:
                python = argv[argv.index("--python") + 1]
                environment = next(e for e in self.recipe.environments
                                   if python == self.adapter.venv_python(self.adapter.join(self.root, e.relative_path)))
                self.environments[environment.name] = True
            return self.result()
        if argv[0].endswith("/bin/python") and "-c" in argv:
            assert request.env["CUDA_VISIBLE_DEVICES"] == ""
            expected = json.loads(argv[-1])
            if "fake_environment" not in expected:  # install receipt digest script
                return self.result(runner._json({"record_sha256": {n: "c" * 64 for n in expected["wheels"]}}))
            return self.result(code=0 if self.environments.get(expected["fake_environment"]) else 1)
        pytest.fail("unexpected command: " + str(argv))


class FakeHttpsContext(FakeContext):
    placement = "runpod"

    def file_fact(self, path, *, hash_bytes, deadline):
        result = super().file_fact(path, hash_bytes=hash_bytes, deadline=deadline)
        return FileFact(**json.loads(json.dumps(asdict(result))))

    def download_model(self, model, *, models_root, deadline):
        model = type(model)(**json.loads(json.dumps(asdict(model))))
        result = super().download_model(model, models_root=models_root, deadline=deadline)
        return DownloadResult(**json.loads(json.dumps(asdict(result))))

    def run(self, request):
        # Exercise wire shapes (tuples -> arrays, mapping -> JSON) without a
        # provider, controller download, remote shell, bearer or SSH dependency.
        raw = json.loads(json.dumps(asdict(request)))
        raw["argv"] = tuple(raw["argv"])
        response = super().run(CommandRequest(**raw))
        return CommandResult(**json.loads(json.dumps(asdict(response))))


def _resolve_once_recipe():
    """The pre-lock Linux recipe: same sources/wheels, resolved by the canonical compile at install time."""
    from astrid.core.generation.engine_contracts import RequirementsSpec, SourcePath
    recipe = recipes.load_recipe("comfyui", PLATFORM)
    requirements = RequirementsSpec(
        "resolve-once", None,
        (SourcePath("comfyui", "requirements.txt"), SourcePath("vibecomfy", "pyproject.toml"),
         SourcePath("videohelpersuite", "requirements.txt")),
        ("comfy-kitchen==0.2.35", "comfy-aimdo==0.5.5", "opencv-python-headless==5.0.0.93", "pip==25.2"),
        "2026-10-19T00:00:00Z")
    return replace(recipe, environments=(replace(recipe.environments[0], requirements=requirements),))


@pytest.fixture
def resolve_once(monkeypatch):
    recipe = _resolve_once_recipe()
    monkeypatch.setattr(recipes, "load_recipe", lambda *_: recipe)
    return recipe


@pytest.fixture
def setup(monkeypatch):
    adapter = FakeAdapter()
    context = FakeContext()
    deadline = time.monotonic() + 45
    context.deadline = deadline
    plan = runner.plan_install(context.recipe, root=ROOT, placement="local")
    module = types.ModuleType("astrid.core.generation.engine_platforms")
    module.platform_adapter = lambda key: adapter
    monkeypatch.setitem(sys.modules, module.__name__, module)
    return plan, context, adapter, deadline


def install(setup):
    plan, context, adapter, deadline = setup
    return runner.install_engine(plan, context=context, adapter=adapter, deadline=deadline)


def test_install_writes_private_receipt_per_final_wheel(setup, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "2")
    receipt = install(setup)
    start = next(step for step in receipt.steps if step.kind == "start")
    assert start.facts["cuda_visible_devices"] == "2"
    context = setup[1]
    wheels = context.recipe.environments[0].final_wheels
    assert wheels
    for wheel in wheels:
        path = context.adapter.join(context.root, ".astrid-engine/environments",
                                    context.recipe.environments[0].name, target.receipt_name(wheel.name))
        assert context.files[path] == {"name": wheel.name, "version": wheel.version, "url": wheel.url,
                                       "sha256": wheel.sha256, "record_sha256": "c" * 64}


def status(setup):
    plan, context, adapter, deadline = setup
    return runner.engine_status(plan.engine, platform=plan.platform, placement=plan.placement,
                                root=plan.root, context=context, deadline=deadline)


@pytest.mark.parametrize('failure', ['none', 'cancel', 'changed', 'before-replace', 'after-replace', 'bad-health'])
def test_refresh_preserves_raw_receipt_and_has_safe_commit_boundary(setup, monkeypatch, failure):
    from concurrent.futures import CancelledError
    plan, context, adapter, deadline = setup
    install(setup)
    path = ROOT + '/' + runner._RECEIPT
    raw = copy.deepcopy(context.files[path])
    legacy = {'id': 'model:legacy', 'kind': 'model', 'action': 'executed', 'validation': 'passed',
              'facts': {'legacy': 'preserve'}}
    raw['steps'].append(legacy)
    for step in raw['steps']:
        if step['kind'] in {'start', 'health'}:
            step['facts']['unrelated'] = 'preserved'
    context.files[path] = copy.deepcopy(raw)
    live = runner._Runner(plan, context, adapter, deadline, device=2)
    with context.acquire_install_lock(ROOT, deadline=deadline):
        action, start_facts = live.start()
        health_action, health_facts = live.check_health()
        calls = []
        def check():
            calls.append(True)
            if failure == 'cancel' and len(calls) == 2:
                raise CancelledError('cancel before commit')
        if failure == 'changed':
            context.files[path]['receipt_id'] = 'changed-by-other-writer'
        if failure in {'before-replace', 'after-replace'}:
            write = context.write_json_atomic
            def failing_write(path, value, *, deadline):
                if failure == 'after-replace':
                    write(path, value, deadline=deadline)
                raise OSError('injected atomic write failure')
            monkeypatch.setattr(context, 'write_json_atomic', failing_write)
        if failure == 'bad-health':
            health_facts = {**health_facts, 'object_info_sha256': '0' * 64}
        if failure == 'none':
            digest = runner.refresh_installed_launch(live, raw, start_action=action, start_facts=start_facts,
                health_action=health_action, health_facts=health_facts, check=check)
            assert digest == hashlib.sha256(runner._json(context.files[path]).encode()).hexdigest()
        else:
            error = (CancelledError if failure == 'cancel' else OSError if failure in {'before-replace', 'after-replace'}
                     else IntegrityError)
            with pytest.raises(error):
                runner.refresh_installed_launch(live, raw, start_action=action, start_facts=start_facts,
                    health_action=health_action, health_facts=health_facts, check=check)
    committed = context.files[path]
    if failure in {'none', 'after-replace'}:
        assert next(step for step in committed['steps'] if step['kind'] == 'start')['facts']['cuda_visible_devices'] == '2'
        assert committed['steps'][-1] == legacy
        assert all(step['facts']['unrelated'] == 'preserved' for step in committed['steps']
                   if step['kind'] in {'start', 'health'})
        for key in raw:
            if key not in {'steps', 'health'}:
                assert committed[key] == raw[key]
        for old, new in zip(raw['steps'], committed['steps']):
            if old['kind'] not in {'start', 'health'}:
                assert old == new
    elif failure == 'changed':
        assert committed['receipt_id'] == 'changed-by-other-writer'
    else:
        assert committed == raw


def test_protocol_shapes_exactly_match_v1():
    assert [f.name for f in fields(CommandRequest)] == ["argv", "cwd", "env", "deadline", "max_output_bytes"]
    assert CommandRequest(("true",), ROOT, {}, 123).max_output_bytes == 262144
    assert [f.name for f in fields(CommandResult)] == ["exit_code", "stdout", "stderr", "timed_out", "duration_seconds", "job_id", "stdout_truncated", "stderr_truncated"]
    assert [f.name for f in fields(FileFact)] == ["exists", "regular", "symlink", "bytes", "sha256"]


def test_plan_is_complete_deterministic_and_has_real_names(setup):
    plan, context, *_ = setup
    assert plan == runner.plan_install(context.recipe, root=ROOT, placement="local")
    expected_ids = [
        *("source:" + source.name for source in context.recipe.sources),
        *("environment:" + environment.name for environment in context.recipe.environments),
        "start", "health",
    ]
    # Plan 32 T5h: the install is model-free, so the plan has no model steps.
    assert [s.id for s in plan.steps] == expected_ids
    assert len(plan.steps) == len(context.recipe.sources) + len(context.recipe.environments) + 2
    assert not [s for s in plan.steps if s.id.startswith("model:")]
    seen = set()
    for step in plan.steps:
        assert set(step.depends_on) <= seen
        seen.add(step.id)
    assert plan.models_lock_sha256 == context.recipe.models_lock.sha256
    assert not context.events


@pytest.mark.parametrize("root", ["relative", "/", "/target/../escape", "/target//root", "/target/", "//target/root", "C:\\target", "/target\nroot"])
def test_unsafe_plan_roots_rejected(setup, root):
    with pytest.raises(UnsafePathError):
        runner.plan_install(setup[1].recipe, root=root, placement="local")


def test_windows_schema_plans_but_execution_refuses_before_context_io(setup):
    recipe = recipes.load_recipe("comfyui", "windows-x86_64-cu130")
    plan = runner.plan_install(recipe, root=r"C:\engines\vibe", placement="local")
    assert plan.root == r"C:\engines\vibe"
    with pytest.raises(UnsupportedPlatform):
        runner.plan_install(recipe, root=plan.root, placement="runpod")
    setup[1].platform = setup[2].key = recipe.platform
    with pytest.raises(UnsupportedPlatform):
        runner.install_engine(plan, context=setup[1], adapter=setup[2], deadline=setup[3])
    assert not setup[1].events


def test_unknown_placement_refuses(setup):
    with pytest.raises(UnsupportedPlacement):
        runner.plan_install(setup[1].recipe, root=ROOT, placement="cloud")


@pytest.mark.parametrize("change", [
    lambda p: replace(p, recipe_sha256="0" * 64),
    lambda p: replace(p, models_lock_sha256="0" * 64),
    lambda p: replace(p, schema="astrid.engine-plan.v2"),
    lambda p: replace(p, steps=p.steps[:-1]),
    lambda p: replace(p, steps=(replace(p.steps[0], description="run attacker command"), *p.steps[1:])),
    lambda p: replace(p, steps=tuple(reversed(p.steps))),
])
def test_forged_plan_refuses_before_target_io(setup, change):
    plan, context, adapter, deadline = setup
    with pytest.raises(IntegrityError):
        runner.install_engine(change(plan), context=context, adapter=adapter, deadline=deadline)
    assert context.events == []


def test_mutated_recipe_is_not_a_planning_input(setup):
    with pytest.raises(IntegrityError):
        runner.plan_install(replace(setup[1].recipe, start=replace(setup[1].recipe.start, port=9999)),
                            root=ROOT, placement="local")


@pytest.mark.parametrize("part", ["context-platform", "context-placement", "adapter"])
def test_target_identity_must_match_before_io(setup, part):
    plan, context, adapter, deadline = setup
    if part == "context-platform":
        context.platform = "linux-x86_64-cu128"
    elif part == "context-placement":
        context.placement = "runpod"
    else:
        adapter.key = "linux-x86_64-cu128"
    with pytest.raises(UnsupportedPlatform):
        install(setup)
    assert context.events == []


def test_full_install_local_and_https_produce_same_steps_and_facts(setup):
    plan, context, adapter, deadline = setup
    local = install(setup)
    https = FakeHttpsContext()
    https.deadline = deadline
    remote = runner.install_engine(replace(plan, placement="runpod"), context=https, adapter=adapter, deadline=deadline)
    assert local.status == remote.status == "installed"
    assert local.qualified is remote.qualified is False
    assert local.steps == remote.steps
    assert local.models == remote.models
    assert local.source_commits == remote.source_commits
    assert local.requirements_locks == remote.requirements_locks == {"comfy": "b" * 64}
    assert local.health.object_info_sha256 == runner._sha(context.health_objects)
    assert all(s.validation == "passed" for s in local.steps)
    for ctx in (context, https):
        assert ctx.events[0][0] == "acquire" and ctx.events[-1][0] == "release"
        assert ctx.files[ROOT + "/" + runner._RECEIPT]["status"] == "installed"
        assert not ctx.held


def test_clean_rerun_skips_every_step_but_reads_sha_and_current_health(setup):
    first = install(setup)
    context = setup[1]
    context.events.clear()
    second = install(setup)
    assert first.status == second.status == "installed"
    assert all(s.action == "skipped" and s.validation == "passed" for s in second.steps)
    assert context.bootstrap_count == 1
    # Plan 32 T5h: a model-free install downloads and hashes no model on rerun.
    assert not [e for e in context.events if e[0] == "download"]
    assert not [e for e in context.events if e[0] == "hash" and "/models/" in e[1]]
    assert len([e for e in context.events if e[0] == "service:health"]) >= 1
    assert not [e for e in context.events if e[0] == "service:start"]
    assert not [e for e in context.events if e[0] == "argv" and ("fetch" in e[1] or "install" in e[1] or "sync" in e[1])]


def test_run_two_reuses_the_instance_run_one_left_running(setup):
    from astrid.core.generation.engine_local_endpoint import _installed_receipt

    install(setup)
    plan, context, _, _ = setup
    def run():
        return _installed_receipt(plan.engine, platform=plan.platform, root=plan.root,
                                  deadline_seconds=30, context=context, status=None)

    first = run()
    owner = context.server
    assert owner is not None
    assert len([event for event in context.events if event[0] == "service:start"]) == 1
    context.events.clear()
    second = run()
    assert first.status == second.status == "installed"
    assert context.server == owner
    assert any(event[0] == "service:health" for event in context.events)
    assert not [event for event in context.events if event[0] in {"service:start", "service:stop"}]


def test_run_two_with_a_listener_no_supervisor_owns_is_coded_ownership_not_not_installed(setup):
    from astrid.core.generation.engine_local_endpoint import _installed_receipt

    install(setup)
    plan, context, _, _ = setup
    first = _installed_receipt(plan.engine, platform=plan.platform, root=plan.root,
                               deadline_seconds=30, context=context, status=None)
    assert first.status == "installed"
    context.server = "alien"
    context.events.clear()
    with pytest.raises(EndpointOwnershipError) as caught:
        _installed_receipt(plan.engine, platform=plan.platform, root=plan.root,
                           deadline_seconds=30, context=context, status=None)
    assert caught.value.code == "endpoint_ownership"
    assert not [event for event in context.events if event[0] in {"service:start", "service:stop"}]


def test_target_paths_never_touch_controller_filesystem_or_processes(setup, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("controller subprocess"))
    original_open = Path.open
    def guarded_open(path, *args, **kwargs):
        assert not str(path).startswith(ROOT)
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded_open)
    assert install(setup).status == "installed"


def test_order_generated_hash_lock_before_sync_and_wheels_last(resolve_once, setup):
    install(setup)
    events = setup[1].events
    commands = [e[1] for e in events if e[0] == "argv"]
    compile_index = next(i for i, a in enumerate(commands) if "compile" in a)
    commit_index = next(i for i, a in enumerate(commands) if "commit-lock" in a)
    sync_index = next(i for i, a in enumerate(commands) if "sync" in a)
    install_indexes = [i for i, a in enumerate(commands) if "install" in a]
    assert compile_index < commit_index < sync_index < min(install_indexes)
    assert "--require-hashes" in commands[max(install_indexes)]
    assert "wheels.lock" in " ".join(commands[max(install_indexes)])
    assert all("--no-deps" in commands[i] for i in install_indexes)
    assert not any(a[0] in {"sh", "bash", "curl", "wget"} for a in commands)


@pytest.mark.parametrize("declared", [False, True])
def test_source_install_commands_come_only_from_environment_spec(setup, monkeypatch, declared):
    plan, context, adapter, deadline = setup
    environment = context.recipe.environments[0]
    if not declared:
        environment = replace(environment, source_installs=())
    recipe = replace(context.recipe, environments=(environment,))
    monkeypatch.setattr(recipes, "load_recipe", lambda *_: recipe)
    context.recipe = recipe
    plan = runner.plan_install(recipe, root=ROOT, placement="local")
    assert runner.install_engine(plan, context=context, adapter=adapter, deadline=deadline).status == "installed"
    commands = [event[1] for event in context.events if event[0] == "argv"]
    source_commands = [argv for argv in commands if "install" in argv and "--require-hashes" not in argv]
    expected = [tuple(arg.format_map({"install_root": ROOT, "python": ROOT + "/runtime/venv/bin/python"})
                      for arg in install.argv) for install in environment.source_installs]
    assert source_commands == expected
    if declared:
        assert source_commands[0][0] == ROOT + "/runtime/bootstrap/bin/uv"
        assert source_commands[0][-1] == "."
        command_index = next(i for i, event in enumerate(context.events)
                             if event[0] == "argv" and event[1] == source_commands[0])
        assert context.events[command_index - 1][1] == ROOT + "/runtime/vibecomfy"


@pytest.mark.parametrize("error", [
    CommandResult(17, "secret arbitrary output", "Authorization: Bearer secret", False, 0.1, "job1", False, False),
    CommandResult(None, "", "", True, 0.1, "job1", False, False),
    CustodyUnresolved("reconcile exact job", recovery_handle="/custody/exact-job-97"),
])
def test_command_failure_timeout_and_custody_keep_authority(setup, error):
    setup[1].fail = ("git", error)
    expected = CustodyUnresolved if isinstance(error, CustodyUnresolved) else DeadlineExceeded if error.timed_out else CommandFailed
    with pytest.raises(expected) as caught:
        install(setup)
    assert caught.value.step_id == "source:comfyui"
    assert "secret" not in str(caught.value)
    if isinstance(error, CustodyUnresolved):
        assert caught.value is error
        assert caught.value.recovery_handle == "/custody/exact-job-97"
    receipt = setup[1].files[ROOT + "/" + runner._RECEIPT]
    assert receipt["status"] == "failed"
    assert receipt["steps"][-1]["validation"] == "failed"
    assert receipt["qualified"] is False
    assert not setup[1].held
    assert not [e for e in setup[1].events if e[0] == "download"]


def test_expired_deadline_never_acquires_lock(setup):
    with pytest.raises(DeadlineExceeded):
        runner.install_engine(setup[0], context=setup[1], adapter=setup[2], deadline=time.monotonic() - 1)
    assert not setup[1].events


def test_deadline_propagates_to_every_context_operation(setup):
    install(setup)
    deadline = setup[3]
    assert all(e[2] == deadline for e in setup[1].events if e[0] not in {"service:health", "argv"} or e[0] == "argv" and "health" not in e[1])


def test_wrong_helper_identity_has_no_install_side_effect(setup):
    setup[1].wrong_identity = True
    with pytest.raises(IntegrityError) as failed:
        install(setup)
    assert failed.value.step_id == "helper-identity"
    assert not [e for e in setup[1].events if e[0] in {"write", "mkdir", "download"}]


@pytest.mark.parametrize("command", ["install", "status"])
def test_reinstall_cli_exposes_safe_clean_root_command(tmp_path, capsys, command, monkeypatch):
    import shlex

    from astrid.core import engine_cli
    from astrid.core.generation.engine_contracts import EngineReinstallRequired

    monkeypatch.setattr(engine_cli, "_disk_anchor", lambda root: ("/", 1, 1, 10**12))
    root = str(tmp_path / "old engine")
    environment = "comfy"

    def refuse(**kwargs):
        raise EngineReinstallRequired.for_install(
            engine="comfyui", platform=PLATFORM, root=root, environment=environment,
            step_id="environment:comfy")

    rc = engine_cli.main([command, "comfyui", "--platform", PLATFORM,
                          "--placement", "local", "--root", root], backend_factory=refuse)
    assert rc == 1
    error = json.loads(capsys.readouterr().err)["error"]
    clean_root = root + "-clean"
    expected = shlex.join(("astrid", "engines", "install", "comfyui", "--platform",
                           PLATFORM, "--placement", "local", "--root", clean_root))
    assert error["code"] == "engine_reinstall_required"
    assert error["environment"] == environment
    assert error["clean_root"] == clean_root
    assert error["install_command"] == expected
    assert clean_root in error["message"] and expected in error["message"]


def test_old_environment_identity_refuses_then_clean_root_installs_after_owned_stop(
        tmp_path, monkeypatch, capsys):
    """One local journey exercises byte refusal, original stop, and a real CPU reinstall."""
    import io
    import base64
    import zipfile
    import os
    import select
    import shlex
    import socket
    import threading
    from contextlib import redirect_stdout
    from dataclasses import replace

    from astrid.core import engine_cli
    from astrid.core.generation import engine_custody as custody, engine_recipe
    # The free-space floor is a policy gate; this journey runs a real install on a sandbox disk.
    monkeypatch.setattr(engine_cli, "_disk_anchor", lambda root: (str(root), 1, 1, 10**15))
    from astrid.core.generation.engine_contracts import (
        CommandResult, DataRef, EngineRecipe, EngineReinstallRequired, HealthSpec, RequirementsSpec,
        SourcePin, SourceSpec, StartSpec,
    )
    from astrid.core.generation import engine_execution
    from astrid.core.generation.engine_execution import LocalLinuxExecutionContext
    from astrid.core.generation.engine_platforms import platform_adapter

    uv = shutil.which("uv")
    assert uv, "the real uv executable is required for the CPU install fixture"
    uv_version = subprocess.check_output([uv, "--version"], text=True, timeout=5).split()[1]
    python_version = sys.version.split()[0]
    model_bytes = b"small locked CPU model fixture\n"
    fixture = tmp_path / "cpu-fixture"
    fixture.mkdir()
    wheel_path = fixture / "tinyfixture-1.0-py3-none-any.whl"
    wheel_members = {
        "tinyfixture/__init__.py": b"VALUE = 'cpu-fixture'\n",
        "tinyfixture-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: tinyfixture\nVersion: 1.0\n\n",
        "tinyfixture-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    rows = []
    for name, payload in wheel_members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")
        rows.append((name, "sha256=" + digest, str(len(payload))))
    record_path = "tinyfixture-1.0.dist-info/RECORD"
    rows.append((record_path, "", ""))
    record_data = "".join(",".join(row) + "\n" for row in rows).encode()
    with zipfile.ZipFile(wheel_path, "w") as archive:
        for name, payload in wheel_members.items():
            archive.writestr(name, payload)
        archive.writestr(record_path, record_data)
    wheel_digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
    packaging_dist = metadata.distribution("packaging")
    packaging_version = packaging_dist.version
    packaging_wheel = fixture / f"packaging-{packaging_version}-py3-none-any.whl"
    packaging_members = {}
    for relative in packaging_dist.files or ():
        first = relative.parts[0]
        if first == "packaging" or (first.startswith("packaging-") and first.endswith(".dist-info")):
            if relative.name != "RECORD":
                packaging_members[str(relative)] = packaging_dist.locate_file(relative).read_bytes()
    packaging_record_rows = []
    for name, payload in packaging_members.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")
        packaging_record_rows.append((name, "sha256=" + digest, str(len(payload))))
    packaging_record = f"packaging-{packaging_version}.dist-info/RECORD"
    packaging_record_rows.append((packaging_record, "", ""))
    packaging_record_data = "".join(",".join(row) + "\n" for row in packaging_record_rows).encode()
    with zipfile.ZipFile(packaging_wheel, "w") as archive:
        for name, payload in packaging_members.items():
            archive.writestr(name, payload)
        archive.writestr(packaging_record, packaging_record_data)
    packaging_digest = hashlib.sha256(packaging_wheel.read_bytes()).hexdigest()
    lock_bytes = (f"tinyfixture @ {wheel_path.as_uri()} --hash=sha256:{wheel_digest}\n"
                  f"packaging @ {packaging_wheel.as_uri()} --hash=sha256:{packaging_digest}\n").encode()
    (fixture / "requirements.lock").write_bytes(lock_bytes)
    (fixture / "model.bin").write_bytes(model_bytes)
    with socket.socket() as port_probe:
        port_probe.bind(("127.0.0.1", 0))
        port = port_probe.getsockname()[1]
    server_text = (
        "from http.server import BaseHTTPRequestHandler,HTTPServer\n"
        "import json,sys\n"
        "class H(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  value={'system':{'os':'linux'}} if self.path=='/system_stats' else {'FixtureNode':{}}\n"
        "  body=json.dumps(value).encode(); self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)\n"
        " def log_message(self,*args): pass\n"
        "HTTPServer(('127.0.0.1',int(sys.argv[1])),H).serve_forever()\n")
    model_digest = hashlib.sha256(model_bytes).hexdigest()
    model_lock_bytes = runner._json({"schema": "astrid.models-lock.v1", "models": [{
        "name": "cpu-model", "repository": None, "revision": None, "path": None,
        "url": "https://fixture.invalid/model.bin", "destination": "diffusion_models/cpu.bin",
        "bytes": len(model_bytes), "sha256": model_digest,
    }]}).encode()
    model_lock_sha = hashlib.sha256(model_lock_bytes).hexdigest()
    (fixture / "models.json").write_bytes(model_lock_bytes)

    # Each source is a real tiny pinned Git repository. The runner performs its
    # normal init/fetch/checkout and verifies each resulting commit.
    source_specs = []
    source_repositories = {}
    for name, relative in (("comfyui", "runtime/ComfyUI"), ("vibecomfy", "runtime/vibecomfy")):
        work = fixture / (name + "-work")
        work.mkdir()
        (work / "tiny.py").write_text("# immutable CPU source fixture\n", encoding="utf-8")
        if name == "comfyui":
            (work / "server.py").write_text(server_text, encoding="utf-8")
        for command in (["git", "init", "-q"], ["git", "add", "."],
                        ["git", "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
                         "commit", "-qm", "fixture"]):
            subprocess.run(command, cwd=work, check=True, capture_output=True, timeout=5)
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=work,
                                           text=True, timeout=5).strip()
        bare = fixture / (name + ".git")
        subprocess.run(["git", "clone", "--bare", str(work), str(bare)], check=True,
                       capture_output=True, timeout=5)
        source_repositories[name] = bare
        source_specs.append(SourceSpec(name, SourcePin(f"https://fixture.invalid/{name}.git", revision), relative))

    pins_bytes = runner._json({"schema": "astrid.engine-pins.v1", "sources": {
        source.name: {"repository": source.pin.repository, "revision": source.pin.revision}
        for source in source_specs
    }}).encode()
    (fixture / "pins.json").write_bytes(pins_bytes)

    original = recipes.load_recipe("comfyui", PLATFORM)
    base_environment = original.environments[0]
    environment = replace(
        base_environment, name="comfy", relative_path="runtime/cpu-venv",
        python_version=python_version, uv_version=uv_version,
        requirements=RequirementsSpec("packaged", DataRef("cpu-fixture/requirements.lock",
                                      hashlib.sha256(lock_bytes).hexdigest()), (), (), None),
        final_wheels=(), node_lock=None, source_installs=())
    model_ref = DataRef("cpu-fixture/models.json", model_lock_sha)
    recipe_draft = EngineRecipe(
        original.schema, original.engine, original.platform,
        DataRef("cpu-fixture/pins.json", hashlib.sha256(pins_bytes).hexdigest()), tuple(source_specs),
        (environment,), "comfy", model_ref,
        StartSpec(("{python}", "server.py", str(port)), "{comfy_root}", {}, "127.0.0.1", port),
        HealthSpec("/system_stats", "/object_info", ("FixtureNode",), 5.0, 0.05))

    source_code_root = engine_recipe._ROOT
    current_code_root = tmp_path / "current-python/astrid"
    shutil.copytree(source_code_root, current_code_root,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    for name in ("requirements.lock", "models.json", "pins.json"):
        destination = current_code_root / "cpu-fixture" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture / name, destination)
    recipe_rel = "packs/runpod/cpu-recipe.json"
    def ref_json(ref):
        return {"path": ref.path, "sha256": ref.sha256}
    recipe_document = {
        "schema": recipe_draft.schema, "engine": recipe_draft.engine, "platform": recipe_draft.platform,
        "pins": ref_json(recipe_draft.pins),
        "source_bindings": [{"name": source.name, "pin": source.name,
                             "relative_path": source.relative_path} for source in recipe_draft.sources],
        "environments": [{
            "name": environment.name, "relative_path": environment.relative_path,
            "python_version": environment.python_version, "uv_version": environment.uv_version,
            "requirements": {"mode": environment.requirements.mode,
                "packaged_lock": ref_json(environment.requirements.packaged_lock),
                "input_paths": [], "constraints": [], "exclude_newer": None},
            "final_wheels": [], "node_lock": None, "source_installs": [],
        } for environment in recipe_draft.environments],
        "primary_environment": recipe_draft.primary_environment,
        "models_lock": ref_json(recipe_draft.models_lock),
        "start": {"argv": list(recipe_draft.start.argv), "cwd": recipe_draft.start.cwd,
            "env": dict(recipe_draft.start.env), "bind_host": recipe_draft.start.bind_host,
            "port": recipe_draft.start.port},
        "health": {"path": recipe_draft.health.path,
            "object_info_path": recipe_draft.health.object_info_path,
            "required_nodes": list(recipe_draft.health.required_nodes),
            "timeout_seconds": recipe_draft.health.timeout_seconds,
            "poll_interval_seconds": recipe_draft.health.poll_interval_seconds},
    }
    recipe_bytes = json.dumps(recipe_document, sort_keys=True).encode()
    recipe_path = current_code_root / recipe_rel
    recipe_path.parent.mkdir(parents=True, exist_ok=True)
    recipe_path.write_bytes(recipe_bytes)
    declarations = current_code_root / "packs"
    for stale in declarations.glob("*/extensions/generation.engine/*.yaml"):
        stale.unlink()
    declaration = declarations / f"vibecomfy/extensions/generation.engine/{recipe_draft.engine}.yaml"
    declaration.parent.mkdir(parents=True, exist_ok=True)
    declaration.write_text(
        f"recipes:\n  - platform: {recipe_draft.platform}\n    path: {recipe_rel}\n"
        f"    sha256: \"{hashlib.sha256(recipe_bytes).hexdigest()}\"\n")
    monkeypatch.setattr(engine_recipe, "_ROOT", current_code_root)
    recipe = engine_recipe.load_recipe("comfyui", PLATFORM)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ASTRID_INSTALL_UV", uv)

    # Keep the full model downloader and its range/hash checks. Only the
    # unavailable HTTPS response is supplied at the urllib network boundary.
    from astrid.core.generation import model_downloader
    class RangeResponse:
        status = 206
        def __init__(self, data, content_range):
            self._stream = io.BytesIO(data)
            self.headers = {"Content-Range": content_range}
        def read(self, size=-1):
            return self._stream.read(size)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self._stream.close()
    def model_response(request, *, timeout):
        start, end = map(int, request.get_header("Range").removeprefix("bytes=").split("-"))
        return RangeResponse(model_bytes[start:end + 1], f"bytes {start}-{end}/{len(model_bytes)}")
    monkeypatch.setattr(model_downloader.urllib.request, "urlopen", model_response)

    # Retain the production target dispatcher and target-file checks in a real
    # supervised child process, selecting the exact owner's package snapshot.
    original_context_run = LocalLinuxExecutionContext.run
    target_events, command_failures = [], []
    def local_run(self, request):
        argv = request.argv
        if tuple(argv[:4]) == ("python3", "-I", "-m", "astrid.core.generation.engine_runner_target"):
            selected_code_root = str(engine_recipe._ROOT)
            child_program = ("import sys; sys.dont_write_bytecode=True; "
                             f"sys.path.insert(0, {str(Path(selected_code_root).parent)!r}); "
                             "from astrid.core.generation import engine_runner_target as target; "
                             "raise SystemExit(target.main(sys.argv[1:]))")
            translated = (sys.executable, "-I", "-c", child_program, *argv[4:])
            result = original_context_run(self, replace(request, argv=translated))
            target_events.append({"operation": argv[4], "exit_code": result.exit_code,
                                  "stdout": result.stdout, "stderr": result.stderr})
            return result
        if (len(argv) >= 7 and tuple(argv[:5]) == ("git", "-C", argv[2], "fetch", "--depth=1")
                and argv[5] == "origin"):
            source = next(source for source in recipe.sources
                          if Path(argv[2]).as_posix().endswith(source.relative_path))
            translated = (*argv[:5], str(source_repositories[source.name]), *argv[6:])
            return original_context_run(self, replace(request, argv=translated))
        # Keep the ordinary bootstrap and uv venv creation on the local
        # supervisor's process channel. Copying the already available pinned
        # uv binary avoids an index download; the actual uv version is checked
        # by the runner before it uses the executable.
        if argv and argv[0] == uv and "--prefix" in argv and any(arg.startswith("uv==") for arg in argv):
            uv_target = Path(argv[argv.index("--prefix") + 1]) / "bin/uv"
            uv_target.parent.mkdir(parents=True, exist_ok=True)
            return original_context_run(self, replace(request, argv=("cp", uv, str(uv_target))))
        if argv and Path(argv[0]).name == "uv" and "venv" in argv and "--managed-python" in argv:
            destination = argv[-1]
            command = (argv[0], "--no-config", "venv", "--allow-existing",
                       "--python", sys.executable, destination)
            return original_context_run(self, replace(request, argv=command))
        try:
            result = original_context_run(self, request)
        except BaseException as exc:
            command_failures.append((argv, type(exc).__name__, str(exc)))
            raise
        if result.exit_code != 0:
            command_failures.append((argv, result.exit_code, result.stdout, result.stderr))
        return result
    monkeypatch.setattr(LocalLinuxExecutionContext, "run", local_run)

    # Reconstruct an older selected package from the real candidate package
    # snapshot, changing only one selected helper byte after copying it.
    current_code_root = engine_recipe._ROOT
    source_code_root = current_code_root
    baseline_manifest = runner.helper_manifest(recipe)
    old_code_root = tmp_path / "old-python/astrid"
    shutil.copytree(current_code_root, old_code_root,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    changed_helper = old_code_root / "core/generation/engine_contracts.py"
    changed_helper.write_bytes(changed_helper.read_bytes() + b"\n# prior helper fixture\n")
    monkeypatch.setattr(engine_recipe, "_ROOT", old_code_root)

    old_root = str(tmp_path / "old-engine")
    clean_root = old_root + "-clean"

    def supervisor_argv(identity, lock_fd, token):
        # Import the real supervisor modules from exactly the package snapshot
        # selected by this owner's installer. Its normal parser loads the CPU
        # recipe and hashes the actual package closure.
        selected_code_root = str(engine_recipe._ROOT)
        child_program = ("import sys; sys.dont_write_bytecode=True; "
                         f"sys.path.insert(0, {str(Path(selected_code_root).parent)!r}); "
                         "from astrid.core.generation.engine_custody import main; main()")
        return [sys.executable, "-I", "-c", child_program, "_serve", runner._json(identity),
                "--lock-fd", str(lock_fd), "--launch-token", token]
    monkeypatch.setattr(custody, "_supervisor_argv", supervisor_argv)

    contexts = {}
    original_backend_factory = engine_execution.create_local_backend
    def observe_backend(*, engine, platform, placement, root, deadline):
        context, adapter = original_backend_factory(engine=engine, platform=platform,
            placement=placement, root=root, deadline=deadline)
        contexts[root] = context
        return context, adapter
    monkeypatch.setattr(engine_execution, "create_local_backend", observe_backend)

    old_context = None
    try:
        with redirect_stdout(io.StringIO()):
            install_code = engine_cli.main(["install", recipe.engine, "--platform", PLATFORM,
                                            "--placement", "local", "--root", old_root])
        (tmp_path / "journey-debug.json").write_text(json.dumps({
            "target_events": target_events, "command_failures": command_failures,
        }, sort_keys=True, indent=2))
        assert install_code == 0, {"target_events": target_events, "command_failures": command_failures}
        old_context = contexts[old_root]
        old_client = old_context.service_client
        old_helper = old_client.identity["helper_sha256"]
        old_lock_path = Path(old_root, ".astrid-engine/environments/comfy/requirements.lock")
        target_lock = old_lock_path.with_name("lock.json")
        receipt_path = old_lock_path.with_name("receipt.json")
        install_receipt_path = Path(old_root, ".astrid-engine/install.json")
        original_requirements_bytes = old_lock_path.read_bytes()
        original_target_lock_bytes = target_lock.read_bytes()
        original_receipt_bytes = receipt_path.read_bytes()
        original_install_bytes = install_receipt_path.read_bytes()
        assert json.loads(target_lock.read_text())["helper_sha256"] == old_helper
        assert json.loads(receipt_path.read_text())["helper_sha256"] == old_helper
        assert runner._sha(baseline_manifest) != old_helper

        monkeypatch.setattr(engine_recipe, "_ROOT", current_code_root)
        current_helper = runner._sha(runner.helper_manifest(recipe))
        deadline = time.monotonic() + 60
        stale = runner.engine_status(recipe.engine, platform=PLATFORM, placement="local",
            root=old_root, context=observe_backend(engine=recipe.engine, platform=PLATFORM,
                placement="local", root=old_root, deadline=deadline)[0], deadline=deadline)
        assert (stale.status, stale.error_code, stale.failed_step_id) == (
            "failed", "engine_reinstall_required", "environment:comfy")
        status_code = engine_cli.main(["status", recipe.engine, "--platform", PLATFORM,
            "--placement", "local", "--root", old_root])
        captured = capsys.readouterr()
        diagnostic = json.loads(captured.err)["error"]
        command = (f"astrid engines install {recipe.engine} --platform {PLATFORM} "
                   f"--placement local --root {shlex.quote(clean_root)}")
        assert status_code == 1 and diagnostic["code"] == "engine_reinstall_required"
        assert diagnostic["environment"] == "comfy" and diagnostic["clean_root"] == clean_root
        assert diagnostic["install_command"] == command and command in diagnostic["message"]
        marker_path = Path(old_root, "runtime/out/sessions", recipe.engine, "launch.json")
        marker_before_stop = marker_path.read_bytes()
        install_code = engine_cli.main(["install", recipe.engine, "--platform", PLATFORM,
            "--placement", "local", "--root", old_root])
        install_diagnostic = json.loads(capsys.readouterr().err)["error"]
        assert install_code == 1 and install_diagnostic["code"] == "engine_reinstall_required"
        assert install_diagnostic["environment"] == "comfy"
        # Resource availability is the counterfactual input in this negative
        # CLI check; the old tuple and every identity read remain real.
        with monkeypatch.context() as space:
            space.setattr(engine_cli, "_disk_anchor", lambda root: ("/", 1, 1, 0))
            blocked_code = engine_cli.main(["install", recipe.engine, "--platform", PLATFORM,
                "--placement", "local", "--root", old_root])
        blocked_diagnostic = json.loads(capsys.readouterr().err)["error"]
        assert blocked_code == 1 and blocked_diagnostic["code"] == "engine_reinstall_required"
        assert blocked_diagnostic["environment"] == "comfy"
        assert blocked_diagnostic["install_command"] == command
        assert marker_path.read_bytes() == marker_before_stop
        current_client = contexts[old_root].service_client
        with monkeypatch.context() as patch:
            patch.setattr(custody, "_send", lambda *args, **kwargs: pytest.fail(
                "current identity attempted a request against the old owner"))
            with pytest.raises(EngineReinstallRequired):
                current_client.stop_owned(deadline=time.monotonic() + 10)
        assert old_context.service_client._spawned.poll() is None
        assert marker_path.read_bytes() == marker_before_stop
        assert old_lock_path.read_bytes() == original_requirements_bytes
        assert target_lock.read_bytes() == original_target_lock_bytes
        assert receipt_path.read_bytes() == original_receipt_bytes
        assert install_receipt_path.read_bytes() == original_install_bytes

        registry = Path(old_root, "runtime/out/sessions", recipe.engine)
        marker = json.loads((registry / "launch.json").read_text())
        old_supervisor_pid, old_engine_pid = marker["pid"], marker["comfy_pid"]

        # Relay the original client's stop_owned exchange through a real
        # socketpair into its real filesystem listener and _Supervisor.connection.
        real_connect = old_client._connect
        forwarders = []
        def socketpair_proxy(fd, operation_deadline, *, start):
            unix_channel = real_connect(fd, operation_deadline, start=start)
            local, peer = socket.socketpair()
            def relay():
                try:
                    while True:
                        readable, _, _ = select.select([peer, unix_channel], [], [], 10)
                        if not readable:
                            return
                        for source in readable:
                            data = source.recv(65536)
                            if not data:
                                return
                            (unix_channel if source is peer else peer).sendall(data)
                finally:
                    peer.close()
                    unix_channel.close()
            thread = threading.Thread(target=relay, daemon=True)
            forwarders.append(thread)
            thread.start()
            return local
        old_client._connect = socketpair_proxy
        old_client.stop_owned(deadline=time.monotonic() + 30)
        for thread in forwarders:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert old_client._spawned is not None and old_client._spawned.poll() == 0
        assert not Path(old_client.control, "session.json").exists()
        assert not Path(f"/proc/{old_supervisor_pid}").exists()
        assert not Path(f"/proc/{old_engine_pid}").exists()
        assert old_lock_path.read_bytes() == original_requirements_bytes
        assert target_lock.read_bytes() == original_target_lock_bytes
        assert receipt_path.read_bytes() == original_receipt_bytes
        assert install_receipt_path.read_bytes() == original_install_bytes

        installed_code = engine_cli.main(["install", recipe.engine, "--platform", PLATFORM,
            "--placement", "local", "--root", clean_root])
        captured = capsys.readouterr()
        assert installed_code == 0, captured.err
        assert json.loads(captured.out)["status"] == "installed"
        current_env = Path(clean_root, ".astrid-engine/environments/comfy")
        current_lock = current_env / "lock.json"
        current_receipt = current_env / "receipt.json"
        current_requirements = current_env / "requirements.lock"
        current_install_receipt = Path(clean_root, ".astrid-engine/install.json")
        assert json.loads(current_lock.read_text())["helper_sha256"] == current_helper
        assert json.loads(current_receipt.read_text())["helper_sha256"] == current_helper
        assert runner.engine_status(recipe.engine, platform=PLATFORM, placement="local",
            root=clean_root, context=contexts[clean_root], deadline=time.monotonic()+30).status == "installed"
        assert old_lock_path.read_bytes() == original_requirements_bytes
        assert target_lock.read_bytes() == original_target_lock_bytes
        assert receipt_path.read_bytes() == original_receipt_bytes
        assert install_receipt_path.read_bytes() == original_install_bytes

        evidence = tmp_path / "journey-evidence"
        evidence.mkdir()
        artifacts = {
            "old-requirements.lock": old_lock_path, "old-lock.json": target_lock,
            "old-environment-receipt.json": receipt_path, "old-install.json": install_receipt_path,
            "current-requirements.lock": current_requirements, "current-lock.json": current_lock,
            "current-environment-receipt.json": current_receipt, "current-install.json": current_install_receipt,
        }
        for label, path in artifacts.items():
            shutil.copy2(path, evidence / label)
        (evidence / "summary.json").write_text(json.dumps({
            "old_helper_sha256": old_helper, "current_helper_sha256": current_helper,
            "old_install_sha256": hashlib.sha256(original_install_bytes).hexdigest(),
            "current_install_sha256": hashlib.sha256(current_install_receipt.read_bytes()).hexdigest(),
            "status_refusal": {"code": diagnostic["code"], "environment": diagnostic["environment"],
                               "install_command": diagnostic["install_command"]},
            "old_supervisor_exited": not Path(f"/proc/{old_supervisor_pid}").exists(),
            "old_engine_exited": not Path(f"/proc/{old_engine_pid}").exists(),
            "socketpair_forwarding_completed": all(not thread.is_alive() for thread in forwarders),
            "current_status": "installed",
        }, sort_keys=True, indent=2))
    finally:
        for owned_context in (old_context or contexts.get(old_root), contexts.get(clean_root)):
            if (owned_context is not None and owned_context.service_client._spawned is not None
                    and owned_context.service_client._spawned.poll() is None):
                owned_context.service_client.stop_owned(deadline=time.monotonic() + 20)


@pytest.mark.parametrize("step", [None, "existing-boundary"])
def test_initial_helper_error_retains_code_identity_and_step_without_receipt(setup, step):
    error = UnsafePathError("private path detail", step_id=step)

    def fail(request):
        raise error

    setup[1].run = fail
    with pytest.raises(UnsafePathError) as failed:
        install(setup)
    assert failed.value is error and failed.value.code == "unsafe_path"
    assert failed.value.step_id == (step or "helper-identity")
    assert not [e for e in setup[1].events if e[0] in {"write", "mkdir", "download"}]
    assert not setup[1].held


@pytest.mark.parametrize("condition", ["absent", "missing-node", "unhealthy", "wrong-owner", "unowned-port"])
def test_server_or_nodes_cannot_be_faked_installed(setup, condition):
    context = setup[1]
    if condition == "missing-node":
        context.health_objects = {"not-an-h3-node": {}}
    elif condition == "unhealthy":
        context.health_healthy = False
    elif condition == "wrong-owner":
        context.health_owner = "other-incarnation"
    elif condition == "unowned-port":
        context.server = "alien"
    else:
        original = context.run
        def absent(request):
            result = original(request)
            if "engine_execution" in " ".join(request.argv) and request.argv[-2] == "start":
                value = json.loads(result.stdout)
                value["facts"]["state"] = "absent"
                return replace(result, stdout=runner._json(value))
            return result
        context.run = absent
    with pytest.raises((EndpointOwnershipError, EndpointHealthError)):
        install(setup)
    assert context.files[ROOT + "/" + runner._RECEIPT]["status"] == "failed"


def test_status_absent_is_none_and_readonly_without_platform_module(setup, monkeypatch):
    monkeypatch.delitem(sys.modules, "astrid.core.generation.engine_platforms")
    assert status(setup) is None
    assert [e[0] for e in setup[1].events] == ["fact"]


@pytest.mark.parametrize("node,input_name", [
    ("LoadImage", "image"), ("LoadImageMask", "image"),
    ("LoadVideo", "video"), ("LoadVideo", "file"),
    ("VHS_LoadVideo", "video"), ("VHS_LoadVideo", "file"),
])
def test_status_tolerates_only_dynamic_file_choice_drift(setup, node, input_name):
    context = setup[1]
    context.health_objects[node] = {
        "input": {"required": {input_name: [["initial.png"], {"image_upload": True}]}},
        "output": ["IMAGE"],
    }
    receipt = install(setup)
    persisted = copy.deepcopy(context.files)
    context.events.clear()
    context.health_objects[node]["input"]["required"][input_name][0] = ["new.png", "other.png"]
    current = status(setup)
    assert current.status == "installed" and current.health.status == "healthy"
    assert current.health.required_nodes_present is True
    assert current.health.object_info_sha256 == receipt.health.object_info_sha256
    assert current.failed_step_id is None and context.files == persisted
    assert not [e for e in context.events if e[0] in {"write", "mkdir", "download", "service:start"}]

    context.health_objects[node]["output"] = ["MASK"]
    current = status(setup)
    assert (current.status, current.error_code, current.failed_step_id) == (
        "failed", "integrity_failed", "receipt-evidence")
    assert current.health.status == "unhealthy" and current.health.required_nodes_present is False
    assert context.files == persisted
    assert not [e for e in context.events if e[0] in {"write", "mkdir", "download", "service:start"}]


@pytest.mark.parametrize("code", ["PRIVATE_INPUT_SENTINEL", "a" * 10000, None])
def test_status_failure_does_not_serialize_unrecognized_error_codes(setup, monkeypatch, code):
    install(setup)
    error = IntegrityError("PRIVATE_INPUT_SENTINEL", step_id="/arbitrary/private/path")
    error.code = code
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(runner._Runner, "execute", fail)
    report = status(setup)
    assert report.failed_step_id == "source:comfyui" and report.error_code == "engine_error"
    assert "PRIVATE_INPUT_SENTINEL" not in runner._json(report)


@pytest.mark.parametrize("gate", ["helper-identity", "receipt-evidence"])
def test_status_failure_identifies_non_plan_verification_gates(setup, gate):
    install(setup)
    context = setup[1]
    persisted = copy.deepcopy(context.files)
    context.events.clear()
    if gate == "helper-identity":
        context.wrong_identity = True
    else:
        context.health_objects["new-node"] = {}
    report = status(setup)
    assert (report.status, report.error_code, report.failed_step_id) == ("failed", "integrity_failed", gate)
    assert context.files == persisted
    assert not [e for e in context.events if e[0] in {"write", "mkdir", "download", "service:start"}]


def test_status_reads_legacy_receipt_without_diagnostics(setup):
    receipt = install(setup)
    setup[1].files[ROOT + "/" + runner._RECEIPT].pop("failed_step_id")
    report = status(setup)
    assert report.status == "installed" and report.failed_step_id is None
    for field in ("receipt_id", "root", "recipe_sha256", "models_lock_sha256", "source_commits", "requirements_locks"):
        assert getattr(report, field) == getattr(receipt, field)


@pytest.mark.parametrize("field,value", [
    ("failed_step_id", "/arbitrary/private/path"), ("failed_step_id", "source:PRIVATE_INPUT_SENTINEL"),
    ("failed_step_id", ["health"]), ("failed_step_id", "health"),
])
def test_status_refuses_untrusted_or_inconsistent_receipt_diagnostics(setup, field, value):
    install(setup)
    raw = setup[1].files[ROOT + "/" + runner._RECEIPT]
    raw[field] = value
    with pytest.raises(IntegrityError):
        status(setup)


@pytest.mark.parametrize("field,value", [("schema", "wrong"), ("qualified", True), ("root", "/other"), ("unknown", True), ("steps", [])])
def test_invalid_receipt_cannot_be_admitted(setup, field, value):
    install(setup)
    setup[1].files[ROOT + "/" + runner._RECEIPT][field] = value
    with pytest.raises(IntegrityError):
        status(setup)


def test_import_and_planning_with_provider_and_ssh_imports_blocked():
    code = r'''
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'runpod_lifecycle','runpod','paramiko'}:
            raise AssertionError(fullname)
sys.meta_path.insert(0, Block())
from astrid.core.generation.engine_recipe import load_recipe
from astrid.core.generation.engine_runner import plan_install
p=plan_install(load_recipe('comfyui','linux-x86_64-cu130'),root='/uncreated/install',placement='local')
# Plan 32 T5h: the install is model-free, so the plan has no model steps.
assert {s.kind for s in p.steps} == {'source','environment','start','health'}
assert not any(m in sys.modules for m in ('runpod_lifecycle','runpod','paramiko'))
print(len(p.steps))
'''
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=20)
    assert result.returncode == 0, result.stderr
    recipe = recipes.load_recipe('comfyui', 'linux-x86_64-cu130')
    assert int(result.stdout.strip()) == len(recipe.sources) + len(recipe.environments) + 2


def test_helper_closure_uses_relative_paths_exact_bytes_and_no_install_root(setup):
    manifest = runner.helper_manifest(setup[1].recipe)
    assert all(not path.startswith("/") for path in manifest)
    assert "core/generation/model_downloader.py" in manifest
    assert "core/generation/engine_runner_target.py" in manifest
    assert setup[1].recipe.models_lock.path in manifest
    assert all(len(digest) == 64 for digest in manifest.values())
    assert ROOT not in runner._json(manifest)
    for path, digest in manifest.items():
        assert hashlib.sha256((recipes._ROOT / path).read_bytes()).hexdigest() == digest
    assert {"packs/vibecomfy/extensions/generation.engine/comfyui.yaml", "packs/runpod/recipe.linux-cu130.json",
            setup[1].recipe.pins.path} <= manifest.keys()


@pytest.mark.parametrize("path", ["core/generation/engine_runner.py", "core/generation/engine_runner_target.py",
                                  "core/generation/model_downloader.py",
                                  "packs/vibecomfy/extensions/generation.engine/comfyui.yaml"])
def test_helper_identity_changes_with_exact_code_or_index_bytes(setup, tmp_path, monkeypatch, path):
    recipe = setup[1].recipe
    original = runner.helper_manifest(recipe)
    for relative in original:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((recipes._ROOT / relative).read_bytes())
    monkeypatch.setattr(recipes, "_ROOT", tmp_path)
    assert runner.helper_manifest(recipe) == original
    changed = tmp_path / path
    changed.write_bytes(changed.read_bytes() + b"\n")
    current = runner.helper_manifest(recipe)
    assert current[path] != original[path]
    assert runner._sha(current) != runner._sha(original)


def _helper_payload(root, recipe=None, **extra):
    recipe = recipes.load_recipe("comfyui", PLATFORM) if recipe is None else recipe
    return {"schema": runner._HELPER_SCHEMA, "engine": recipe.engine, "platform": recipe.platform,
            "root": str(root), "recipe_sha256": recipes.recipe_digest(recipe),
            "helper_sha256": runner._sha(runner.helper_manifest(recipe)), **extra}


def _helper_sources(root, recipe):
    for source in recipe.sources:
        directory = root / source.relative_path
        directory.mkdir(parents=True, exist_ok=True)
        if source.name in {"comfyui", "videohelpersuite"}:
            (directory / "requirements.txt").write_text("demo==1.0\n")
        if source.name == "vibecomfy":
            (directory / "pyproject.toml").write_text('[build-system]\nrequires = ["setuptools==80.9.0", "wheel==0.46.3"]\n')


HASH_LOCK = "demo==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"


def test_linux_lock_inputs_do_not_require_nonexistent_h3_requirements(resolve_once, tmp_path):
    recipe = recipes.load_recipe("comfyui", PLATFORM)
    _helper_sources(tmp_path, recipe)
    h3 = next(source for source in recipe.sources if source.name == "h3-motion-context")
    h3_requirements = tmp_path / h3.relative_path / "requirements.txt"
    # H3 361624fb has no runtime requirements file. Its torch, numpy,
    # safetensors, torchaudio and imageio-ffmpeg dependencies are supplied by
    # ComfyUI/VHS and the final wheels; custom_nodes.lock declares safetensors.
    assert not h3_requirements.exists()
    payload = _helper_payload(tmp_path, environment="comfy")
    assert target.dispatch("lock-inputs", payload)["facts"] == {"lock_exists": False}
    assert [(path.source, path.path) for path in recipe.environments[0].requirements.input_paths] == [
        ("comfyui", "requirements.txt"),
        ("vibecomfy", "pyproject.toml"),
        ("videohelpersuite", "requirements.txt"),
    ]
    assert not h3_requirements.exists()
    # Removing the erroneous input must not relax validation of real inputs.
    comfy = next(source for source in recipe.sources if source.name == "comfyui")
    (tmp_path / comfy.relative_path / "requirements.txt").unlink()
    with pytest.raises(IntegrityError, match="requirements input is absent"):
        target.dispatch("lock-inputs", payload)


@pytest.mark.parametrize("declared", [False, True])
def test_target_helper_persists_and_reuses_bound_hash_lock_before_install(resolve_once, tmp_path, monkeypatch, declared):
    recipe = recipes.load_recipe("comfyui", PLATFORM)
    if not declared:
        recipe = replace(recipe, environments=(replace(recipe.environments[0], source_installs=()),))
        monkeypatch.setattr(recipes, "load_recipe", lambda *_: recipe)
    _helper_sources(tmp_path, recipe)
    payload = _helper_payload(tmp_path, environment="comfy")
    inputs = target.dispatch("lock-inputs", payload)
    assert inputs["facts"] == {"lock_exists": False}
    directory = tmp_path / ".astrid-engine/environments/comfy"
    generated = (directory / "requirements.in").read_text()
    assert "setuptools==80.9.0" in generated
    assert "torch @ https://" in generated
    assert "comfy-kitchen==0.2.35" in generated
    (directory / "requirements.lock.tmp").write_text(HASH_LOCK)
    committed = target.dispatch("commit-lock", payload)
    expected = hashlib.sha256(HASH_LOCK.encode()).hexdigest()
    assert committed["facts"] == {"requirements_lock_sha256": expected}
    assert (directory / "requirements.lock").read_text() == HASH_LOCK
    metadata = json.loads((directory / "lock.json").read_text())
    assert metadata["recipe_sha256"] == payload["recipe_sha256"]
    assert metadata["helper_sha256"] == payload["helper_sha256"]
    assert metadata["requirements_lock_sha256"] == expected
    original_mtime = (directory / "requirements.lock").stat().st_mtime_ns
    assert target.dispatch("lock-inputs", payload)["facts"] == {"lock_exists": True}
    assert (directory / "requirements.lock").stat().st_mtime_ns == original_mtime
    target.dispatch("install-locks", payload)
    assert "demo==1.0" in (directory / "support.lock").read_text()
    wheels = (directory / "wheels.lock").read_text()
    assert all(w.sha256 in wheels for w in recipe.environments[0].final_wheels)
    spec = target.dispatch("environment-spec", payload)["facts"]
    assert spec["requirements_lock_sha256"] == expected
    assert spec["expected"]["sources"] == ([{"path": str(tmp_path / "runtime/vibecomfy"),
                                               "modules": ["vibecomfy"]}] if declared else [])
    assert spec["expected"]["python"] == "3.12.10"


@pytest.fixture
def pip_free_model_environment(tmp_path):
    """A disposable probe subject, populated only from real local packaging files.

    Check every hashed RECORD entry before copying; no pip, installer subprocess,
    index access, parser substitute, or shared-environment write is involved.
    """
    root = tmp_path / "model-venv"
    venv.EnvBuilder(with_pip=False).create(root)
    python = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    result = subprocess.run([str(python), "-I", "-c",
                             "import sysconfig; print(sysconfig.get_path('purelib'))"],
                            capture_output=True, text=True, timeout=20, check=True)
    site_packages = Path(result.stdout.strip())
    distribution = metadata.distribution("packaging")
    wheel = metadata.distribution("wheel")
    assert wheel.version == "0.46.3"
    assert "packaging >= 24.0" in wheel.requires
    files = {}
    try:
        assert distribution.files
        for relative in distribution.files:
            assert not relative.is_absolute() and ".." not in relative.parts
            assert relative.parts[0] in {"packaging", f"packaging-{distribution.version}.dist-info"}
            if "__pycache__" in relative.parts and relative.suffix == ".pyc" and relative.hash is None:
                continue
            data = distribution.locate_file(relative).read_bytes()
            if relative.hash:
                assert relative.hash.mode == "sha256"
                assert base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=") == relative.hash.value
                assert len(data) == relative.size
            else:
                assert relative.name == "RECORD"
            destination = site_packages / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            files[str(relative)] = hashlib.sha256(data).hexdigest()
        evidence = {"packaging_version": distribution.version, "files_sha256": files,
                    "wheel_version": wheel.version, "wheel_requires_dist": wheel.requires,
                    "wheel_metadata_sha256": hashlib.sha256(wheel.read_text("METADATA").encode()).hexdigest()}
        print("PIP_FREE_FIXTURE " + json.dumps(evidence, sort_keys=True))
        yield python, distribution.version
    finally:
        shutil.rmtree(root)


def test_actual_environment_probe_succeeds_with_packaging_and_no_pip(pip_free_model_environment):
    python, version = pip_free_model_environment
    # -I excludes the checkout, PYTHONPATH, user site, and the test interpreter's
    # installed distributions. Both import discovery and distribution lookup
    # must demonstrate that pip is absent in this selected model environment.
    identity = subprocess.run([str(python), "-I", "-c", """
import importlib.metadata as metadata, importlib.util, json, sys
from pathlib import Path
import packaging
assert sys.prefix != sys.base_prefix
assert importlib.util.find_spec('pip') is None
try:
    metadata.distribution('pip')
except metadata.PackageNotFoundError:
    pass
else:
    raise AssertionError('pip metadata present')
assert Path(packaging.__file__).is_relative_to(sys.prefix)
print(json.dumps({'python': sys.version.split()[0], 'packaging': metadata.version('packaging')}))
"""], capture_output=True, text=True, timeout=20, check=True)
    facts = json.loads(identity.stdout)
    assert facts["packaging"] == version
    pip = subprocess.run([str(python), "-I", "-m", "pip", "--version"],
                         capture_output=True, text=True, timeout=20, check=False)
    assert pip.returncode != 0 and "No module named pip" in pip.stderr
    expected = {"python": facts["python"], "requirements": [f"packaging=={version}"],
                "constraints": ["packaging>=24.0"], "wheels": {}, "sources": [], "cuda": "13.0"}
    result = subprocess.run([str(python), "-I", "-c", target._ENV_PROBE, runner._json(expected)],
                            capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"valid": True}
    # Exercise the real public parser's version and marker handling as well as
    # the interpreter check; invalid evidence must never validate successfully.
    expected["requirements"].append('absent-probe-dependency==1; python_version < "2"')
    for field, bad in [("python", "0.0.0"), ("requirements", ["packaging==0"]),
                       ("constraints", ["packaging<24.0"])]:
        invalid = {**expected, field: bad}
        refused = subprocess.run([str(python), "-I", "-c", target._ENV_PROBE, runner._json(invalid)],
                                 capture_output=True, text=True, timeout=20, check=False)
        assert refused.returncode != 0
        assert '"valid": true' not in refused.stdout
    valid_marker = subprocess.run([str(python), "-I", "-c", target._ENV_PROBE, runner._json(expected)],
                                 capture_output=True, text=True, timeout=20, check=False)
    assert valid_marker.returncode == 0, valid_marker.stderr
    print("PIP_FREE_PROBE " + json.dumps({"identity": facts, "pip_exit": pip.returncode,
                                         "pip_stderr": pip.stderr.strip(), "expected": expected,
                                         "probe_exit": result.returncode, "probe_stdout": result.stdout.strip(),
                                         "invalid_evidence_cases_refused": 3}, sort_keys=True))


WHEEL_URL = "https://example.invalid/fakewheel-1.0-py3-none-any.whl"
WHEEL_SHA = "d" * 64


@pytest.fixture
def receipt_environment(tmp_path):
    """A disposable venv holding a fake hash-pinned wheel plus the real packaging library."""
    root = tmp_path / "receipt-venv"
    venv.EnvBuilder(with_pip=False).create(root)
    python = root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    purelib = Path(subprocess.run([str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                  capture_output=True, text=True, timeout=20, check=True).stdout.strip())
    packaging_dist = metadata.distribution("packaging")
    for relative in packaging_dist.files:
        destination = purelib / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(packaging_dist.locate_file(relative).read_bytes())
    (purelib / "fakewheel").mkdir()
    (purelib / "fakewheel/__init__.py").write_text('__version__ = "1.0"\n')
    info = purelib / "fakewheel-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: fakewheel\nVersion: 1.0\n")
    (info / "direct_url.json").write_text(json.dumps({"url": WHEEL_URL, "archive_info": {}}))
    (info / "RECORD").write_text("fakewheel/__init__.py,,\nfakewheel-1.0.dist-info/METADATA,,\nfakewheel-1.0.dist-info/RECORD,,\n")
    receipt = tmp_path / "install-receipt-fakewheel.json"
    expected = {"python": subprocess.run([str(python), "-I", "-c", "import sys; print(sys.version.split()[0])"],
                                         capture_output=True, text=True, check=True).stdout.strip(),
                "requirements": [], "constraints": [], "sources": [], "cuda": "13.0",
                "wheels": {"fakewheel": {"version": "1.0", "url": WHEEL_URL, "sha256": WHEEL_SHA,
                                         "receipt": str(receipt)}}}

    def write_receipt(**override):
        digest = subprocess.run([str(python), "-I", "-c", target._RECEIPT_SCRIPT,
                                 runner._json({"wheels": {"fakewheel": {"version": "1.0"}}})],
                                capture_output=True, text=True, timeout=20, check=True)
        value = {"name": "fakewheel", "version": "1.0", "url": WHEEL_URL, "sha256": WHEEL_SHA,
                 "record_sha256": json.loads(digest.stdout)["record_sha256"]["fakewheel"], **override}
        receipt.write_text(json.dumps(value))

    def probe():
        return subprocess.run([str(python), "-I", "-c", target._ENV_PROBE, runner._json(expected)],
                              capture_output=True, text=True, timeout=20, check=False).returncode == 0

    yield types.SimpleNamespace(info=info, receipt=receipt, write_receipt=write_receipt, probe=probe)


def test_probe_accepts_uv_empty_archive_info_with_matching_receipt(receipt_environment):
    receipt_environment.write_receipt()
    assert receipt_environment.probe()


def test_probe_refuses_missing_receipt(receipt_environment):
    assert not receipt_environment.probe()


def test_probe_refuses_unreadable_receipt(receipt_environment):
    receipt_environment.receipt.write_text("{not json")
    assert not receipt_environment.probe()


def test_probe_refuses_receipt_with_wrong_sha256(receipt_environment):
    receipt_environment.write_receipt(sha256="e" * 64)
    assert not receipt_environment.probe()


def test_probe_refuses_record_modified_after_receipt(receipt_environment):
    receipt_environment.write_receipt()
    with (receipt_environment.info / "RECORD").open("a") as record:
        record.write("fakewheel/evil.py,,\n")
    assert not receipt_environment.probe()


def test_probe_still_accepts_pip_style_archive_hash_without_receipt(receipt_environment):
    (receipt_environment.info / "direct_url.json").write_text(json.dumps(
        {"url": WHEEL_URL, "archive_info": {"hashes": {"sha256": WHEEL_SHA}}}))
    assert receipt_environment.probe()


def test_target_lock_commit_interruption_reuses_exact_candidate(resolve_once, tmp_path, monkeypatch):
    recipe = recipes.load_recipe("comfyui", PLATFORM)
    _helper_sources(tmp_path, recipe)
    payload = _helper_payload(tmp_path, environment="comfy")
    target.dispatch("lock-inputs", payload)
    directory = tmp_path / ".astrid-engine/environments/comfy"
    (directory / "requirements.lock.tmp").write_text(HASH_LOCK)
    atomic = target._atomic
    def interrupted(path, value):
        if path.name == "requirements.lock":
            raise OSError("interrupted promotion")
        atomic(path, value)
    with monkeypatch.context() as patch:
        patch.setattr(target, "_atomic", interrupted)
        with pytest.raises(OSError):
            target.dispatch("commit-lock", payload)
    assert (directory / "lock.json").exists()
    assert not (directory / "requirements.lock").exists()
    result = target.dispatch("lock-inputs", payload)
    assert result["facts"] == {"lock_exists": True}
    assert (directory / "requirements.lock").read_text() == HASH_LOCK


@pytest.mark.parametrize("operation", ["lock-inputs", "commit-lock"])
def test_target_stale_identity_refuses_interrupted_lock_without_mutation(
        resolve_once, tmp_path, operation):
    from astrid.core.generation.engine_contracts import EngineReinstallRequired

    recipe = recipes.load_recipe("comfyui", PLATFORM)
    _helper_sources(tmp_path, recipe)
    payload = _helper_payload(tmp_path, environment="comfy")
    target.dispatch("lock-inputs", payload)
    directory = tmp_path / ".astrid-engine/environments/comfy"
    (directory / "requirements.lock.tmp").write_text(HASH_LOCK)
    target.dispatch("commit-lock", payload)

    metadata_path = directory / "lock.json"
    stale_metadata = json.loads(metadata_path.read_text())
    stale_metadata["helper_sha256"] = "0" * 64
    metadata_path.write_text(runner._json(stale_metadata))
    (directory / "requirements.lock").rename(directory / "requirements.lock.tmp")
    before = {path.relative_to(directory).as_posix(): path.read_bytes()
              for path in directory.rglob("*") if path.is_file()}

    with pytest.raises(EngineReinstallRequired) as refused:
        target.dispatch(operation, payload)

    assert refused.value.code == "engine_reinstall_required"
    assert refused.value.environment == "comfy"
    from astrid.core.generation.engine_execution import create_local_backend
    (tmp_path / ".astrid-engine").chmod(0o700)
    deadline = time.monotonic() + 5
    context, adapter = create_local_backend(engine=recipe.engine, platform=PLATFORM, placement="local",
        root=str(tmp_path), deadline=deadline)
    with pytest.raises(EngineReinstallRequired):
        runner.install_engine(runner.plan_install(recipe, root=str(tmp_path), placement="local"),
            context=context, adapter=adapter, deadline=deadline)
    assert not (tmp_path / ".astrid-engine/session").exists()
    assert not (tmp_path / ".astrid-engine/install.json").exists()
    after = {path.relative_to(directory).as_posix(): path.read_bytes()
             for path in directory.rglob("*") if path.is_file()}
    assert after == before
    assert not (directory / "requirements.lock").exists()
    assert (directory / "requirements.lock.tmp").read_bytes() == HASH_LOCK.encode()


@pytest.mark.parametrize("bad", ["demo==1.0\n", "demo>=1.0 --hash=sha256:" + "a" * 64,
                                 "--index-url https://unreviewed.invalid\n", "", "demo==1.0 \\"])
def test_target_refuses_unhashed_or_unresolved_locks(resolve_once, tmp_path, bad):
    recipe = recipes.load_recipe("comfyui", PLATFORM)
    _helper_sources(tmp_path, recipe)
    payload = _helper_payload(tmp_path, environment="comfy")
    target.dispatch("lock-inputs", payload)
    directory = tmp_path / ".astrid-engine/environments/comfy"
    (directory / "requirements.lock.tmp").write_text(bad)
    with pytest.raises(IntegrityError):
        target.dispatch("commit-lock", payload)
    assert not (directory / "requirements.lock").exists()


def test_target_refuses_modified_or_unbound_persisted_lock(resolve_once, tmp_path):
    recipe = recipes.load_recipe("comfyui", PLATFORM)
    _helper_sources(tmp_path, recipe)
    payload = _helper_payload(tmp_path, environment="comfy")
    target.dispatch("lock-inputs", payload)
    directory = tmp_path / ".astrid-engine/environments/comfy"
    (directory / "requirements.lock").write_text(HASH_LOCK)
    with pytest.raises(IntegrityError):
        target.dispatch("lock-inputs", payload)
    (directory / "requirements.lock").unlink()
    (directory / "requirements.lock.tmp").write_text(HASH_LOCK)
    target.dispatch("commit-lock", payload)
    (directory / "requirements.lock").write_text(HASH_LOCK.replace("demo", "changed"))
    with pytest.raises(IntegrityError):
        target.dispatch("install-locks", payload)


def test_target_helper_rejects_unknown_fields_wrong_code_and_symlink_escape(tmp_path):
    payload = _helper_payload(tmp_path)
    with pytest.raises(RecipeValidationError):
        target.dispatch("identity", {**payload, "surprise": True})
    with pytest.raises(IntegrityError):
        target.dispatch("identity", {**payload, "helper_sha256": "0" * 64})
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "runtime").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        target.dispatch("models-config", {**payload, "models_root": None})
    assert list(outside.iterdir()) == []


def test_target_models_config_is_deterministic_and_loopback_launch_is_recipe_bound(tmp_path, setup):
    payload = _helper_payload(tmp_path)
    assert target.dispatch("models-config", {**payload, "models_root": None})["facts"] == {"written": True}
    path = tmp_path / "runtime/extra_model_paths.yaml"
    before = path.read_bytes()
    target.dispatch("models-config", {**payload, "models_root": None})
    assert path.read_bytes() == before
    assert json.loads(before)["astrid_engine"]["base_path"] == str(tmp_path / "models")
    launch = setup[2].start_request(setup[1].recipe, ROOT, deadline=setup[3])
    assert "127.0.0.1" in launch.argv and "0.0.0.0" not in launch.argv
    assert "CUDA_VISIBLE_DEVICES" not in launch.env


def test_launch_snapshots_ambient_visibility_and_explicitly_clears_unset(setup, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0")
    first = runner._Runner(*setup)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "1")
    second = runner._Runner(*setup)
    assert first.launch_spec()["env"]["CUDA_VISIBLE_DEVICES"] == "0"
    assert second.launch_spec()["env"]["CUDA_VISIBLE_DEVICES"] == "1"
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES")
    assert runner._Runner(*setup).launch_spec()["env"]["CUDA_VISIBLE_DEVICES"] is None
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
    assert runner._Runner(*setup).launch_spec()["env"]["CUDA_VISIBLE_DEVICES"] == ""


def test_helper_error_envelope_preserves_stable_error_without_raw_exception(tmp_path, capsys):
    payload = _helper_payload(tmp_path)
    assert target.main(["identity", runner._json({**payload, "helper_sha256": "0" * 64})]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    value = json.loads(captured.out)
    assert value == {"schema": runner._HELPER_SCHEMA, "operation": "identity", "facts": {"error_code": "integrity_failed"}}


def test_occupied_source_is_not_initialized_or_overwritten(setup):
    context = setup[1]
    context.directories.add(ROOT + "/runtime/ComfyUI")
    original = context.run
    def run(request):
        if request.argv[-2:-1] == ("empty-source",):
            return context.result(runner._json({"schema": runner._HELPER_SCHEMA, "operation": "empty-source",
                                               "facts": {"error_code": "integrity_failed"}}))
        return original(request)
    context.run = run
    with pytest.raises(IntegrityError):
        install(setup)
    assert not context.sources


def test_dirty_source_failure_records_reason_and_bounded_porcelain(setup):
    context = setup[1]
    original = context.run
    dirty = "\n".join(f"?? comfyui-qwentts/file{i}.py" for i in range(25))
    def run(request):
        if "status" in request.argv:
            return context.result(dirty)
        return original(request)
    context.run = run
    with pytest.raises(IntegrityError) as caught:
        install(setup)
    assert caught.value.code == "integrity_failed" and caught.value.step_id == "source:comfyui"
    assert caught.value.diagnostics["reason"] == "source contains uncommitted content"
    assert caught.value.diagnostics["porcelain"].splitlines() == [f"?? comfyui-qwentts/file{i}.py" for i in range(20)]
    receipt = context.files[ROOT + "/" + runner._RECEIPT]
    step = next(s for s in receipt["steps"] if s["id"] == "source:comfyui")
    assert step["facts"]["reason"] == "source contains uncommitted content"
    assert step["facts"]["porcelain"].count("\n") == 19
    assert "file24" not in step["facts"]["porcelain"]


def test_custody_receipt_refuses_reexecution_until_exact_recovery(setup):
    context = setup[1]
    error = CustodyUnresolved("reconcile job", recovery_handle="/private/recovery/exact-incarnation")
    context.fail = ("git", error)
    with pytest.raises(CustodyUnresolved):
        install(setup)
    context.fail = None
    context.events.clear()
    with pytest.raises(CustodyUnresolved) as caught:
        install(setup)
    assert caught.value.recovery_handle == error.recovery_handle
    assert not [e for e in context.events if e[0] == "argv" and e[1][0] == "git"]
    assert not [e for e in context.events if e[0] in {"download", "write", "mkdir", "service:start"}]


def test_install_lock_is_held_until_final_receipt_commit(setup):
    plan, context, adapter, deadline = setup
    original = context.write_json_atomic
    commits = []
    def write(path, value, *, deadline):
        if path.endswith("/install.json") and value["status"] == "installed":
            assert context.held
            competitor = FakeContext()
            competitor.deadline = deadline
            @contextmanager
            def locked(root, *, deadline):
                raise CustodyUnresolved("exact live lock holder", recovery_handle="/lock/holder-incarnation")
                yield
            competitor.acquire_install_lock = locked
            with pytest.raises(CustodyUnresolved):
                runner.install_engine(plan, context=competitor, adapter=adapter, deadline=deadline)
            assert not competitor.events
            commits.append(value["status"])
        original(path, value, deadline=deadline)
    context.write_json_atomic = write
    assert install(setup).status == "installed"
    assert commits == ["installed"] and not context.held


def test_environment_repair_reuses_lock_without_resolving_again(setup):
    install(setup)
    setup[1].environments.clear()
    setup[1].events.clear()
    assert install(setup).status == "installed"
    commands = [e[1] for e in setup[1].events if e[0] == "argv"]
    assert not any("compile" in command for command in commands)
    assert any("sync" in command for command in commands)


def test_failed_install_status_is_returned_without_repair(setup):
    setup[1].fail = ("git", CommandResult(1, "", "", False, 0, "job1", False, False))
    with pytest.raises(CommandFailed):
        install(setup)
    setup[1].events.clear()
    assert status(setup).status == "failed"
    assert [event[0] for event in setup[1].events] == ["fact", "read"]


def test_nonzero_diagnostics_are_bounded_and_secret_free(setup):
    setup[1].fail = ("git", CommandResult(2, "x" * 500000, "Bearer secret", False, 0, "job1", True, True))
    with pytest.raises(CommandFailed) as caught:
        install(setup)
    assert len(caught.value.message) < 200
    assert "secret" not in runner._json(setup[1].files[ROOT + "/" + runner._RECEIPT])


def test_status_absent_server_does_not_start_or_repair(setup):
    install(setup)
    setup[1].server = None
    setup[1].events.clear()
    assert status(setup).status == "failed"
    assert not any(e[0] in {"service:start", "write", "download", "mkdir"} for e in setup[1].events)


def test_status_custody_unresolved_preserves_exact_error(setup):
    install(setup)
    error = CustodyUnresolved("exact custody", recovery_handle="/recovery/exact-status-job")
    setup[1].fail = ("run", error)
    with pytest.raises(CustodyUnresolved) as caught:
        status(setup)
    assert caught.value is error


def test_final_revalidation_refuses_sources_dirtied_by_build(setup):
    context = setup[1]
    original = context.run
    def run(request):
        result = original(request)
        if "engine_execution" in " ".join(request.argv) and request.argv[-2] == "health":
            context.sources[ROOT + "/runtime/ComfyUI"]["dirty"] = " M main.py"
        return result
    context.run = run
    with pytest.raises(IntegrityError):
        install(setup)
    assert context.files[ROOT + "/" + runner._RECEIPT]["status"] == "failed"


def test_named_environments_have_distinct_lock_paths_and_complete_dependencies(setup, monkeypatch):
    plan, context, adapter, deadline = setup
    primary = context.recipe.environments[0]
    extra = replace(primary, name="second", relative_path="runtime/second-venv", node_lock=None)
    recipe = replace(context.recipe, environments=(primary, extra))
    original_load = recipes.load_recipe
    monkeypatch.setattr(recipes, "load_recipe", lambda engine, platform: recipe if engine == recipe.engine else original_load(engine, platform))
    context.recipe = recipe
    plan = runner.plan_install(recipe, root=ROOT, placement="local")
    receipt = runner.install_engine(plan, context=context, adapter=adapter, deadline=deadline)
    assert receipt.requirements_locks == {"comfy": "b" * 64, "second": "b" * 64}
    for model_step in (s for s in plan.steps if s.kind == "model"):
        assert model_step.depends_on == ("environment:comfy", "environment:second")
    assert ROOT + "/.astrid-engine/environments/comfy/requirements.lock" in context.files
    assert ROOT + "/.astrid-engine/environments/second/requirements.lock" in context.files


def test_target_node_lock_checks_canonical_commits_after_hash(tmp_path, monkeypatch):
    recipe = recipes.load_recipe("comfyui", PLATFORM)
    comfy = next(s for s in recipe.sources if s.name == "comfyui")
    prefix = comfy.relative_path + "/custom_nodes/"
    text = "\n".join(f'[nodepacks."{s.relative_path[len(prefix):]}"]\ncommit = "{s.pin.revision}"'
                     for s in recipe.sources if s.relative_path.startswith(prefix))
    source = next(s for s in recipe.sources if s.name == "vibecomfy")
    directory = tmp_path / source.relative_path
    directory.mkdir(parents=True)
    lock = directory / "custom_nodes.lock"
    lock.write_text(text)
    environment = recipe.environments[0]
    modified = replace(recipe, environments=(replace(environment, node_lock=replace(
        environment.node_lock, sha256=hashlib.sha256(text.encode()).hexdigest())),))
    monkeypatch.setattr(recipes, "load_recipe", lambda *_: modified)
    payload = _helper_payload(tmp_path, modified, environment="comfy")
    assert target.dispatch("node-lock", payload)["facts"] == {"valid": True}
    bad_text = text.replace(next(s.pin.revision for s in recipe.sources if s.relative_path.startswith(prefix)), "0" * 40)
    lock.write_text(bad_text)
    modified = replace(recipe, environments=(replace(environment, node_lock=replace(
        environment.node_lock, sha256=hashlib.sha256(bad_text.encode()).hexdigest())),))
    payload = _helper_payload(tmp_path, modified, environment="comfy")
    with pytest.raises(IntegrityError, match="canonical source"):
        target.dispatch("node-lock", payload)


def test_packaged_requirements_follow_same_hashed_lock_path(tmp_path, monkeypatch):
    from astrid.core.generation.engine_contracts import DataRef, RequirementsSpec
    recipe = recipes.load_recipe("comfyui", PLATFORM)
    lock_bytes = HASH_LOCK.encode()
    ref = DataRef("packs/test/requirements.lock", hashlib.sha256(lock_bytes).hexdigest())
    req = RequirementsSpec("packaged", ref, (), (), None)
    recipe = replace(recipe, environments=(replace(recipe.environments[0], requirements=req),))
    read_ref = recipes._read_ref
    monkeypatch.setattr(recipes, "load_recipe", lambda *_: recipe)
    monkeypatch.setattr(recipes, "_read_ref", lambda value, **kwargs: (tmp_path / "packaged.lock", lock_bytes) if value == ref else read_ref(value, **kwargs))
    payload = _helper_payload(tmp_path, recipe, environment="comfy")
    assert target.dispatch("lock-inputs", payload)["facts"] == {"lock_exists": False}
    assert target.dispatch("commit-lock", payload)["facts"] == {"requirements_lock_sha256": ref.sha256}
    target.dispatch("install-locks", payload)
    assert (tmp_path / ".astrid-engine/environments/comfy/requirements.lock").read_bytes() == lock_bytes


LINUX_LOCK_PATH = "packs/runpod/requirements.linux-cu130.lock"
LINUX_LOCK_SHA256 = "73589f81c96f487b9ed39f45c9835e47a10add4ba20617b58da8c9072090e51b"


def test_shipped_linux_lock_is_the_exact_packaged_hash_bearing_resolution():
    recipe = recipes.load_recipe("comfyui", PLATFORM)
    requirements = recipe.environments[0].requirements
    assert (requirements.mode, requirements.input_paths, requirements.constraints, requirements.exclude_newer) == (
        "packaged", (), (), None)
    ref = requirements.packaged_lock
    assert (ref.path, ref.sha256) == (LINUX_LOCK_PATH, LINUX_LOCK_SHA256)
    path = Path(recipes._ROOT) / LINUX_LOCK_PATH
    assert hashlib.sha256(path.read_bytes()).hexdigest() == LINUX_LOCK_SHA256
    entries = target._entries(path.read_text(encoding="utf-8"))
    by_name = {re.match(r"[A-Za-z0-9._-]+", row[0])[0].lower(): row for row in entries}
    assert len(by_name) == len(entries) == 183
    for wheel in recipe.environments[0].final_wheels:
        requirement, hashes, _ = by_name[wheel.name]
        assert requirement == f"{wheel.name} @ {wheel.url}"
        assert hashes == [wheel.sha256]
    for pin in ("comfy-kitchen==0.2.35", "comfy-aimdo==0.5.5", "opencv-python==5.0.0.93"):
        name, version = pin.split("==")
        assert by_name[name][0] == pin
    # pip is not declared by the canonical source inputs for this closure.
    assert "pip" not in by_name
    # The reviewed index digest binds the exact recipe bytes that name this lock.
    entry = next(e for e in recipes.load_recipe_index().entries if (e.engine, e.platform) == ("comfyui", PLATFORM))
    assert hashlib.sha256((Path(recipes._ROOT) / entry.recipe.path).read_bytes()).hexdigest() == entry.recipe.sha256


def test_fresh_root_selects_exact_shipped_lock_bytes_without_resolution(tmp_path, monkeypatch):
    recipe = recipes.load_recipe("comfyui", PLATFORM)
    shipped = (Path(recipes._ROOT) / LINUX_LOCK_PATH).read_bytes()
    payload = _helper_payload(tmp_path, recipe, environment="comfy")
    directory = tmp_path / ".astrid-engine/environments/comfy"
    # A fresh root has no source checkouts or requirement inputs for a resolver to read.
    assert not (tmp_path / "runtime").exists() and not directory.exists()

    def forbidden(*args, **kwargs):
        raise AssertionError("fresh packaged-lock selection must not run a resolver or any child process")
    for name in ("run", "Popen", "check_call", "check_output", "call"):
        monkeypatch.setattr(subprocess, name, forbidden)
    monkeypatch.setattr(target.os, "system", forbidden)

    assert target.dispatch("lock-inputs", payload)["facts"] == {"lock_exists": False}
    assert (directory / "requirements.lock.tmp").read_bytes() == shipped
    assert not (directory / "requirements.in").exists()
    committed = target.dispatch("commit-lock", payload)
    assert committed["facts"] == {"requirements_lock_sha256": LINUX_LOCK_SHA256}
    assert (directory / "requirements.lock").read_bytes() == shipped
    metadata_value = json.loads((directory / "lock.json").read_text())
    assert metadata_value["requirements_lock_sha256"] == LINUX_LOCK_SHA256
    assert metadata_value["recipe_sha256"] == payload["recipe_sha256"]
    assert target.dispatch("lock-inputs", payload)["facts"] == {"lock_exists": True}
    target.dispatch("install-locks", payload)
    support = (directory / "support.lock").read_text()
    wheels = (directory / "wheels.lock").read_text()
    assert "torch @" not in support and "torchvision @" not in support and "torchaudio @" not in support
    assert "comfy-kitchen==0.2.35" in support and "opencv-python==5.0.0.93" in support
    assert all(w.sha256 in wheels and w.url in wheels for w in recipe.environments[0].final_wheels)
    # An altered shipped lock is refused rather than re-resolved.
    (directory / "requirements.lock").write_bytes(shipped + b"# tamper\n")
    with pytest.raises(IntegrityError):
        target.dispatch("install-locks", payload)


def test_runner_installs_packaged_linux_lock_with_no_compile_step(setup):
    result = install(setup)
    assert result.status == "installed"
    commands = [e[1] for e in setup[1].events if e[0] == "argv"]
    assert not any("compile" in command for command in commands)
    assert any("commit-lock" in command for command in commands)
    assert result.requirements_locks == {"comfy": "b" * 64}


def test_h3_packaged_sync_allows_only_three_source_builds_and_uses_support_lock(setup, tmp_path):
    result = install(setup)
    assert result.status == "installed"
    commands = [event[1] for event in setup[1].events if event[0] == "argv"]
    support = ROOT + "/.astrid-engine/environments/comfy/support.lock"
    sync = next(command for command in commands if command[:4] == (
        ROOT + "/runtime/bootstrap/bin/uv", "--no-config", "pip", "sync"))
    assert sync == (
        ROOT + "/runtime/bootstrap/bin/uv", "--no-config", "pip", "sync",
        "--python", ROOT + "/runtime/venv/bin/python",
        "--index-url", "https://pypi.org/simple", "--require-hashes",
        "--only-binary", ":all:", "--no-binary", "openai-whisper",
        "--no-binary", "argbind", "--no-binary", "randomname",
        "--build-constraints", support, support)
    recipe = setup[1].recipe
    assert (recipe.engine, recipe.platform, recipe.environments[0].requirements.mode) == (
        "comfyui", PLATFORM, "packaged")
    sync_index = commands.index(sync)
    check = next(i for i, command in enumerate(commands) if "check" in command)
    source_installs = [i for i, command in enumerate(commands)
                       if "--no-deps" in command and "--no-build-isolation" in command]
    final_wheels = next(i for i, command in enumerate(commands)
                        if "-r" in command and command[command.index("-r") + 1].endswith("/wheels.lock"))
    assert sync_index < check
    assert all(sync_index < i < final_wheels < check for i in source_installs)
    assert sync_index < final_wheels < check

    payload = _helper_payload(tmp_path, recipe, environment="comfy")
    target.dispatch("lock-inputs", payload)
    target.dispatch("commit-lock", payload)
    target.dispatch("install-locks", payload)
    support_rows = (tmp_path / ".astrid-engine/environments/comfy/support.lock").read_text().splitlines()
    expected_sources = {
        "openai-whisper": "https://files.pythonhosted.org/packages/35/8e/d36f8880bcf18ec026a55807d02fe4c7357da9f25aebd92f85178000c0dc/openai_whisper-20250625.tar.gz#sha256=37a91a3921809d9f44748ffc73c0a55c9f366c85a3ef5c2ae0cc09540432eb96",
        "argbind": "https://files.pythonhosted.org/packages/8e/25/4422cf7777c0cedd2fbcbdf8face786be76186e6d2e7b66f5f37e266b604/argbind-0.3.9.tar.gz#sha256=1b159c04af56858a91d59c7a47bc9ea39d96adfce1d7fcfa38050d7ac9815745",
        "randomname": "https://files.pythonhosted.org/packages/e8/c2/525e9e9b458c3ca493d9bd0871f3ed9b51446d26fe82d462494de188f848/randomname-0.2.1.tar.gz#sha256=b79b98302ba4479164b0a4f87995b7bebbd1d91012aeda483341e3e58ace520e",
    }
    for expected_name, expected_artifact in expected_sources.items():
        expected_url, digest = expected_artifact.split("#sha256=", 1)
        expected_hash = "--hash=sha256:" + digest
        assert any(row.startswith(expected_name + " @ " + expected_url)
                   and expected_hash in row for row in support_rows)
    setuptools_url = (
        "https://files.pythonhosted.org/packages/95/9c/c510029fc6ef33a6275cd2c5d3cecd6613dfd6aa401d57c54f1c18852ccf/"
        "setuptools-84.0.0-py3-none-any.whl#sha256=51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670"
    )
    setuptools_hash = "--hash=sha256:51a52592b3b99e102b609654876bd65f19f999935166d1352678931132b0c670"
    assert any(setuptools_url in row and setuptools_hash in row for row in support_rows)


@pytest.mark.parametrize("change", ["engine", "platform", "mode"])
def test_three_source_build_policy_does_not_leak_to_other_syncs(setup, monkeypatch, change):
    _plan, context, adapter, deadline = setup
    recipe = context.recipe
    if change == "engine":
        recipe = replace(recipe, engine="other-engine")
    elif change == "platform":
        recipe = replace(recipe, platform="linux-x86_64-cu128")
        context.platform = recipe.platform
        adapter.key = recipe.platform
    else:
        # Preserve a valid resolve-once contract: mode requires source inputs
        # and an exact cutoff. Changing only the mode creates exclude_newer=None,
        # which correctly fails the public argv validation before sync policy.
        requirements = _resolve_once_recipe().environments[0].requirements
        recipe = replace(recipe, environments=(replace(
            recipe.environments[0], requirements=requirements),))
    monkeypatch.setattr(recipes, "load_recipe", lambda *_: recipe)
    monkeypatch.setattr(runner, "helper_manifest", lambda _recipe: {})
    context.recipe = recipe
    plan = runner.plan_install(recipe, root=ROOT, placement="local")
    runner.install_engine(plan, context=context, adapter=adapter, deadline=deadline)
    commands = [event[1] for event in context.events if event[0] == "argv"]
    sync = next(command for command in commands if command[:4] == (
        ROOT + "/runtime/bootstrap/bin/uv", "--no-config", "pip", "sync"))
    support = ROOT + "/.astrid-engine/environments/comfy/support.lock"
    assert sync == (
        ROOT + "/runtime/bootstrap/bin/uv", "--no-config", "pip", "sync",
        "--python", ROOT + "/runtime/venv/bin/python",
        "--index-url", "https://pypi.org/simple", "--require-hashes", support)


def test_health_timeout_is_bounded_by_recipe_and_caller_deadline(setup):
    plan, context, adapter, _ = setup
    deadline = time.monotonic() + 1000
    context.deadline = deadline
    before = time.monotonic()
    runner.install_engine(plan, context=context, adapter=adapter, deadline=deadline)
    requests = [event for event in context.events if event[0] == "service:health"]
    assert requests
    for _, payload, limit in requests:
        assert before < limit < deadline
        assert limit <= time.monotonic() + context.recipe.health.timeout_seconds
        assert payload["poll_interval_seconds"] == context.recipe.health.poll_interval_seconds


def test_changed_models_config_refuses_installed_status_without_repair(setup):
    install(setup)
    setup[1].models_config_valid = False
    setup[1].events.clear()
    assert status(setup).status == "failed"
    assert not any(event[0] in {"write", "mkdir", "download", "service:start"} for event in setup[1].events)


def test_target_models_config_check_reads_exact_bytes(tmp_path):
    payload = _helper_payload(tmp_path)
    assert target.dispatch("models-config-check", {**payload, "models_root": None})["facts"] == {"valid": False}
    target.dispatch("models-config", {**payload, "models_root": None})
    assert target.dispatch("models-config-check", {**payload, "models_root": None})["facts"] == {"valid": True}
    (tmp_path / "runtime/extra_model_paths.yaml").write_text('{"unbound":"elsewhere"}')
    assert target.dispatch("models-config-check", {**payload, "models_root": None})["facts"] == {"valid": False}


def test_context_lock_failure_never_runs_or_writes(setup):
    error = CustodyUnresolved("live lease has not been fenced", recovery_handle="/holder/exact-incarnation")
    @contextmanager
    def occupied(root, *, deadline):
        raise error
        yield
    setup[1].acquire_install_lock = occupied
    with pytest.raises(CustodyUnresolved) as caught:
        install(setup)
    assert caught.value is error and not setup[1].events


def test_changed_root_from_adapter_refuses_before_target_io(setup):
    setup[2].validate_root = lambda root: "/different/root"
    with pytest.raises(UnsafePathError):
        install(setup)
    assert not setup[1].events


def test_dangling_receipt_symlink_is_not_absent_installation(setup):
    setup[1].files[ROOT + "/" + runner._RECEIPT] = FileFact(False, False, True, None, None)
    with pytest.raises(UnsafePathError):
        status(setup)
    with pytest.raises(UnsafePathError):
        install(setup)
    assert not any(event[0] in {"write", "mkdir", "download"} for event in setup[1].events)


def test_empty_adapter_command_is_refused_with_stable_error(setup):
    setup[2].environment_commands = lambda *_: ((),)
    with pytest.raises(RecipeValidationError) as caught:
        install(setup)
    assert caught.value.step_id == "environment:comfy"


def test_target_health_hashes_parsed_canonical_json_with_nonascii_and_key_order():
    first = {"z": {"labels": ["café", "video"]}, "H3": {"b": 2, "a": 1}}
    second = {"H3": {"a": 1, "b": 2}, "z": {"labels": ["café", "video"]}}
    a = target._health_facts({"system": {}}, first, ("H3",))
    b = target._health_facts({"system": {}}, second, ("H3",))
    canonical = json.dumps(first, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert a == b == {"healthy": True, "object_info_sha256": hashlib.sha256(canonical).hexdigest(),
                      "required_nodes_present": True}
    assert len(runner._json(a)) < 200


@pytest.mark.parametrize("health,objects,nodes", [(None, {"H3": {}}, ("H3",)), ({}, None, ("H3",)),
                                                 ({}, {"H3": float("nan")}, ("H3",)), ({}, {"other": {}}, ("H3",))])
def test_target_health_invalid_or_missing_evidence_never_admits(health, objects, nodes):
    result = target._health_facts(health, objects, nodes)
    assert result["healthy"] is not True or result["required_nodes_present"] is not True


def test_environment_command_env_applies_only_to_its_indexed_bootstrap_command(setup):
    plan, context, _, deadline = setup

    class PipEnvAdapter(FakeAdapter):
        def environment_commands(self, recipe, environment, root):
            return (("fake-bootstrap", environment.name, "pip"), ("fake-bootstrap", environment.name, "venv"))

        def environment_command_env(self, recipe, environment, root, index):
            return {"DEB_PYTHON_INSTALL_LAYOUT": "deb"} if index == 0 else {}

    adapter = PipEnvAdapter()
    seen = []
    original = context.run

    def recording(request):
        if request.argv[0] == "fake-bootstrap":
            seen.append((request.argv[-1], dict(request.env)))
        return original(request)
    context.run = recording
    assert runner.install_engine(plan, context=context, adapter=adapter, deadline=deadline).status == "installed"
    base = runner.engine_process_env(adapter.environment(context.recipe, ROOT), ROOT)
    assert seen == [("pip", {**base, "DEB_PYTHON_INSTALL_LAYOUT": "deb"}), ("venv", base)]


def _sanity_context(context, code=0, stdout=""):
    original = context.run

    def run(request):
        if request.argv[0] == "fake-sanity":
            context.events.append(("sanity", request.argv, dict(request.env)))
            return context.result(stdout, code)
        return original(request)
    context.run = run


def test_sanity_hook_is_off_by_default(setup, monkeypatch):
    monkeypatch.delenv(runner.SANITY_CMD_ENV, raising=False)
    _sanity_context(setup[1])
    assert install(setup).status == "installed"
    assert not [e for e in setup[1].events if e[0] == "sanity"]


def test_sanity_failure_aborts_before_any_model_step_and_names_lib_paths(setup):
    plan, context, adapter, deadline = setup
    _sanity_context(context, code=2, stdout="FAIL RuntimeError: boom\nLIB /v/nvidia/cublas/lib/libcublas.so.13\n")
    with pytest.raises(runner.CommandFailed) as caught:
        runner.install_engine(plan, context=context, adapter=adapter, deadline=deadline,
                              sanity_cmd="fake-sanity")
    message = str(caught.value)
    assert "libcublas.so.13" in message and "LD_LIBRARY_PATH=" in message and "exit 2" in message
    assert not [e for e in context.events if e[0] in {"download", "service:start"}]


def test_model_free_install_receipt_binds_the_model_root_unpatched(setup):
    """Plan 32 T5h: a fresh model-free install records no models, and the model-root binding
    derived from that receipt is valid (empty inventory), with no patch on the derivation."""
    from astrid.core.generation import engine_local_endpoint as endpoint

    receipt = install(setup)
    assert receipt.status == "installed" and tuple(receipt.models) == ()
    binding = endpoint.model_root_binding_from_receipt(receipt, ROOT, engine="comfyui")
    assert list(binding.inventory) == []
    bound = endpoint.composed_model_root_binding(receipt, ROOT, engine="comfyui", deadline=time.monotonic() + 30)
    assert list(bound.inventory) == []


def test_legacy_receipt_with_model_rows_parses_and_binds(setup):
    """Plan 32 T5h: a legacy receipt's verified model rows parse as the receipt's rows and bind."""
    from astrid.core.generation import engine_local_endpoint as endpoint

    plan = runner.plan_install(setup[1].recipe, root=ROOT, placement="local")
    row = DownloadResult("synthetic-test-lora", ROOT + "/models/loras/synthetic-test-b.safetensors",
                         "downloaded", 9, "b" * 64, True, 0)
    legacy = replace(install(setup), models=(row,))
    parsed = runner._parse_receipt(json.loads(json.dumps(runner._plain(legacy))), plan)
    assert parsed.models == (row,)
    binding = endpoint.model_root_binding_from_receipt(parsed, ROOT, engine="comfyui")
    assert [(r["name"], r["subdir"]) for r in binding.inventory] == [("synthetic-test-b.safetensors", "loras")]


def test_true_legacy_receipt_with_model_steps_and_rows_parses(setup):
    """Plan 32 T5h: a receipt written the old way (model steps plus model rows) parses as installed.
    The model steps are validated and dropped; the plan's expected steps and rows are kept."""
    plan = runner.plan_install(setup[1].recipe, root=ROOT, placement="local")
    lock = recipes.load_model_lock(setup[1].recipe.models_lock).models
    rows = tuple(DownloadResult(m.name, ROOT + "/models/" + m.destination, "downloaded", m.bytes, m.sha256,
                                True, 0) for m in lock)
    model_steps = tuple(runner.StepReceipt("model:" + m.name, "model", "executed", "passed", {}) for m in lock)
    base = install(setup)
    plan_steps = tuple(base.steps)
    legacy_steps = plan_steps[:-2] + model_steps + plan_steps[-2:]  # model steps sat before start/health
    legacy = replace(base, models=rows, steps=legacy_steps)
    parsed = runner._parse_receipt(json.loads(json.dumps(runner._plain(legacy))), plan)
    assert parsed.status == "installed"
    assert parsed.models == rows
    assert tuple(s.id for s in parsed.steps) == tuple(s.id for s in plan.steps)


def test_status_rechecks_with_the_model_directory_the_install_bound(setup):
    """A paid pod install bound a volume model cache; `engines status` names none.

    Status must take the receipt's models_root, or the read-only start check
    compares the live models configuration against <root>/models and reports a
    healthy engine as integrity_failed at step "start".
    """
    plan, context, adapter, deadline = setup
    bound = {}
    original_run = context.run

    def run(request):
        argv = request.argv
        if "engine_runner_target" in " ".join(argv):
            operation, payload = argv[-2], json.loads(argv[-1])
            if operation == "models-config":
                bound["models_root"] = payload.get("models_root")
            elif operation == "models-config-check":
                context.models_config_valid = (
                    "models_root" in bound and payload.get("models_root") == bound["models_root"])
        return original_run(request)

    context.run = run
    cached = runner.plan_install(context.recipe, root=ROOT, placement="local", models_root="/MODEL_CACHE/models")
    installed = runner.install_engine(cached, context=context, adapter=adapter, deadline=deadline)
    assert installed.status == "installed" and installed.models_root == "/MODEL_CACHE/models"
    # The reader names no model directory, exactly like `astrid engines status` on the pod.
    current = runner.engine_status(plan.engine, platform=plan.platform, placement=plan.placement,
                                   root=plan.root, context=context, deadline=deadline)
    assert current.status == "installed", (current.status, current.error_code, current.failed_step_id)


def test_plan_from_receipt_rejects_locator_mismatches(setup):
    plan, context, adapter, deadline = setup
    installed = runner.install_engine(plan, context=context, adapter=adapter, deadline=deadline)
    with pytest.raises(IntegrityError, match="root"):
        runner.plan_from_receipt(context.recipe, installed, root=ROOT + "/other", placement="local")
    with pytest.raises(IntegrityError, match="placement"):
        runner.plan_from_receipt(context.recipe, installed, root=ROOT, placement="runpod")


def test_install_plan_bound_fields_are_recorded_or_recipe_derived():
    # Add every newly bound InstallPlan field to exactly one category here.
    recorded = {"root", "placement", "models_root"}
    recipe_derived = {"schema", "engine", "platform", "recipe_sha256", "models_lock_sha256", "steps"}
    assert {field.name for field in fields(runner.InstallPlan)} == recorded | recipe_derived


def test_source_accepts_only_verified_model_link_entries(setup, monkeypatch):
    plan, context, adapter, deadline = setup
    instance = runner._Runner(plan, context, adapter, deadline)
    source = next(s for s in instance.recipe.sources if s.name == "comfyui")
    context.sources[ROOT + "/runtime/ComfyUI"] = {
        "origin": source.pin.repository, "head": source.pin.revision, "dirty": "?? models/hunyuan_foley"}
    context.directories.update({ROOT + "/runtime/ComfyUI", ROOT + "/runtime/ComfyUI/.git"})
    monkeypatch.setattr(instance, "helper", lambda operation, **kwargs: {"valid": operation == "models-source-check"})
    assert instance.source(source, readonly=True)[0] == "skipped"
    monkeypatch.setattr(instance, "helper", lambda operation, **kwargs: {"valid": False})
    with pytest.raises(IntegrityError, match="source contains uncommitted content"):
        instance.source(source, readonly=True)
    context.sources[ROOT + "/runtime/ComfyUI"]["dirty"] = "?? stray"
    with pytest.raises(IntegrityError, match="source contains uncommitted content"):
        instance.source(source, readonly=True)


def test_a_failed_command_keeps_its_whole_output_on_the_error():
    from types import SimpleNamespace

    from astrid.core.generation.engine_contracts import CommandFailed

    result = SimpleNamespace(timed_out=False, exit_code=2, job_id=None, stdout="out line\n",
                             stderr="err line\n", stdout_truncated=False, stderr_truncated=True)
    owner = runner._Runner.__new__(runner._Runner)
    owner.deadline, owner.step_id, owner.env = time.monotonic() + 60, "environment:comfy", {}
    owner.plan = SimpleNamespace(root="/engine")
    owner.context = SimpleNamespace(run=lambda request: result)
    with pytest.raises(CommandFailed) as caught:
        owner.run(("uv", "pip", "install"))
    assert caught.value.command_output == {"argv": ["uv", "pip", "install"], "exit_code": 2,
                                           "stdout": "out line\n", "stderr": "err line\n", "truncated": True}
