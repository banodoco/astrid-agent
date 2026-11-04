"""Working-copy persistence across real processes: restart, crashes, partial writes, two writers, stale bases, discard.

Every "process" in this file is a child interpreter (``worker_main``) that keeps its own ``Checkout`` in memory
until it exits, so the only thing two writers share is the working-copy file on disk. Each worker:

- uses a temp data root (``ASTRID_RUNTIME_DATA_ROOT``) and a temp ``ASTRID_HOME``;
- opens the timeline through a fake client that reads a head file the test controls;
- refuses to open the live runtime (``AstridClient.open_from_launcher`` raises), and stubs the publish
  transport, so publish runs its real three-way guard and stops at the stubbed write.
"""
from __future__ import annotations

import copy
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

THIS_FILE = Path(__file__).resolve()
REPO = THIS_FILE.parents[2]
PUBLISHABLE = REPO / "tests" / "fixtures" / "timeline_editing" / "tiny_publishable.json"
TIMEOUT = 120  # seconds a worker may take to answer one request before the test calls it hung
CRASH_EXIT = 9

from astrid.sdk.timeline_checkout import Checkout, TimelineEditError, draft_path  # noqa: E402

CUT_A = "c1"  # the first cut, named as the sheet and show print it
BOOT = (
    "import importlib.util, sys\n"
    "spec = importlib.util.spec_from_file_location('tp_persist', sys.argv[1])\n"
    "mod = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(mod)\n"
    "mod.worker_main()\n"
)


class Refused(Exception):
    """The worker's Checkout refused the operation (the message is what the user would read)."""


# ---------------------------------------------------------------- the worker (runs in a child process)

class _FakeTimelines:
    """Unit adapter for the file-backed head; it is not Runtime public authority."""

    def __init__(self, head_file: str):
        self.head_file = Path(head_file)

    def _head(self) -> dict:
        return json.loads(self.head_file.read_text(encoding="utf-8"))

    def open_composition(self, project, timeline, **_kw):
        head = self._head()
        bundle = head["bundle"]
        return SimpleNamespace(ok=True, error="", data={
            "native_inspection": {"project_id": bundle["project_id"], "timeline_id": bundle["timeline_id"]},
            "summary": {"head_revision_id": head["revision_id"],
                        "head_publication_id": head["publication_id"]}})

    def history(self, project, timeline, *, cursor=None, limit=50):
        """Expose the current public head/history pair used by ``fetch_bundle``."""
        if cursor is not None or limit < 1:
            return SimpleNamespace(ok=False, error="unsupported history page", data=None)
        head = self._head()
        pin = {key: head[key] for key in ("revision_id", "publication_id")}
        return SimpleNamespace(ok=True, error="", data={
            "head": pin,
            "items": [{
                "publication_id": pin["publication_id"],
                "revision_id": pin["revision_id"],
                "previous": None,
                "receipt": {"receipt_id": pin["publication_id"]},
                "intent": "bootstrap",
                "records": None,
                "provider_version": None,
                "compiler_version": None,
                "publisher": None,
            }],
            "next_cursor": None,
        })

    def open_bundle(self, project, timeline, revision_id=None, publication_id=None):
        head = self._head()
        if publication_id is not None and revision_id is None:
            return SimpleNamespace(ok=False, error="publication requires its revision", data=None)
        if revision_id and revision_id != head["revision_id"]:
            return SimpleNamespace(ok=False, error=f"no revision {revision_id}", data=None)
        if publication_id and publication_id != head["publication_id"]:
            return SimpleNamespace(ok=False, error=f"no publication {publication_id}", data=None)
        bundle = copy.deepcopy(head["bundle"])
        if publication_id is not None:
            bundle.setdefault("base_parent", {})["publication_id"] = publication_id
        return SimpleNamespace(ok=True, error="", data={
            "bundle": bundle,
            "project_id": bundle["project_id"],
            "timeline_id": bundle["timeline_id"],
            "revision_id": head["revision_id"],
            "publication_id": head["publication_id"],
        })


