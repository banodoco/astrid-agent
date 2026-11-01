"""Working-copy persistence across real processes: restart, crashes, partial writes, two writers, stale bases, discard.

Every "process" in this file is a child interpreter (``worker_main``) that keeps its own ``Checkout`` in memory
until it exits, so the only thing two writers share is the working-copy file on disk. Each worker:

- uses a temp data root (``ASTRID_RUNTIME_DATA_ROOT``) and a temp ``ASTRID_HOME``;
- opens the timeline through a fake client that reads a head file the test controls;
- refuses to open the live runtime (``AstridClient.open_from_launcher`` raises), and stubs the publish
  transport, so publish runs its real three-way guard and stops at the stubbed write.
"""
from __future__ import annotations

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
    """Reads the head from a file the test controls; the head is whatever the test last 'published'."""

    def __init__(self, head_file: str):
        self.head_file = Path(head_file)

    def _head(self) -> dict:
        return json.loads(self.head_file.read_text(encoding="utf-8"))

    def open_composition(self, project, timeline, **_kw):
        head = self._head()
        return SimpleNamespace(ok=True, error="", data={
            "native_inspection": {"project_id": "p", "timeline_id": "t"},
            "summary": {"head_revision_id": head["revision_id"]}})

    def open_bundle(self, project, timeline, revision_id=None):
        head = self._head()
        if revision_id and revision_id != head["revision_id"]:
            return SimpleNamespace(ok=False, error=f"no revision {revision_id}", data=None)
        return SimpleNamespace(ok=True, error="", data={"bundle": head["bundle"]})


def worker_main() -> None:
    """Serve JSON-line requests on stdin; answer on the real stdout. Library prints go to stderr."""
    proto = sys.stdout
    sys.stdout = sys.stderr
    from astrid.sdk import AstridClient, authoring_bundle, autobootstrap, workspace_client
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
    head.write_text(json.dumps({"revision_id": "rev-0", "bundle": timeline()}), encoding="utf-8")
    w = SimpleNamespace(tmp=tmp_path, root=root, head=head, writers=[])
    w.writer = lambda name: Writer(w, name)
    w.working_copy = lambda: draft_path("p", "t", "main")
    w.move_head = lambda rev, edit: _move_head(head, rev, edit)
    w.read_state = lambda: _read_state(w)
    yield w
    for writer in w.writers:  # never leave a child behind, even when an assertion failed
        if writer.proc.poll() is None:
            writer.proc.kill()
            writer.proc.wait(timeout=30)
        if not writer._err.closed:
            writer._err.close()


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
    head.write_text(json.dumps({"revision_id": rev, "bundle": doc}), encoding="utf-8")


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
    holder = subprocess.Popen([sys.executable, "-c", HOLD, str(path)], cwd=str(REPO),
                              stdout=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        assert holder.stdout.readline().strip() == "held"  # the holder has the lock and its pid on it
        monkeypatch.setenv("ASTRID_TIMELINE_LOCK_TIMEOUT", "0.5")  # the real wait is 30 s; this test shortens it
        tl = Checkout.load(path)
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

def test_publish_after_a_restart_merges_other_clips_onto_the_moved_head(world):
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
