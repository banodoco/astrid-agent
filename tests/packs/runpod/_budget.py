"""One synchronized CPU budget for transport attempts and traced pod processes.

The machine budget uses production HTTPS framing against CpuAgent; strace follows
actual shell/exec/fork children. Legacy public-fake adapters remain available.
"""
from __future__ import annotations

import inspect
import json
import re
import shlex
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import MutableMapping
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from runpod_lifecycle.https import HttpsPodTransport as RealHttpsPodTransport

from astrid.packs.runpod.remote_placement import _HttpsOps

_REAL_BOUNDED_CALL = _HttpsOps._bounded_call


class CpuHttpsPodTransport(RealHttpsPodTransport):
    def __init__(self, pod_id, token, **kwargs):
        # The legacy fixture's pod-1 is not a valid RunPod proxy hostname.
        # Adapt only at the loopback wire boundary, never in claim-scoped ops.
        assert kwargs.get("base_url", "").startswith("http://127.0.0.1:")
        super().__init__("pod1" if pod_id == "pod-1" else pod_id, token, **kwargs)


class SharedCounts(MutableMapping):
    """SQLite transactions synchronize threads and independent helper processes."""

    def __init__(self, path):
        self.path = str(path)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS counts (kind TEXT PRIMARY KEY, n INTEGER NOT NULL)")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, kind, n=1):
        with self.connection() as db:
            db.execute("INSERT INTO counts VALUES (?, ?) ON CONFLICT(kind) DO UPDATE SET n=n+excluded.n", (kind, n))

    def __getitem__(self, key):
        with self.connection() as db:
            row = db.execute("SELECT n FROM counts WHERE kind=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        return row[0]

    def __setitem__(self, key, value):
        with self.connection() as db:
            db.execute("INSERT OR REPLACE INTO counts VALUES (?, ?)", (key, value))

    def __delitem__(self, key):
        with self.connection() as db:
            db.execute("DELETE FROM counts WHERE kind=?", (key,))

    def __iter__(self):
        with self.connection() as db:
            return iter([row[0] for row in db.execute("SELECT kind FROM counts ORDER BY kind")])

    def __len__(self):
        with self.connection() as db:
            return db.execute("SELECT count(*) FROM counts").fetchone()[0]

    def clear(self):
        with self.connection() as db:
            db.execute("DELETE FROM counts")


class RemoteBudget:
    def __init__(self, path=None):
        self._temporary = tempfile.TemporaryDirectory(prefix="remote-budget-") if path is None else None
        self.counts = SharedCounts(path or Path(self._temporary.name) / "counts.sqlite")

    def wrap(self, monkeypatch, target, name, *, kind=None, command=False):
        original = getattr(target, name)
        key = kind or name

        def count(args, kwargs):
            self.counts.add(key)
            if command:
                position = 1 if isinstance(target, type) else 0
                self.counts.add("process_start", self.process_starts(
                    args[position] if len(args) > position else kwargs["command"]))

        if inspect.iscoroutinefunction(original):
            @wraps(original)
            async def wrapped(*args, **kwargs):
                count(args, kwargs)
                return await original(*args, **kwargs)
        else:
            @wraps(original)
            def wrapped(*args, **kwargs):
                count(args, kwargs)
                return original(*args, **kwargs)
        monkeypatch.setattr(target, name, wrapped)

    def http_attempts(self, monkeypatch=None, *, machine=False):
        """Below HttpsPodTransport._request's retry loop and chunk machinery."""
        import httpx
        original = httpx.Client._send_single_request
        counts = self.counts

        def send(client, request):
            path = request.url.path
            if path == "/uploads":
                kind = "put.create"
            elif path.startswith("/uploads/"):
                kind = {"PUT": "put.chunk", "GET": "put.reconcile", "POST": "put.verify"}[request.method]
            elif path == "/download-chunk":
                body = json.loads(request.content)
                kind = ("job_poll.read" if body["path"].rsplit("/", 1)[-1] in {"status", "exit", "pid"}
                        and "/.astrid-jobs/" in body["path"] else "get.chunk")
            else:
                kind = "http." + request.method.lower() + "." + path.strip("/").split("/")[0]
            counts.add(kind)
            return original(client, request)
        if monkeypatch is None:
            httpx.Client._send_single_request = send
        else:
            monkeypatch.setattr(httpx.Client, "_send_single_request", send)
            # Python-spawned transport helpers import sitecustomize before
            # their target. Do not rely on a parent-only monkeypatch surviving.
            import os
            startup = Path(self.counts.path).parent / "budget-startup"
            startup.mkdir(exist_ok=True)
            (startup / "sitecustomize.py").write_text(
                "from tests.packs.runpod._budget import RemoteBudget, CpuHttpsPodTransport\n"
                + ("from runpod_lifecycle import https\n"
                   "https.HttpsPodTransport = CpuHttpsPodTransport\n" if machine else "")
                + f"_budget = RemoteBudget({self.counts.path!r})\n"
                "_budget.http_attempts()\n")
            monkeypatch.setenv("PYTHONPATH", str(startup) + os.pathsep + os.environ.get("PYTHONPATH", ""))

    def run_pod(self, command, *, cwd=None, env=None):
        """Trace successful kernel execs, including shell/Python grandchildren.

        The trace follows forks and execs; a quoted program is never parsed as
        shell source. This runs only CPU fake programs in a caller-owned scratch
        directory. No provider, environment or credentials are loaded.
        """
        with tempfile.TemporaryDirectory(prefix="pod-trace-") as directory:
            trace = Path(directory) / "execs"
            result = subprocess.run(["strace", "-ff", "-qq", "-s", "4096", "-e", "trace=%process",
                                     "-o", str(trace), "bash", "-c", command],
                                    cwd=cwd, env=env, capture_output=True, text=True, timeout=30)
            for process_trace in Path(directory).glob("execs.*"):
                for line in process_trace.read_text().splitlines():
                    if (re.search(r"\b(?:clone|clone3|fork|vfork)\(.*= [1-9][0-9]*$", line)
                            and "CLONE_THREAD" not in line):
                        self.counts.add("process_fork")
                    if not re.search(r"= 0$", line):
                        continue
                    match = re.search(r'execve(?:at)?\((?:[^",]+,\s*)?"([^"\n]+)"', line)
                    if match is None:
                        continue
                    self.counts.add("process_launch")
                    binary = Path(match[1]).name
                    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|astrid(?:-tools)?", binary):
                        self.counts.add("process_start")
            return result

    @staticmethod
    def process_starts(command):
        # Tokenizing keeps quoted multiline Python bodies opaque. Preserve shell
        # newlines as operators so every submitted command is examined.
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|(){}\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        tokens = list(lexer)
        boundary = True
        starts = 0
        for token in tokens:
            if token and all(char in ";&|(){}\n" for char in token):
                boundary = True
                continue
            if boundary and (token in {"then", "do", "exec", "env", "!"}
                             or re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", token)):
                continue
            if boundary:
                binary = token.rsplit("/", 1)[-1]
                if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?|astrid(?:-tools)?", binary):
                    starts += 1
            boundary = False
        return starts

    def pod(self, pod, monkeypatch):
        for name in ("exec", "put", "get", "job_start", "job_poll", "job_cancel"):
            self.wrap(monkeypatch, pod, name, command=name in {"exec", "job_start"})

    def https(self, transport, monkeypatch):
        """For HTTPS-only fakes; use instead of pod() when it delegates there."""
        for name, kind in (("execute_command", "exec"), ("put", "put"), ("get", "get"),
                           ("detached_start", "job_start"), ("detached_poll", "job_poll"),
                           ("detached_cancel", "job_cancel")):
            self.wrap(monkeypatch, transport, name, kind=kind, command=kind in {"exec", "job_start"})

    def machine_case(self, case, monkeypatch):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        import httpx
        import runpod_lifecycle
        from runpod_lifecycle import api, https, lifecycle
        from runpod_lifecycle.pod import Pod

        from astrid.packs.runpod import target_selection, worker_preparation
        from astrid.packs.runpod.remote_placement import _HttpsOps

        self.http_attempts(monkeypatch, machine=True)
        agent = CpuAgent(case.pod, self, Path(self.counts.path).parent / "pod")

        class Handler(BaseHTTPRequestHandler):
            def dispatch(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                try:
                    reply = agent.handle(httpx.Request(self.command, "http://localhost" + self.path, content=body))
                except (AssertionError, OSError, ValueError, KeyError, TypeError) as exc:
                    agent.error = exc
                    self.send_error(500)
                    return
                self.send_response(reply.status_code)
                self.send_header("Content-Length", str(len(reply.content)))
                self.end_headers()
                self.wfile.write(reply.content)

            do_GET = do_POST = do_PUT = do_DELETE = dispatch
            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        base_url = self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        original = _HttpsOps.__init__
        def init(ops, pod_id, token, **kwargs):
            kwargs["base_url"] = base_url
            # Redirect the wire endpoint, preserving the exact claim identity.
            original(ops, pod_id, token, **kwargs)
        monkeypatch.setattr(_HttpsOps, "__init__", init)
        assert _REAL_BOUNDED_CALL.__name__ == "_bounded_call", "CPU budget must restore real spawned transport"
        monkeypatch.setattr(_HttpsOps, "_bounded_call", _REAL_BOUNDED_CALL)
        monkeypatch.setattr(https, "HttpsPodTransport", CpuHttpsPodTransport)
        self.agent = agent
        self.wrap(monkeypatch, case.machine, "_observe_mounts", kind="provider.observe_mounts")
        for target, names in (
            (api, ("get_network_volumes", "get_pod_status", "list_pods", "terminate_pod",
                   "reconcile_pod_cleanup")),
            (lifecycle, ("create_pod", "find_gpu_type", "get_storage_volume_id", "check_and_expand_storage")),
            (runpod_lifecycle, ("gpu_availability", "get_pod")),
            (worker_preparation, ("read_exact_pod", "confirm_acquired_machine", "_get_provider_pod")),
            (target_selection, ("read_exact_pod",)),
            (case.adapter, ("observe_mounts",)),
            (Pod, ("wait_ready",)),
            (case.provider, ("claim", "release", "pod_exists")),
        ):
            for name in names:
                self.wrap(monkeypatch, target, name, kind="provider." + name.lstrip("_"))

    def close(self):
        if hasattr(self, "server"):
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=2)

    def assert_within(self, bounds):
        with self.counts.connection() as db:
            observed = dict(db.execute("SELECT kind, n FROM counts ORDER BY kind"))
        excess = {kind: count for kind, count in observed.items() if count > bounds.get(kind, 0)}
        assert not excess, f"remote budget exceeded: {excess}; per-kind counts={observed}; bounds={bounds}"


class CpuAgent:
    """In-memory agent protocol with real HTTPS retries/chunks and shell launches.

    Model/Runtime work remains the machine_case fake. The production shell job
    (including the detached supervisor) executes in scratch with a CPU Astrid
    executable. Standard-library -c programs and their children really execute.
    """

    def __init__(self, pod, budget, root):
        self.pod, self.budget, self.root = pod, budget, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.uploads, self.jobs = {}, {}
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.bin / 'python').symlink_to(sys.executable)
        (self.bin / 'python3').symlink_to(sys.executable)
        script = self.bin / 'astrid'
        script.write_text('''#!'''+sys.executable+'''
import json,sys
from pathlib import Path
args=sys.argv[1:]
if args[:2]==['generate','list']:
    print(json.dumps([{'id':'cpu-image','backend':'local','mode':'t2i','ready':True,'availability':'available'}]))
elif args and args[0]=='generate':
    out=Path(args[args.index('--out')+1]); out.mkdir(parents=True,exist_ok=True)
    (out/'cpu.png').write_bytes(b'png-bytes')
    print(json.dumps({'status':'succeeded'}))
else:
    print('null')
''')
        script.chmod(0o700)
        for part in ('astrid', 'astrid/packs', 'astrid/packs/runpod'):
            directory = self.root / 'modules' / part
            directory.mkdir(parents=True, exist_ok=True)
            (directory / '__init__.py').write_text('')
        (directory / 'pod_reset.py').write_text("print('{\"reset\":\"ok\"}')\n")

    def local(self, path):
        return self.root / path.lstrip('/')

    def execute(self, command):
        # Mirror only the fake's declared files, never host files/credentials.
        for path, data in self.pod.files.items():
            if not path.startswith('/'):
                continue
            target = self.local(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for name in ('ready.json', 'prepared-connection.json'):
            target = self.local('/root/astrid/' + name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{}')
        if self.pod.active_job is None:
            self.local('/root/astrid/active-job.json').unlink(missing_ok=True)
        import base64
        generation = ' generate ' in command and '--placement local' in command
        def mapped(text):
            for prefix in ('/root/astrid', '/workspace'):
                text = text.replace(prefix, str(self.local(prefix)))
            return text.replace('/opt/astrid/bin/', str(self.bin) + '/')
        # Watchdog source is a nested base64 program. Execute it too, with all
        # paths in scratch. The closed environment below contains no provider
        # identity, so its existing _identity gate refuses before any API call.
        if '__pod_watchdog__' in command:
            for encoded in re.findall(r"[A-Za-z0-9+/=]{100,}", command):
                program = base64.b64decode(encoded).decode()
                command = command.replace(encoded, base64.b64encode(mapped(program).encode()).decode())
        if 'command.sh' in command and 'base64 -d' in command:
            encoded = re.search(r'printf %s ([A-Za-z0-9+/=]+)', command)[1]
            child = base64.b64decode(encoded).decode()
            generation = ' generate ' in child and '--placement local' in child
            command = command.replace(encoded, base64.b64encode(mapped(child).encode()).decode())
        command = mapped(command)
        env = {'PATH': str(self.bin) + ':/usr/bin:/bin', 'HOME': str(self.root),
               'PYTHONPATH': str(self.root / 'modules')}
        result = self.budget.run_pod(command, cwd=self.root, env=env)
        if generation:
            directory = re.search(r'^d=(.+)$', command, re.MULTILINE)[1]
            exit_path = Path(directory) / 'exit'
            assert result.returncode == 0 and exit_path.read_text().strip() == '0', (
                f'CPU shell job failed: {result.stderr}\n{(Path(directory) / "stderr").read_text()}')
        return result

    def handle(self, request):
        import base64
        import hashlib

        import httpx

        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content and method == 'POST' else {}
        if path == '/jobs' and method == 'POST':
            command = body['command']
            self.execute(command)
            if 'command.sh' in command and 'base64 -d' in command:
                encoded = re.search(r'printf %s ([A-Za-z0-9+/=]+)', command)[1]
                child = base64.b64decode(encoded).decode()
                directory = re.search(r'^d=(.+)$', command, re.MULTILINE)[1]
                job_id = directory.rsplit('/', 1)[-1]
                self.pod.job_start(child, job_id=job_id)
                for name, value in {'status': b'exited', 'exit': str(self.pod.jobs[job_id]).encode(), 'pid': b'12345'}.items():
                    self.pod.files[directory + '/' + name] = value
                code, stdout, stderr = 0, '', ''
            else:
                code, stdout, stderr = self.pod.exec(command)
            self.jobs[body['id']] = {'status': 'exited', 'exit_code': code,
                                     'stdout': base64.b64encode(stdout.encode()).decode(),
                                     'stderr': base64.b64encode(stderr.encode()).decode(),
                                     'stdout_offset': len(stdout.encode()), 'stderr_offset': len(stderr.encode()),
                                     'stdout_eof': True, 'stderr_eof': True}
            value = {'id': body['id']}
        elif path.startswith('/jobs/'):
            value = self.jobs[path.rsplit('/', 1)[-1]] if method == 'GET' else {}
        elif path == '/uploads':
            upload_id = str(len(self.uploads))
            self.uploads[upload_id] = {**body, 'data': b''}
            value = {'id': upload_id, 'offset': 0, 'complete': body['size'] == 0}
            if value['complete']:
                self.pod.files[body['path']] = b''
        elif path.startswith('/uploads/'):
            state = self.uploads[path.rsplit('/', 1)[-1]]
            if method == 'PUT':
                assert int(request.url.params['offset']) == len(state['data'])
                state['data'] += request.content
            complete = len(state['data']) == state['size']
            if complete:
                assert hashlib.sha256(state['data']).hexdigest() == state['sha256']
                self.pod.files[state['path']] = state['data']
            value = {'offset': len(state['data']), 'complete': complete}
        elif path == '/download-chunk':
            from runpod_lifecycle.https import CHUNK
            if body['path'] not in self.pod.files:
                return httpx.Response(400, json={'error': 'missing'})
            data = self.pod.files[body['path']]
            chunk = data[body['offset']:body['offset'] + CHUNK]
            value = {'offset': body['offset'], 'total': len(data), 'etag': hashlib.sha256(data).hexdigest(),
                     'sha256': hashlib.sha256(chunk).hexdigest(), 'data': base64.b64encode(chunk).decode()}
        else:
            raise AssertionError(f'unexpected CPU agent request: {method} {path}')
        return httpx.Response(200, json=value)