def worker_main() -> None:
    """Serve JSON-line requests on stdin; answer on the real stdout. Library prints go to stderr."""
    proto = sys.stdout
    sys.stdout = sys.stderr
    from astrid.core.timeline import authoring_bundle
    from astrid.sdk import AstridClient, autobootstrap, workspace_client
    from astrid.sdk import timeline_checkout as tc

    def no_live_runtime(*_a, **_k):
        raise RuntimeError("persistence tests must never reach the live runtime")

    AstridClient.open_from_launcher = classmethod(no_live_runtime)
    tc.Checkout.SAY_SAVED = False
    publish_seen = {"candidate": None, "runtime_touched": False}

    def stub_ensure_runtime(**_kw):
        publish_seen["runtime_touched"] = True
        return {"endpoint": "http://127.0.0.1:9", "credential_file": "/nonexistent"}

    def stub_publish(candidate, _writer, *, idempotency_key):
        publish_seen["candidate"] = json.loads(json.dumps(candidate, default=str))
        return {"publication": {"new_head": "rev-published"}}

    class _StubTransport:
        def __init__(self, *_a):
            pass

    autobootstrap.ensure_runtime = stub_ensure_runtime
    authoring_bundle.publish_authoring_candidate = stub_publish
    workspace_client.resolve_runtime_connection = lambda endpoint, cred: (endpoint, "t")
    workspace_client.WorkspaceClient = _StubTransport

    client = SimpleNamespace(timelines=_FakeTimelines(os.environ["TP_HEAD"]))
    tl = {}

    def handle(op: str, req: dict):
        if op == "draft":
            tl["h"] = tc.Checkout.draft("p", "t", client=client)
            return {"path": str(tl["h"].path), "name": tl["h"].draft_name}
        if op == "find":
            found = tc.find_draft("p", "t", client=client)
            return str(found) if found else None
        h = tl["h"]
        if op == "state":
            return {
                "base": h.base_revision,
                "clips": {c.id: {"start": round(c.start, 3), "params": dict(c.params)} for c in h.clips()},
                "notes": h.cut_notes_all(),
                "merged": list(h.merged),
            }
        if op == "nudge":
            h.clip(req["clip"]).nudge(req["seconds"])
            return None
        if op == "set":
            h.clip(req["clip"]).set(**req["params"])
            return None
        if op == "note":
            h.set_cut_note(req["cut"], why=req["why"])
            return None
        if op == "save":
            return str(h.save(quiet=True, force=bool(req.get("force"))))
        if op == "save_dying":  # die inside the save: lock held, temp file written, target not replaced
            os.replace = lambda *_a, **_k: os._exit(CRASH_EXIT)
            h.save(quiet=True)
            return None
        if op == "undo":
            return h.undo(int(req.get("steps", 1)))
        if op == "discard":
            h.discard()
            return None
        if op == "candidate":
            return {"bundle": json.loads(json.dumps(h.bundle)), "path": str(h.path) if h.path else None}
        if op == "publish":
            receipt = h.publish("persistence test", client=client, force=bool(req.get("force")))
            return {"merged": receipt.get("merged"), "new_head": receipt.get("new_head"),
                    "candidate": publish_seen["candidate"], "runtime_touched": publish_seen["runtime_touched"]}
        raise ValueError(f"unknown op {op!r}")

    for line in sys.stdin:
        req = json.loads(line)
        op = req.pop("op")
        try:
            reply = {"ok": True, "result": handle(op, req)}
        except Exception as exc:  # the parent reads this as a refusal or failure
            import traceback

            traceback.print_exc()  # into the worker's stderr log, which a failing test shows
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        proto.write(json.dumps(reply, default=str) + "\n")
        proto.flush()


# ---------------------------------------------------------------- the parent side

