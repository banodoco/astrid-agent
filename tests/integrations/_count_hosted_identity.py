"""Test-only guard for nested repository roots in hosted Agent checkouts."""
from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

from runtime_protocol.source_identity import SourceIdentityError


NESTED_REPOSITORIES = (
    "astrid-pinokio",
    "astrid-runtime",
    "comfyui-checkout",
    "runpod-lifecycle-checkout",
    "vibecomfy-checkout",
)


def _git(root: Path, *args: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise SourceIdentityError(f"cannot inspect test source checkout: {root}") from exc
    return result.stdout


def _checkout_state(root: Path, *, allow_nested: frozenset[bytes] = frozenset()) -> tuple[str, tuple[bytes, ...]]:
    root = root.resolve(strict=True)
    top = Path(_git(root, "rev-parse", "--show-toplevel").strip().decode()).resolve(strict=True)
    if top != root:
        raise SourceIdentityError(f"source path is not the root of its Git checkout: {root}")
    head = _git(root, "rev-parse", "--verify", "HEAD^{commit}").strip().decode("ascii")
    if len(head) not in (40, 64) or any(char not in "0123456789abcdef" for char in head):
        raise SourceIdentityError(f"Git returned an invalid full commit ID for {root}")
    rows = tuple(row for row in _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0") if row)
    dirty = []
    for row in rows:
        status, raw_path = row[:2], row[3:]
        if status == b"??" and raw_path.rstrip(b"/") in allow_nested:
            continue
        dirty.append(row)
    if dirty:
        raise SourceIdentityError(f"source checkout is dirty: {root}")
    return head, rows


@contextlib.contextmanager
def hosted_identity_scope(monkeypatch, source_identity, agent_root: Path, *, hosted: bool):
    """Verify nested roots and filter only those roots from Agent untracked paths.

    All ordinary untracked paths remain visible to Runtime's identity capture,
    which rejects them. Nested repositories are admitted only after their own
    clean Git root and HEAD have been independently verified.
    """
    agent_root = Path(agent_root).resolve(strict=True)
    component_roots = {name: agent_root / name for name in NESTED_REPOSITORIES}
    absent = [name for name, root in component_roots.items() if not root.exists()]
    if hosted and absent:
        raise SourceIdentityError(f"hosted source closure is missing nested repositories: {', '.join(absent)}")

    present = {name: root.resolve(strict=True) for name, root in component_roots.items() if root.exists()}
    names = {str(root.relative_to(agent_root)).encode(): name for name, root in present.items()}
    allow_nested = frozenset(names)
    agent_head, _ = _checkout_state(agent_root, allow_nested=allow_nested)
    heads = {"astrid-agent": agent_head}
    for name, root in present.items():
        heads[name] = _checkout_state(root)[0]
    initial = dict(heads)

    original = source_identity._untracked

    def guarded_untracked(root: Path) -> list[bytes]:
        paths = original(root)
        if Path(root).resolve() != agent_root:
            return paths
        # Revalidate both status and identity immediately before allowing a
        # nested checkout path to be omitted from the Agent's untracked list.
        current_agent_head, _ = _checkout_state(agent_root, allow_nested=allow_nested)
        if current_agent_head != initial["astrid-agent"]:
            raise SourceIdentityError("Agent HEAD changed during source identity capture")
        current = {"astrid-agent": current_agent_head}
        for name, nested in present.items():
            current[name] = _checkout_state(nested)[0]
        if current != initial:
            raise SourceIdentityError("nested source identities changed during capture")
        nested_paths = {path for path in paths if path.rstrip(b"/") in allow_nested}
        unexpected = [path for path in paths if path not in nested_paths]
        if unexpected:
            raise SourceIdentityError("Agent contains unrecognized untracked paths")
        return [path for path in paths if path not in nested_paths]

    with monkeypatch.context() as scoped:
        scoped.setattr(source_identity, "_untracked", guarded_untracked)
        try:
            yield heads
        finally:
            final = {"astrid-agent": _checkout_state(agent_root, allow_nested=allow_nested)[0]}
            for name, root in present.items():
                final[name] = _checkout_state(root)[0]
            if final != initial:
                raise SourceIdentityError("source identities changed during capture")
