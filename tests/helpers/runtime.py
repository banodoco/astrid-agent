"""The one way a test reaches a real Runtime (testing plan AT-1).

* ``runtime_checkout()`` is the only locator. It returns the Runtime source tree, found through
  ``ASTRID_RUNTIME_CHECKOUT`` or else the ``astrid-runtime`` sibling of this checkout (or of the
  main checkout, when this is a linked worktree). A missing Runtime fails in a lane that runs the
  ``integration`` tests and skips the module elsewhere (R-6). ``locate_runtime()`` is the same search without
  that verdict, for code that must still import when no Runtime is present.
* ``commit=`` runs the Runtime as of one commit (``git archive`` of the located tree, extracted once per
  process) instead of the working tree. ``vendored_runtime_commit()`` is the commit the vendored workspace
  client was produced from, so the schema digests agree.
* ``use_runtime()`` installs the lock-selected Runtime and client into the test interpreter, then
  puts the source tree on ``sys.path`` so parent-process fixtures can import ``runtime_protocol``.
* The ``real_runtime`` fixture (``tests/conftest.py``) starts a daemon on a temp realm through
  ``new_runtime_daemon`` and stops it at teardown. Do not construct ``RuntimeDaemon`` in a test.

One process imports one Runtime tree: the first ``use_runtime`` wins, so every test of a run shares it.
"""

from __future__ import annotations

import atexit
import importlib.metadata
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from tests.lanes import unavailable

ENV = "ASTRID_RUNTIME_CHECKOUT"
ASTRID = Path(__file__).resolve().parents[2]
_ARCHIVES: dict[str, Path] = {}
_exposed: Path | None = None
_child_runtime_provisioned = False


def vendored_runtime_commit() -> str:
    """The Runtime commit the vendored workspace client was produced from."""
    lock = ASTRID / "runtime.lock.json"
    return json.loads(lock.read_text(encoding="utf-8"))["source_commit"]


def _candidates() -> list[Path]:
    override = os.environ.get(ENV)
    if override:
        return [Path(override).expanduser()]
    roots = [ASTRID.parent]
    try:  # a linked worktree lives elsewhere; its main checkout's siblings are searched too
        common = subprocess.run(
            ["git", "-C", str(ASTRID), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            check=False, capture_output=True, text=True,
        )
    except OSError:
        common = None
    if common is not None and common.returncode == 0 and common.stdout.strip():
        roots.append(Path(common.stdout.strip()).parent.parent)
    return [root / "astrid-runtime" for root in dict.fromkeys(roots)]


def locate_runtime() -> Path | None:
    """The Runtime source tree, or None when there is none."""
    for candidate in _candidates():
        if (candidate / "runtime_protocol").is_dir():
            return candidate.resolve()
    return None


def runtime_checkout(commit: str | None = None) -> Path:
    """The Runtime source tree (as of ``commit`` when given); the one locator tests use."""
    root = locate_runtime()
    if root is None:
        unavailable(f"no Runtime checkout: set {ENV} to one (looked at {[str(c) for c in _candidates()]})")
    if commit is None:
        return root
    if commit not in _ARCHIVES:
        pinned = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{commit}^{{commit}}"],
                                check=False, capture_output=True)
        archive = subprocess.run(["git", "-C", str(root), "archive", "--format=tar", commit],
                                 check=False, capture_output=True) if pinned.returncode == 0 else pinned
        if archive.returncode != 0:
            unavailable(f"the Runtime checkout {root} has no commit {commit}")
        target = Path(tempfile.mkdtemp(prefix="astrid-runtime-archive-"))
        atexit.register(shutil.rmtree, target, ignore_errors=True)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as tar:
            tar.extractall(target, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
        _ARCHIVES[commit] = target
    return _ARCHIVES[commit]


def _ensure_runtime_installed_for_child() -> None:
    """Install the lock-selected Runtime into ``sys.executable`` for sanitized workers.

    A source path added to this test process cannot reach a child that deliberately drops ambient
    PYTHONPATH. Use the canonical CI installer so the test interpreter's site-packages contain
    Runtime and its client before a real worker starts.
    """
    global _child_runtime_provisioned
    if _child_runtime_provisioned:
        return

    commit = vendored_runtime_commit()
    pinned_archive = f"https://github.com/banodoco/astrid-runtime/archive/{commit}.tar.gz"
    installed_from_pin = True
    for distribution in ("astrid-runtime", "astrid-runtime-client"):
        try:
            direct_url = importlib.metadata.distribution(distribution).read_text("direct_url.json")
        except importlib.metadata.PackageNotFoundError:
            installed_from_pin = False
            break
        try:
            origin = json.loads(direct_url).get("url") if direct_url else None
        except (TypeError, json.JSONDecodeError):
            origin = None
        if origin != pinned_archive:
            installed_from_pin = False
            break
    if not installed_from_pin:
        subprocess.run([sys.executable, "-m", "pip", "uninstall", "--yes",
                        "astrid-runtime", "astrid-runtime-client"], check=True)
        installer = ASTRID / "scripts" / "ci_install_runtime.py"
        subprocess.run([sys.executable, str(installer)], cwd=ASTRID, check=True)
    _child_runtime_provisioned = True


def use_runtime(commit: str | None = None) -> Path:
    """Locate the Runtime and make ``runtime_protocol`` importable from it.

    Without ``commit`` an already exposed tree is reused, so a pinned module and the fixtures it uses agree.
    """
    global _exposed
    _ensure_runtime_installed_for_child()
    if commit is None and _exposed is not None:
        return _exposed
    root = runtime_checkout(commit)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    _exposed = _exposed or root
    return root


def initialize_runtime_realm(root: str | Path) -> Path:
    """Provision a missing fixture root through Runtime's canonical creator."""
    root = Path(root)
    if not root.exists():
        from runtime_protocol.store import RealmStore

        RealmStore.initialize(root).close()
    return root


def new_runtime_daemon(root: str | Path, *, commit: str | None = None, **kwargs: Any) -> Any:
    """A daemon, not yet started, on a realm provisioned through Runtime's creator."""
    use_runtime(commit)
    from runtime_protocol.daemon import RuntimeDaemon

    initialize_runtime_realm(root)
    return RuntimeDaemon(root, **kwargs)


EXECUTOR_SCOPES = ("handshake", "worker:execute", "tasks:read", "objects:read", "objects:write")


def executor_token(daemon: Any, executor_id: str) -> str:
    """A bearer bound to one executor id, without owner or registration scope.

    Since Runtime 8c9b77d every daemon hands out only the scoped shared worker credential
    (``daemon.worker_token``, actor ``astrid-pack-host``); it operates only an executor of that same id.
    A test that plays another executor provisions its own bound credential, as the Runtime's
    ``tests/runtime_credentials.py`` does, and registers the executor with the owner token.
    """
    token, _path = daemon.credentials.provision(executor_id, list(EXECUTOR_SCOPES))
    return token