class Writer:
    """One child process holding one working-copy handle. Use as a context manager for a clean exit."""

    def __init__(self, world: SimpleNamespace, name: str):
        self.world = world
        self.name = name
        self.rc: int | None = None
        self.err_path = world.tmp / f"{name}-{len(world.writers)}.err"
        self._err = open(self.err_path, "w", encoding="utf-8")
        env = {**os.environ, "ASTRID_RUNTIME_DATA_ROOT": str(world.root), "TP_HEAD": str(world.head),
               "ASTRID_HOME": str(world.tmp / "astrid-home"), "ASTRID_WORKSPACE_CONFIG_DIR": str(world.tmp / "ws")}
        self.proc = subprocess.Popen(
            [sys.executable, "-c", BOOT, str(THIS_FILE), "worker"], cwd=str(REPO), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._err, text=True, encoding="utf-8")
        world.writers.append(self)

    def send(self, op: str, **kw):
        """The worker's reply dict, or None if the process exited without answering."""
        try:
            self.proc.stdin.write(json.dumps({"op": op, **kw}) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            return None
        ready, _, _ = select.select([self.proc.stdout], [], [], TIMEOUT)
        if not ready:
            self.proc.kill()
            raise AssertionError(f"{self.name}: worker hung on {op!r}")
        line = self.proc.stdout.readline()
        return json.loads(line) if line else None

    def do(self, op: str, **kw):
        publisher = getattr(self.world, "publish_candidate", None)
        if op == "publish" and publisher is not None:
            reply = self.send("candidate")
            if reply is None:
                raise AssertionError(f"{self.name}: worker exited during 'candidate': {self.stderr()}")
            if not reply["ok"]:
                raise Refused(reply["error"])
            return publisher(reply["result"])
        reply = self.send(op, **kw)
        if reply is None:
            raise AssertionError(f"{self.name}: worker exited during {op!r}: {self.stderr()}")
        if not reply["ok"]:
            raise Refused(reply["error"])
        return reply["result"]

    def close(self, expect_exit: int | None = 0) -> int:
        if self.rc is None:
            if self.proc.poll() is None:
                self.proc.stdin.close()
            self.rc = self.proc.wait(timeout=60)
            if expect_exit is not None and self.rc != expect_exit:
                raise AssertionError(f"{self.name} exited {self.rc}: {self.stderr()}")
            self._err.close()
        return self.rc

    def stderr(self) -> str:
        self._err.flush()
        return self.err_path.read_text(encoding="utf-8")[-2000:]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close(expect_exit=None if exc[0] else 0)


@pytest.fixture
def world(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(root))  # the parent computes draft paths from the same root
    head = tmp_path / "head.json"
    head.write_text(json.dumps({"revision_id": "rev-0", "publication_id": "pub-0",
                                "bundle": timeline()}), encoding="utf-8")
    w = SimpleNamespace(tmp=tmp_path, root=root, head=head, writers=[])
    w.writer = lambda name: Writer(w, name)
    w.working_copy = lambda: draft_path(
        json.loads(head.read_text(encoding="utf-8"))["bundle"]["project_id"],
        json.loads(head.read_text(encoding="utf-8"))["bundle"]["timeline_id"], "main")
    w.move_head = lambda rev, edit: _move_head(head, rev, edit)
    w.read_state = lambda: _read_state(w)
    yield w
    for writer in w.writers:  # never leave a child behind, even when an assertion failed
        if writer.proc.poll() is None:
            writer.proc.kill()
            writer.proc.wait(timeout=30)
        if not writer._err.closed:
            writer._err.close()


@pytest.fixture
def genuine_runtime(world, tmp_path):
    """Bind one persistence publication to the current Runtime commit/public read path."""
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate
    from astrid.sdk.contracts import DomainResult, ErrorObject
    from astrid.sdk.remote import RemoteTimelines
    from astrid.sdk.workspace_client import WorkspaceClient
    from tests.helpers.runtime import use_runtime
    from tests.helpers.trusted_seed import seed_trusted_parent

    use_runtime()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider

    runtime_root = tmp_path / "runtime-realm"
    RealmStore.initialize(runtime_root).close()
    service = RuntimeService(runtime_root)
    project = service.create_project({"slug": "persistence", "name": "Persistence"})["id"]
    service.create_timeline(project, "t", idempotency_key="create-persistence")

    bundle = timeline()
    old_media = {
        "P": "sha256:c57bc0c5ceafe2c7fff32d0d16a3a4b0b4bb7885238ac376c70dbf53bd669f44",
        "R": "sha256:7474f8a576b8a7b2da6b1e48dca004217b924a51c24424c072f57504dcfc35fb",
        "Q": "sha256:26ae3b4bffec466cdee5db4c6efcf5d1a3780b36d7449cf0b0952d71e2cf0b7d",
    }

    media = {}
    for name in ("P", "R", "Q"):
        media[name] = service.ingest(
            project, name.encode(), original_name=f"{name}.bin",
            media_type="application/octet-stream", idempotency_key=f"media-{name}",
        )["data"]["object_id"]

    def replace_media(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key == "media_id" and isinstance(child, str):
                    for name, digest in old_media.items():
                        if child == digest:
                            value[key] = media[name]
                            break
                else:
                    replace_media(child)
        elif isinstance(value, list):
            for child in value:
                replace_media(child)

    replace_media(bundle)
    _clip(bundle, "a-rocket")["app"].pop("on", None)
    publication = compile_authoring_candidate(bundle).publication
    publication.update(project_id=project, timeline_id="t", parent_revision_id="rev-0", expected_head=None)
    publication["parent_composition"]["registry"] = {}
    for occurrence in publication["parent_composition"]["occurrences"]:
        occurrence["track"] = "picture"
    for revision in publication["internal_timeline_revisions"]:
        revision["timeline_id"] = "t"

    service.register_timeline_commit_provider(load_timeline_commit_provider("astrid"))
    seed_trusted_parent(service, publication, idempotency_key="seed-persistence")

    def generated(name):
        def call(*args, **kwargs):
            if name == "get_project_parent_composition_revision":
                return service.get_project_parent_composition_revision(*args)
            if name == "get_project_shot_revision":
                return service.get_project_shot_revision(*args)
            if name == "get_project_timeline_revision":
                return service.get_project_timeline_revision(*args)
            if name == "list_timeline_commits":
                return service.list_timeline_commits(*args, authorize=lambda: None, **kwargs)
            if name == "commit_timeline":
                return service.commit_timeline(
                    *args, publisher="editor",
                    invocation_path=f"/v1/projects/{project}/timelines/t/commit",
                    authorize=lambda: None, **kwargs,
                )
            raise AssertionError(f"unexpected generated operation {name!r}")
        return call

    generated_client = SimpleNamespace(**{
        name: generated(name) for name in (
            "get_project_parent_composition_revision", "get_project_shot_revision",
            "get_project_timeline_revision", "list_timeline_commits", "commit_timeline",
        )
    })
    transport = WorkspaceClient.__new__(WorkspaceClient)
    transport._generated = generated_client
    transport._last_handshake = {"realm_id": service.realm["id"], "actor_id": "editor"}
    client = SimpleNamespace(
        _remote=SimpleNamespace(_transport=transport),
        timelines=RemoteTimelines(transport),
    )

    def resolve_timeline(requested_project, requested_timeline):
        if requested_project != project or requested_timeline != "t":
            return DomainResult.failure(ErrorObject("not_found", "timeline not found", {}))
        return DomainResult.success({"project_id": project, "timeline_id": "t"})

    client.timelines._resolve_timeline = resolve_timeline
    client.timelines.open_composition = lambda requested_project, requested_timeline, **_kw: (
        DomainResult.success({
            "native_inspection": {"project_id": project, "timeline_id": "t"},
            "summary": {"head_revision_id": service.get_project_timeline(project, "t")["head_revision_id"]},
        })
        if requested_project == project and requested_timeline == "t"
        else DomainResult.failure(ErrorObject("not_found", "timeline not found", {}))
    )

    head = client.timelines.history(project, "t", limit=200).data["head"]
    opened = client.timelines.open_bundle(
        project, "t", revision_id=head["revision_id"], publication_id=head["publication_id"],
    )
    assert opened.ok, opened.error
    current_bundle = opened.data["bundle"]
    world.head.write_text(json.dumps({"revision_id": head["revision_id"],
                                     "publication_id": head["publication_id"],
                                     "bundle": current_bundle}), encoding="utf-8")

    def publish_candidate(candidate):
        from astrid.sdk.timeline.sync import CommitConflict

        bundle = candidate["bundle"]
        saved_path = candidate.get("path")
        checkout = Checkout.load(saved_path) if saved_path else Checkout(bundle)
        assert checkout.bundle == bundle
        try:
            result = checkout.publish("persistence test", client=client, idempotency_key="persistence-test")
        except CommitConflict as exc:
            raise Refused(f"not published: someone published ({exc})") from exc
        latest = client.timelines.open_bundle(project, "t")
        assert latest.ok, latest.error
        moved = world.moved_head_revision
        return {
            "merged": f"merged onto head {moved}" if result.get("merged") else None,
            "new_head": result.get("new_head"), "candidate": latest.data["bundle"],
            "runtime_touched": True, "result": result,
        }

    def move_head(_revision, edit):
        from astrid.sdk.timeline.emitter import emit_document_commands

        history = client.timelines.history(project, "t", limit=200)
        current = history.data["head"]
        opened = client.timelines.open_bundle(
            project, "t", revision_id=current["revision_id"], publication_id=current["publication_id"],
        )
        assert opened.ok, opened.error
        before = opened.data["bundle"]
        candidate = copy.deepcopy(before)
        edit(candidate)
        commands = emit_document_commands(before, candidate)
        result = client.timelines.commit(
            project, "t", {
                "api_version": 1, "project_id": project, "timeline_id": "t",
                "base_revision_id": current["revision_id"],
                "base_publication_id": current["publication_id"],
                "commands": commands, "label": "persistence peer", "source": {"via": "python"},
                "merge": "atomic",
                "steps": [{"id": "edit", "label": "persistence peer", "intent": "",
                           "tags": [], "command_ordinals": list(range(len(commands))),
                           "patch": [change for call in commands for change in call["args"]["changes"]],
                           "reads": [{"kind": "scope", "ref": "timeline", "path": []}]}],
            }, idempotency_key="persistence-peer",
        )
        assert result.ok, result.error
        latest = client.timelines.open_bundle(project, "t")
        assert latest.ok, latest.error
        world.moved_head_revision = _revision
        world.head.write_text(json.dumps({"revision_id": _revision,
                                          "publication_id": result.as_dict()["receipt"]["receipt_id"],
                                          "bundle": latest.data["bundle"]}), encoding="utf-8")

    world.publish_candidate = publish_candidate
    world.move_head = move_head
    world.moved_head_revision = None
    try:
        yield SimpleNamespace(service=service, client=client, project=project, head=head,
                              bundle=current_bundle)
    finally:
        service.close()


def test_genuine_persistence_fixture_binds_exact_pair_and_refuses_bad_scope_without_writes(genuine_runtime):
    env = genuine_runtime
    history = env.client.timelines.history(env.project, "t", limit=200)
    head = history.data["head"]
    entry = next(row for row in history.data["items"] if row["publication_id"] == head["publication_id"])
    opened = env.client.timelines.open_bundle(
        env.project, "t", revision_id=head["revision_id"], publication_id=head["publication_id"],
    )
    assert opened.ok and opened.data["revision_id"] == head["revision_id"]
    bundle = opened.data["bundle"]
    document = bundle["source_mapping"]["_runtime_document"]
    assert bundle["base_parent"]["publication_id"] == head["publication_id"]
    assert document == entry["receipt"]["result"]["canonical_document"]
    assert document["parent_revision"]["content_digest"] == bundle["base_parent"]["content_digest"]
    assert {row["shot_id"] for row in document["shot_revisions"]} == {"A", "B"}
    assert {row["timeline_id"] for row in document["internal_timeline_revisions"]} == {"t"}

    before = tuple(env.service.store.conn.iterdump())
    refused = env.client.timelines.open_bundle("other-project", "t")
    assert not refused.ok
    missing = env.client.timelines.commit(
        env.project, "t", {"project_id": env.project, "timeline_id": "t"},
        idempotency_key="missing-fields",
    )
    assert not missing.ok
    assert tuple(env.service.store.conn.iterdump()) == before


def timeline() -> dict:
    """A publishable timeline (tiny_publishable.json): schema 1, a music bed under both shots, the pinned base a
    real head carries, cuts named c1 and c2. Publish runs its real check and three-way guard on it."""
    return json.loads(PUBLISHABLE.read_text(encoding="utf-8"))


HOLD = ("import sys, time\n"
        "from pathlib import Path\n"
        "from astrid.sdk.timeline_checkout import _file_lock\n"
        "lock = _file_lock(Path(sys.argv[1])); lock.__enter__()\n"
        "print('held', flush=True)\n"
        "time.sleep(120)\n")


def _move_head(head: Path, rev: str, edit) -> None:
    """Someone else published: the head is now ``rev``, the fixture timeline with ``edit`` applied."""
    doc = timeline()
    edit(doc)
    head.write_text(json.dumps({"revision_id": rev, "publication_id": f"pub-{rev}",
                                "bundle": doc}), encoding="utf-8")


def _read_state(w) -> dict:
    with w.writer("reader") as r:
        r.do("draft")
        return r.do("state")


def _clip(bundle_doc: dict, cid: str) -> dict:
    for shot in bundle_doc["shots"].values():
        for clip in shot["internal_timeline"]["clips"]:
            if clip["id"] == cid:
                return clip
    raise KeyError(cid)


# ---------------------------------------------------------------- 1. restart

def test_restart_a_fresh_process_sees_every_edit_and_the_undo_history(world):
    with world.writer("A") as a:
        a.do("draft")
        a.do("nudge", clip="a-rocket", seconds=0.5)
        a.do("save")
        a.do("set", clip="b-type", params={"text": "Now live."})
        a.do("save")
        a.do("note", cut=CUT_A, why="the rocket lands on Viral")
        a.do("save")

    with world.writer("B") as b:  # a fresh process: nothing from A is in memory
        opened = b.do("draft")
        state = b.do("state")
        assert Path(opened["path"]) == world.working_copy()
        assert state["base"] == "rev-0"
        assert state["clips"]["a-rocket"]["start"] == 1.8
        assert state["clips"]["b-type"]["params"]["text"] == "Now live."
        assert "the rocket lands on Viral" in [note["why"] for note in state["notes"].values()]

        assert len(b.do("undo")) == 1  # one edit taken back
        state = b.do("state")
        assert "the rocket lands on Viral" not in [note["why"] for note in state["notes"].values()]  # undone
        assert state["clips"]["b-type"]["params"]["text"] == "Now live."

        b.do("undo")  # the edit before that: the text goes back to A's previous value
        state = b.do("state")
        assert state["clips"]["b-type"]["params"]["text"] == "Live."
        assert state["clips"]["a-rocket"]["start"] == 1.8  # the move is an earlier edit and stays


# ---------------------------------------------------------------- 2. crash mid-save

def test_a_writer_that_dies_holding_the_lock_does_not_block_the_next_writer(world):
    with world.writer("A") as a:
        a.do("draft")
        a.do("nudge", clip="a-rocket", seconds=0.5)
        a.do("save")

    lock = world.working_copy().with_name(f".{world.working_copy().name}.lock")
    with world.writer("C") as c:
        c.do("draft")
        c.do("set", clip="b-type", params={"text": "crashed edit"})
        assert c.send("save_dying") is None  # dies inside the save, holding the flock
        assert c.close(expect_exit=CRASH_EXIT) == CRASH_EXIT

    assert lock.exists()  # the lock file outlives its holder...
    assert len(list(world.working_copy().parent.glob(".main.json.*.tmp"))) == 1  # ...and so does the half-finished temp

    with world.writer("B") as b:  # the kernel released the flock with C: B saves, it does not hang or refuse
        b.do("draft")
        state = b.do("state")
        assert state["clips"]["b-type"]["params"]["text"] == "Live."  # the last good save, not C's unsaved edit
        b.do("set", clip="b-type", params={"text": "from B"})
        b.do("save")

    # a stale lock file naming a dead pid is not an obstacle either: the lock is a flock, not a pid file
    lock.write_text("999999\n", encoding="utf-8")
    with world.writer("D") as d:
        d.do("draft")
        d.do("set", clip="b-type", params={"text": "from D"})
        d.do("save")
    assert _read_state(world)["clips"]["b-type"]["params"]["text"] == "from D"


def test_a_failed_working_copy_write_leaves_no_undo_step_for_the_edit_it_did_not_save(world, monkeypatch):
    """Regression: the undo step used to be written before the working copy. A save that died between the two left
    a step equal to the file on disk, so the first undo changed nothing and a second was needed."""
    path = world.working_copy()
    Checkout(timeline()).save(path, quiet=True)
    tl = Checkout.load(path)
    tl.clip("b-type").set(text="edit one")
    tl.save(quiet=True)
    tl.clip("b-type").set(text="edit two (its write fails)")

    real_replace = os.replace

    def failing_working_copy_write(src, dst, *args, **kwargs):
        if Path(dst) == path:
            raise OSError("simulated: the working copy could not be written")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", failing_working_copy_write)
    with pytest.raises(OSError, match="simulated"):
        tl.save(quiet=True)
    monkeypatch.setattr(os, "replace", real_replace)

    assert Checkout.load(path).clip("b-type").params["text"] == "edit one"  # the file still holds the last good save
    reopened = Checkout.load(path)
    assert reopened.undo(1) == ["c2.card.set(text='edit one')"]  # the one real step: it changes something
    assert Checkout.load(path).clip("b-type").params["text"] == "Live."
    with pytest.raises(TimelineEditError, match="nothing to undo"):
        reopened.undo(1)  # and there is no phantom second step


def test_a_live_holder_that_never_exits_is_waited_for_then_refused_by_name(world, monkeypatch):
    path = world.working_copy()
    Checkout(timeline()).save(path, quiet=True)
    tl = Checkout.load(path)
    holder = subprocess.Popen([sys.executable, "-c", HOLD, str(path)], cwd=str(REPO),
                              stdout=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        assert holder.stdout.readline().strip() == "held"  # the holder has the lock and its pid on it
        monkeypatch.setenv("ASTRID_TIMELINE_LOCK_TIMEOUT", "0.5")  # the real wait is 30 s; this test shortens it
        tl.clip("b-type").set(text="blocked edit")
        started = time.monotonic()
        with pytest.raises(TimelineEditError, match=rf"another writer \(pid {holder.pid}\) has held") as refused:
            tl.save(quiet=True)
        assert time.monotonic() - started < 10  # it gave up at the bound, it did not hang
        assert "nothing was saved" in str(refused.value)
        assert str(path.with_name(f".{path.name}.lock")) in str(refused.value)  # names the lock file
    finally:
        holder.kill()
        holder.wait(timeout=30)
    assert Checkout.load(path).clip("b-type").params["text"] == "Live."  # the refused edit never reached the file


# ---------------------------------------------------------------- 3. partial write

def test_a_truncated_temp_file_is_never_read_as_the_working_copy(world):
    with world.writer("A") as a:
        a.do("draft")
        a.do("nudge", clip="a-rocket", seconds=0.5)
        a.do("save")

    target = world.working_copy()
    good = target.read_text(encoding="utf-8")
    (target.parent / f".{target.name}.424242.tmp").write_text(good[: len(good) // 2], encoding="utf-8")  # cut off mid-write
    (target.parent / f".{target.name}.515151.tmp").write_text("", encoding="utf-8")  # or never written at all

    loaded = Checkout.load(target)  # the library reads the target, never a temp file
    assert loaded.clip("a-rocket").start == pytest.approx(1.8)
    assert target.read_text(encoding="utf-8") == good

    state = _read_state(world)
    assert state["clips"]["a-rocket"]["start"] == 1.8 and state["clips"]["a-rocket"]["params"]["x"] == 10


# ---------------------------------------------------------------- 4. two writers across processes

def test_two_writers_in_separate_processes_merge_different_clips_and_refuse_the_same_param(world):
    a = world.writer("A")
    b = world.writer("B")
    try:
        a.do("draft")
        b.do("draft")  # both load the same base
        a.do("nudge", clip="a-rocket", seconds=0.5)  # A edits clip X
        a.do("save")
        b.do("set", clip="b-type", params={"text": "Now live."})  # B edits clip Y
        b.do("save")
        assert any("merged with another writer's save" in m for m in b.do("state")["merged"])

        state = world.read_state()
        assert state["clips"]["a-rocket"]["start"] == 1.8  # A's move survived B's save
        assert state["clips"]["b-type"]["params"]["text"] == "Now live."  # and B's text survived A's

        a.do("draft")  # A reloads what B saved, then edits the SAME param B is about to edit, differently
        a.do("set", clip="b-type", params={"text": "from A"})
        a.do("save")
        b.do("set", clip="b-type", params={"text": "from B"})
        with pytest.raises(Refused, match="another writer.*both changed.*b-type"):
            b.do("save")
        assert world.read_state()["clips"]["b-type"]["params"]["text"] == "from A"  # A's edit is the one on disk
    finally:
        a.close(expect_exit=None)
        b.close(expect_exit=None)


# ---------------------------------------------------------------- 5. stale base after restart

def test_publish_after_a_restart_merges_other_clips_onto_the_moved_head(world, genuine_runtime):
    with world.writer("A") as a:
        a.do("draft")
        a.do("nudge", clip="a-rocket", seconds=0.5)
        a.do("save")
    world.move_head("rev-1", lambda b: _clip(b, "b-type")["params"].__setitem__("text", "LIVE!"))

    with world.writer("B") as b:
        b.do("draft")
        assert b.do("state")["base"] == "rev-0"  # the working copy still remembers the base it started from
        published = b.do("publish")

    assert published["runtime_touched"] is True  # the write is the stubbed transport, not the runtime
    assert published["merged"] and "merged onto head rev-1" in published["merged"]
    merged = Checkout(published["candidate"])
    assert merged.clip("a-rocket").start == pytest.approx(1.8)  # our move
    assert merged.clip("b-type").params["text"] == "LIVE!"  # their change, kept


@pytest.mark.usefixtures("genuine_runtime")
def test_publish_after_a_restart_refuses_a_clip_both_sides_changed(world):
    with world.writer("A") as a:
        a.do("draft")
        a.do("nudge", clip="a-rocket", seconds=0.5)
        a.do("save")
    world.move_head("rev-1", lambda b: _clip(b, "a-rocket")["params"].__setitem__("x", 99))

    with world.writer("B") as b:
        b.do("draft")
        with pytest.raises(Refused, match="not published: someone published"):
            b.do("publish")
        assert b.do("state")["clips"]["a-rocket"]["start"] == 1.8  # the working copy is untouched by the refusal
    assert json.loads(world.head.read_text(encoding="utf-8"))["revision_id"] == "rev-1"  # nothing was written


# ---------------------------------------------------------------- 6. discard persists

def test_a_discard_in_one_process_leaves_the_next_process_with_no_working_copy(world):
    with world.writer("A") as a:
        a.do("draft")
        a.do("nudge", clip="a-rocket", seconds=0.5)
        a.do("save")
        a.do("discard")
    assert not world.working_copy().exists()
    assert not world.working_copy().with_suffix(".history").exists()

    with world.writer("B") as b:
        assert b.do("find") is None  # no working copy to see
        b.do("draft")  # a new one starts from the published head, not from A's discarded edit
        state = b.do("state")
    assert state["clips"]["a-rocket"]["start"] == 1.3
    assert state["clips"]["a-rocket"]["params"]["x"] == 10
