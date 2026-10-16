"""Purpose-bound rendering.render commands on the existing standard-pipe relay.

No argv service or new socket: the host selects the backend/source/policy and
retains each original Popen. Only that backend's private pipe can request its
fixed Remotion CLI. All children receive the same confinement policy.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import remotion_pipe as pipe
from .process_group import (
    observe_tree,
    popen_owned_process,
    process_birth_identity,
    terminate_tree,
    verify_tree_absent,
)

VERSION = "astrid.render-command.pipe/v1"
_client = None


def command_client():
    return _client


def _digest(value):
    return hashlib.sha256(pipe._encode(value)).hexdigest()


def _response_line(fd):
    # Execution deadlines belong to the existing CommandTransport timeout and
    # host cancellation loop, never an additional relay elapsed-time ceiling.
    raw = bytearray()
    while not raw.endswith(b"\n"):
        if len(raw) >= pipe.LIMIT:
            raise ValueError("render command response exceeds its bound")
        select.select([fd], [], [])
        part = os.read(fd, min(65536, pipe.LIMIT - len(raw)))
        if not part:
            raise EOFError("render command host closed before a complete response")
        raw.extend(part)
    return bytes(raw)


class RenderCommandClient:
    def __init__(self, binding, digest):
        self.binding, self.digest, self.sequence = binding, digest, 0
        if any(not stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in (0, 1)):
            raise ValueError("render command bootstrap requires original standard pipes")
        self.read_fd, self.write_fd = os.dup(0), os.dup(1)
        for fd in (self.read_fd, self.write_fd):
            os.set_inheritable(fd, False)

    def request(self, operation, request):
        if (
            _client is not self
            or self.binding["pid"] != os.getpid()
            or self.binding["birth"] != process_birth_identity()
        ):
            raise ValueError("render command executor incarnation changed")
        self.sequence += 1
        metadata = {"binding": self.digest, "sequence": self.sequence}
        pipe._write(
            self.write_fd,
            pipe.PREFIX
            + pipe._encode({"_rendererPipe": metadata, "operation": operation, "request": request}),
            deadline=time.monotonic() + 300,
        )
        response = pipe._decode(_response_line(self.read_fd))
        returned = response.pop("_rendererPipe", None)
        if (
            not isinstance(returned, dict)
            or type(returned.get("sequence")) is not int
            or returned != metadata
        ):
            raise ValueError("render command reply binding changed")
        if response.get("ok") is not True:
            raise RuntimeError("host render command failed")
        return response["result"]

    def close(self):
        os.close(self.read_fd)
        os.close(self.write_fd)


def install_render_command_client():
    global _client
    if _client is not None:
        raise ValueError("render command client already installed")
    bootstrap = pipe._decode(pipe._line(0, timeout=30))
    binding = bootstrap.get("binding", {})
    if (
        set(bootstrap) != {"protocol", "binding", "digest"}
        or not isinstance(binding, dict)
        or set(binding)
        != {
            "purpose",
            "admission",
            "role",
            "pid",
            "birth",
            "executable",
            "launch_digest",
            "source_digest",
            "policy_digest",
        }
        or bootstrap.get("protocol") != VERSION
        or bootstrap.get("digest") != _digest(binding)
        or binding.get("purpose") != "rendering.render"
        or binding.get("pid") != os.getpid()
        or binding.get("birth") != process_birth_identity()
        or binding.get("executable") != str(Path(sys.executable).resolve())
        or binding.get("role") not in {"executor", "backend"}
        or not isinstance(binding.get("admission"), dict)
        or binding["admission"].get("capability_id") != "rendering.render"
        or not binding["admission"].get("capability_digest")
        or not binding.get("source_digest")
        or not binding.get("policy_digest")
        or not binding.get("launch_digest")
    ):
        raise ValueError("render command bootstrap differs from admitted launch")
    _client = RenderCommandClient(binding, bootstrap["digest"])
    sys.stdout = sys.stderr
    return _client


class HostRenderCommandBridge(pipe.HostPipeBridge):
    """Finite command consumer of HostPipeBridge's existing stdio framing.

    ``observer`` is a host-only test consumer. It receives the real CLI Popen
    and native registration after exec release and before draining it. A test
    can wait on an Event here while the host's cancellation loop remains live.
    It must release its wait when cancellation is requested. No consumer means
    no pause, callback, alternate command, or environment-controlled test hook.
    """

    def __init__(
        self,
        process,
        *,
        policy,
        backend_root,
        admission,
        launch_digest,
        imports,
        observer=None,
        role="executor",
        parent=None,
        closure=None,
        environment=None,
    ):
        if admission.get("capability_id") != "rendering.render" or not admission.get(
            "capability_digest"
        ):
            raise ValueError("render command requires exact admitted capability")
        self.process, self.policy = process, policy
        if not policy.workload_process_creation or policy.pass_fds:
            raise ValueError(
                "render commands require admitted process creation and original standard pipes"
            )
        self.backend_root = Path(backend_root).resolve(strict=True)
        from .generic_host import _verify_admitted_source

        _verify_admitted_source(admission)
        self.source_digest = self._source_digest()
        self.imports, self.observer, self.parent = imports, observer, parent
        self.closure = closure
        self.environment = dict(environment or {})
        self.lifecycle_lock = parent.lifecycle_lock if parent is not None else threading.RLock()
        self.role = role
        self.binding = dict(
            purpose="rendering.render",
            admission=admission,
            role=role,
            pid=process.pid,
            birth=process._astrid_process_birth,
            executable=str(Path(sys.executable).resolve()),
            launch_digest=launch_digest,
            source_digest=self.source_digest,
            policy_digest=hashlib.sha256(repr(policy).encode()).hexdigest(),
        )
        self.digest = _digest(self.binding)
        self.sequence, self.error = 0, None
        self.cancelled = threading.Event()
        self.stdout, self.stderr = bytearray(), bytearray()
        self.children = set()
        self.child_bridges = []
        self._closed = False
        broker = process._astrid_custody._launch_broker
        if (
            broker._retained_launch_child._child is not process
            or not broker._release_sent
            or not broker._post_exec_authority_validated
            or any(
                not stat.S_ISFIFO(os.fstat(stream.fileno()).st_mode)
                for stream in (process.stdin, process.stdout, process.stderr)
            )
        ):
            raise ValueError("render command requires the original retained standard pipes")
        self.actor = None
        self.identity = None
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.stderr_thread = threading.Thread(target=self._stderr, daemon=True)
        try:
            pipe._write(
                process.stdin.fileno(),
                pipe._encode(dict(protocol=VERSION, binding=self.binding, digest=self.digest)),
                deadline=time.monotonic() + 30,
            )
            self.thread.start()
            self.stderr_thread.start()
        except BaseException as error:
            error.remotion_bridge = self
            raise

    def _source_digest(self):
        from .generic_host import _source_digest_for_roots

        return _source_digest_for_roots((self.backend_root,))

    def _guard(self):
        if self.parent is not None:
            self.parent._guard()
        if self.cancelled.is_set() or self.process.poll() is not None:
            raise RuntimeError("render command owner is no longer active")
        if self.actor is None:
            from .custody_broker import (
                AuthenticatedCleanupActor,
                _bootstrap_anchored_token,
                current_process_audit_token,
            )

            broker = self.process._astrid_custody._launch_broker
            expected = broker.registration["identity"]
            anchor = broker.registration["bootstrap_start_anchor"]
            self.actor = AuthenticatedCleanupActor(
                lambda: _bootstrap_anchored_token(self.process.pid, anchor),
                broker.identity_provider,
            )
            self.identity = self.actor.verify()
            if any(self.identity.get(k) != expected.get(k) for k in ("pid", "uid", "birth_id")):
                raise ValueError("render command actor differs from retained registration")
            broker.bind_ready_token(
                expected_pid=self.process.pid,
                expected_identity=expected,
                token_provider=current_process_audit_token,
            )
        identity = self.actor.verify()
        if any(
            identity.get(k) != self.identity.get(k)
            for k in ("pid", "uid", "birth_id", "audit_token_sha256", "audit_token_pidversion")
        ):
            raise RuntimeError("render command peer incarnation changed")

    def _artifact(self, raw, *, exists):
        path = Path(raw)
        root = self.policy.private_write_root.resolve(strict=True)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or path.is_symlink()
            or not path.resolve(strict=exists).is_relative_to(root)
            or any(p.is_symlink() for p in path.parents)
        ):
            raise ValueError("render command artifact is outside retained scratch")
        return path

    def _launch(self, argv, **options):
        with self.lifecycle_lock:
            self._guard()
            return self._launch_locked(argv, **options)

    def _launch_locked(self, argv, **options):
        from .generic_host import _confined_launch_plan

        backend = options.pop("_backend", False)
        cli_environment = options.pop("_cli_environment", None)
        # This is a private host callback, never received from the pipe.
        if backend:
            args = argv[1:]
            startup = (
                "import json,os,runpy,sys; sys.path.extend(json.loads(sys.argv[1])); "
                "os.environ.update(json.loads(sys.argv[2])); sys.argv=sys.argv[3:]; "
                "from astrid.core.execution.render_command_pipe import install_render_command_client; "
                "install_render_command_client(); runpy.run_path(sys.argv[0],run_name='__main__')"
            )
            from astrid.core.subprocess_env import build_child_subprocess_env

            host_env = build_child_subprocess_env(base=self.environment, parent=self.environment)
            # Host-selected render qualification references only. Caller env
            # overlays never cross the pipe; PATH/HOME/TMP stay policy-owned.
            host_env = {k: v for k, v in host_env.items() if k.startswith("ASTRID_")}
            host_env.update(
                ASTRID_INTERNAL_INVOCATION="1", ASTRID_RENDER_BACKEND="rendering.remotion"
            )
            argv = [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                startup,
                json.dumps(self.imports),
                json.dumps(host_env),
                *args,
            ]
        elif cli_environment is not None:
            # A fixed host wrapper preserves the original backend's finite
            # invocation environment under Seatbelt, then SETEXECs the pinned
            # real CLI without changing its PID/parent/birth identity.
            argv = [
                sys.executable,
                "-I",
                "-S",
                "-B",
                "-c",
                "import json,os,sys; args=json.loads(sys.argv[1]); "
                "os.environ.update(json.loads(sys.argv[2])); os.execve(args[0],args,dict(os.environ))",
                json.dumps(argv),
                json.dumps(cli_environment),
            ]
        plan = _confined_launch_plan(
            argv,
            policy=self.policy,
            cwd=Path(options["cwd"]),
            parent_environment=options.get("env"),
        )
        try:
            process = popen_owned_process(
                list(plan.argv),
                **plan.subprocess_options(),
                text=True,
                start_new_session=options.get("start_new_session", True),
            )
        except BaseException as error:
            retained = getattr(error, "process", None)
            if retained is not None:
                self.children.add(retained)
            raise
        self.children.add(process)
        if backend:
            try:
                bridge = HostRenderCommandBridge(
                    process,
                    policy=self.policy,
                    backend_root=self.backend_root,
                    admission=self.binding["admission"],
                    launch_digest=_digest([plan.argv, str(plan.cwd), plan.environment]),
                    imports=self.imports,
                    observer=self.observer,
                    role="backend",
                    parent=self,
                    closure=self.closure,
                    environment=self.environment,
                )
            except BaseException as error:
                failed = getattr(error, "remotion_bridge", None)
                if failed is not None:
                    self.child_bridges.append(failed)
                raise
            self.child_bridges.append(bridge)
        return process

    def _backend(self, request):
        from astrid.core.rendering.errors import RendererException
        from astrid.core.rendering.transport import CommandTransport

        from .generic_host import _verify_admitted_source

        _verify_admitted_source(self.binding["admission"])
        expected = {
            "verb",
            "command",
            "backend",
            "request_path",
            "result_path",
            "cwd",
            "timeout",
            "required_binaries",
        }
        if (
            self.role != "executor"
            or set(request) != expected
            or request["backend"] != "rendering.remotion"
            or request["verb"] not in {"support", "render"}
            or (
                request["command"] != ["python3", "run.py"]
                and request["command"] != [str(Path(sys.executable).resolve()), "run.py"]
                and request["command"] != [sys.executable, "run.py"]
            )
            or Path(request["cwd"]).resolve()
            not in {self.backend_root, self.backend_root / "rendering/renderers/remotion"}
            or self._source_digest() != self.source_digest
        ):
            raise ValueError("render command differs from admitted Remotion backend")
        self._artifact(request["request_path"], exists=True)
        self._artifact(request["result_path"], exists=False)
        # CommandTransport retains parsing/error/timeout semantics. Its launcher
        # is host-selected; the remote caller never gets a synthetic Popen.
        transport = CommandTransport("rendering.remotion")
        transport._process_launcher = lambda argv, **kw: self._launch(argv, _backend=True, **kw)
        # A backend uses its own stdio relay. Drain it through that bridge,
        # rather than racing CommandTransport.communicate for its reserved pipe.
        transport._process_exchange = self._exchange_backend
        transport._process_terminate = self._terminate_backend
        try:
            result = transport.run(**request)
        except RendererException as error:
            return {"payload": error.error.to_dict(), "logs": transport.last_logs}
        return {"payload": result.to_dict(), "logs": transport.last_logs}

    def _terminate_backend(self, process, *, grace):
        terminate_tree(process, grace_seconds=grace)
        process.wait(timeout=5)
        bridge = next(b for b in self.child_bridges if b.process is process)
        return bridge.finish(cancelled=True)

    def _exchange_backend(self, process, timeout):
        bridge = next(b for b in self.child_bridges if b.process is process)
        deadline = None if timeout is None else time.monotonic() + timeout
        while process.poll() is None:
            self._guard()
            bridge.check()
            observe_tree(process)
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args, timeout)
            time.sleep(0.02)
        return bridge.finish(cancelled=process.returncode != 0)

    def _cli(self, request):
        # Only the admitted backend's own pipe has this operation. The executor
        # cannot select Node code, a CLI, environment, or an executable.
        if self.role != "backend" or set(request) != {"argv", "cwd", "render_environment"}:
            raise ValueError("Remotion CLI requires the admitted backend pipe")
        args = request["argv"]
        if (
            not isinstance(args, list)
            or len(args) < 9
            or any(not isinstance(a, str) or "\0" in a for a in args)
        ):
            raise ValueError("invalid Remotion CLI arguments")
        # These paths must already belong to the retained policy. Compilation
        # below revalidates executable, cwd, filesystem and network authority.
        project = Path(request["cwd"]).resolve(strict=True)
        if self.closure is None or project != self.closure.project:
            raise ValueError("Remotion CLI requires the host admitted installed closure")
        cli = self.closure.validate(project)
        if Path(args[1]).resolve(strict=True) != cli.resolve(strict=True) or args[2] != "render":
            raise ValueError("Remotion CLI operation changed")
        if args[3] not in {"TimelineComposition", "AstridTimeline"}:
            raise ValueError("Remotion composition changed")
        # A finite set of render-only flags; no config, entrypoint, JS, browser,
        # executable, or public-dir override can be supplied on this channel.
        flags = (
            "--port=",
            "--concurrency=1",
            "--bundle-cache=false",
            "--image-format=",
            "--crf=",
            "--max-rate=",
            "--buffer-size=",
            "--audio-bitrate=",
            "--codec=",
            "--audio-codec=",
            "--sample-rate=",
            "--scale=",
            "--pixel-format=",
            "--prores-profile=",
        )
        if args[4] != "--props" or args[6] != "--output":
            raise ValueError("Remotion artifact arguments changed")
        self._artifact(args[5], exists=True)
        self._artifact(args[7], exists=False)
        if any(
            a not in {"--allow-html-in-canvas", "--enforce-audio-track"}
            and not any(a.startswith(f) for f in flags)
            for a in args[8:]
        ):
            raise ValueError("unsupported Remotion render flag")
        # Node is pinned to an existing executable binding, never caller PATH.
        bindings = self.policy.executable_bindings
        if not any(
            str(b.executable.resolved) == str(Path(args[0]).resolve()) and b.name == "node"
            for b in bindings
        ):
            raise ValueError("Remotion Node differs from admitted executable")
        environment = request["render_environment"]
        if not isinstance(environment, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in environment.items()
        ):
            raise ValueError("invalid Remotion invocation environment")
        allowed = {
            "TMPDIR",
            "TMP",
            "TEMP",
            "ASTRID_REMOTION_PCM_AAC_OUTPUT",
            "PYTHONPATH",
            "ASTRID_TIMELINE_COMPOSITION_SRC",
            "ASTRID_REMOTION_STREAM_FRAMES",
            "ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES",
        }
        if set(environment) - allowed:
            raise ValueError("unsupported Remotion invocation environment")
        for key in ("TMPDIR", "TMP", "TEMP"):
            self._artifact(environment.get(key, ""), exists=True)
        if len({environment[k] for k in ("TMPDIR", "TMP", "TEMP")}) != 1:
            raise ValueError("Remotion temp roots differ")
        if environment.get("ASTRID_REMOTION_PCM_AAC_OUTPUT", args[7]) != args[7]:
            raise ValueError("Remotion PCM output differs")
        if environment.get(
            "PYTHONPATH", self.environment.get("ASTRID_TIMELINE_SCHEMA_PYTHONPATH")
        ) != self.environment.get("ASTRID_TIMELINE_SCHEMA_PYTHONPATH"):
            raise ValueError("Remotion schema differs from host selection")
        composition = str(project / "node_modules/@banodoco/timeline-composition/typescript/src")
        if environment.get("ASTRID_TIMELINE_COMPOSITION_SRC", composition) != composition:
            raise ValueError("Remotion Composition differs from installed closure")
        budget = self.environment.get("ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES", "0")
        if "ASTRID_REMOTION_STREAM_FRAMES" in environment and (
            environment["ASTRID_REMOTION_STREAM_FRAMES"] != "1"
            or environment.get("ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES") != budget
            or not budget.isdecimal()
            or int(budget) <= 0
        ):
            raise ValueError("Remotion streaming allocation differs from host envelope")
        if (
            "ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES" in environment
            and "ASTRID_REMOTION_STREAM_FRAMES" not in environment
        ):
            raise ValueError("Remotion streaming allocation lacks its finite operation")
        process = self._launch(args, cwd=str(project), _cli_environment=environment)
        registration = process._astrid_custody._launch_broker.registration
        event = {
            "event": "started",
            "pid": process.pid,
            "parent_pid": os.getpid(),
            "birth": process._astrid_process_birth,
            "registration": registration,
            "command": tuple(args),
            "source_digest": self.source_digest,
            "policy_digest": self.binding["policy_digest"],
        }
        try:
            if self.observer is not None:
                from .custody_broker import (
                    AuthenticatedCleanupActor,
                    _bootstrap_anchored_token,
                    _darwin_command_metadata,
                )

                deadline = time.monotonic() + 5
                while True:
                    self._guard()
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("real Remotion CLI start was not observed")
                    metadata = _darwin_command_metadata(process.pid)
                    if metadata["executable"] == str(Path(args[0]).resolve()):
                        break
                    time.sleep(0.001)
                actor = AuthenticatedCleanupActor(
                    lambda: _bootstrap_anchored_token(
                        process.pid, registration["bootstrap_start_anchor"]
                    ),
                    process._astrid_custody._launch_broker.identity_provider,
                )
                event.update(identity=actor.verify(), executable=metadata["executable"])
                if metadata["bsd_identity"]["parent_pid"] != os.getpid():
                    raise RuntimeError("real Remotion CLI parent differs from retained host")
                if process.poll() is not None:
                    raise RuntimeError("real Remotion CLI exited before the start observer")
                self.observer(event, process, self.cancelled)
            while True:
                self._guard()
                try:
                    stdout, stderr = process.communicate(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    observe_tree(process)
            verify_tree_absent(process)
            return dict(stdout=stdout, stderr=stderr, returncode=process.returncode)
        finally:
            if process.poll() is None:
                terminate_tree(process)
            process.wait()
            verify_tree_absent(process)
            if self.observer is not None:
                self.observer(
                    {**event, "event": "exited", "returncode": process.returncode},
                    process,
                    self.cancelled,
                )

    def _dispatch(self, raw):
        frame = pipe._decode(raw)
        metadata = frame.pop("_rendererPipe", None)
        if (
            not isinstance(metadata, dict)
            or type(metadata.get("sequence")) is not int
            or metadata != {"binding": self.digest, "sequence": self.sequence + 1}
        ) or set(frame) != {
            "operation",
            "request",
        }:
            raise ValueError("render command request binding changed")
        self._guard()
        self.sequence += 1
        from .custody_broker import _append_owner_jsonl

        journal = self.process._astrid_custody.root / "custody.journal.jsonl"
        evidence = dict(
            event="render_command_pending",
            binding=self.digest,
            sequence=self.sequence,
            operation=frame["operation"],
            request_sha256=_digest(frame["request"]),
        )
        _append_owner_jsonl(journal, evidence)
        try:
            if frame["operation"] == "backend":
                result = self._backend(frame["request"])
            elif frame["operation"] == "remotion_cli":
                result = self._cli(frame["request"])
            else:
                raise ValueError("unsupported render command operation")
        except BaseException as error:
            _append_owner_jsonl(
                journal,
                {**evidence, "event": "render_command_failed", "error_type": type(error).__name__},
            )
            raise
        _append_owner_jsonl(
            journal,
            {
                **evidence,
                "event": "render_command_completed",
                "children": [
                    {"pid": p.pid, "birth": p._astrid_process_birth, "exit": p.returncode}
                    for p in self.children
                ],
            },
        )
        self._guard()
        pipe._write(
            self.process.stdin.fileno(),
            pipe._encode({"_rendererPipe": metadata, "ok": True, "result": result}),
            deadline=time.monotonic() + 300,
            guard=self._guard,
        )

    def finish(self, *, cancelled=False):
        if cancelled:
            with self.lifecycle_lock:
                self.cancelled.set()
                for bridge in self.child_bridges:
                    bridge.cancelled.set()
                children = tuple(self.children)
            for child in children:
                if child.poll() is None:
                    terminate_tree(child)
        if self.thread.ident is not None:
            self.thread.join(timeout=10)
        if self.stderr_thread.ident is not None:
            self.stderr_thread.join(timeout=5)
        if self.thread.is_alive() or self.stderr_thread.is_alive():
            raise RuntimeError("render command relay cleanup remains unresolved")
        for child in self.children:
            child.wait(timeout=5)
            verify_tree_absent(child)
            for stream in (child.stdin, child.stdout, child.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
        for bridge in self.child_bridges:
            bridge.finish(cancelled=cancelled)
        if not self._closed:
            self._closed = True
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
        if not cancelled:
            self.check()
        return self.stdout.decode(errors="replace"), self.stderr.decode(errors="replace")
