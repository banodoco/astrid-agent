"""The extension registry: discovery, validation, precedence, quarantine, lazy entry, cache (M-Core, X-4).

Every test builds its packs under tmp_path and injects a fake ``discover``; the real home, the real
packs and the real project config are never read (``disabled_ids`` is patched to an empty set).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from astrid.core.extensions import registry as reg
from astrid.core.extensions.model import Quarantined
from astrid.core.extensions.registry import (
    Disabled,
    ExtensionRegistry,
    ExtensionUnavailable,
    Shadowed,
)
from astrid.core.extensions.scopes import Root

POINT_DATA = """description: a test point
version: 1
min_supported: 1
side: client
cardinality: many
effects_allowed: [none, read]
protocol: data
"""

POINT_PROTOCOL = """description: a protocol point
version: 1
min_supported: 1
side: client
cardinality: many
effects_allowed: [none, read]
protocol: proto.py:Op
"""

PROTOCOL_PY = """from typing import Protocol


class Op(Protocol):
    def apply(self, model, args):
        ...
"""


@dataclass(frozen=True)
class _Record:
    """Shaped like astrid.core.pack.discovery.DiscoveredPack: id, pack_dir, pack.version."""

    id: str
    pack_dir: Path
    version: str = "1.0.0"

    @property
    def pack(self) -> SimpleNamespace:
        return SimpleNamespace(version=self.version)


def _fake_discover(*records: _Record):
    calls: list[dict[str, Any]] = []

    def discover(**kwargs: Any) -> list[_Record]:
        calls.append(kwargs)
        return list(records)

    discover.calls = calls  # type: ignore[attr-defined]
    return discover


def _write(path: Path, text: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _contribution(
    *,
    api: int = 1,
    effects: str = "none",
    summary: str = "does a thing",
    status: str | None = None,
    entry: str | None = None,
) -> str:
    lines = [f"api: {api}", f"summary: {summary}", "example: run it", f"effects: {effects}"]
    if entry is not None:
        lines.append(f"entry: {entry}")
    if status is not None:
        lines.append(f"status: {status}")
    return "\n".join(lines) + "\n"


@pytest.fixture(autouse=True)
def _no_real_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reg, "disabled_ids", lambda: frozenset())


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Root]:
    built_in = tmp_path / "builtin"
    installed = tmp_path / "installed"
    user = tmp_path / "user"
    project = tmp_path / "project"
    for folder in (built_in, installed, user, project):
        folder.mkdir()
    return {
        "built-in": Root("built-in", built_in, True, "source tree"),
        "installed": Root("installed", installed, True, "--pack-root"),
        "user": Root("user", user, True, "user"),
        "project": Root("project", project, True, "<project>/.astrid/packs"),
    }


def _registry(roots: dict[str, Root], tmp_path: Path, discover: Any) -> ExtensionRegistry:
    return ExtensionRegistry(
        roots=list(roots.values()),
        sdk_root=tmp_path / "no-sdk" / "extensions",
        discover=discover,
    )


def _pack(root: Path, pack_id: str) -> Path:
    folder = root / pack_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_valid_point_and_contribution(roots, tmp_path):
    alpha = _pack(roots["installed"].path, "alpha")
    _write(alpha / "extensions" / "points" / "widget.yaml", POINT_DATA)
    _write(alpha / "extensions" / "alpha.widget" / "one.yaml", _contribution())
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("alpha", alpha, "2.0.0"))).load()

    assert [p.id for p in reg_.points()] == ["alpha.widget"]
    found = reg_.contributions("alpha.widget")
    assert [c.id for c in found] == ["alpha.one"]
    assert found[0].scope == "installed"
    assert found[0].version == "2.0.0"  # the pack version is the default
    assert reg_.problems() == []


def test_broken_yaml_is_quarantined_with_a_fix(roots, tmp_path):
    alpha = _pack(roots["installed"].path, "alpha")
    _write(alpha / "extensions" / "points" / "widget.yaml", POINT_DATA)
    _write(alpha / "extensions" / "alpha.widget" / "bad.yaml", "api: [1\n  summary: :\n")
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("alpha", alpha))).load()

    assert reg_.contributions("alpha.widget") == []
    quarantined = [p for p in reg_.problems() if isinstance(p, Quarantined)]
    assert [q.id for q in quarantined] == ["alpha.bad"]
    assert "yaml" in quarantined[0].error and quarantined[0].fix
    with pytest.raises(ExtensionUnavailable) as caught:
        reg_.resolve("alpha.widget", "alpha.bad")
    assert caught.value.reason == "quarantined"
    assert caught.value.fix == quarantined[0].fix


def test_broken_import_fails_at_entry_not_at_load(roots, tmp_path):
    alpha = _pack(roots["installed"].path, "alpha")
    marker = tmp_path / "imported.marker"
    _write(alpha / "extensions" / "points" / "widget.yaml", POINT_PROTOCOL)
    _write(alpha / "extensions" / "points" / "proto.py", "class Op:\n    pass\n")
    _write(
        alpha / "extensions" / "alpha.widget" / "boom.yaml",
        _contribution(effects="read", entry="boom.py"),
    )
    _write(
        alpha / "extensions" / "alpha.widget" / "boom.py",
        f"open({str(marker)!r}, 'w').close()\nraise RuntimeError('import failed')\n",
    )
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("alpha", alpha))).load()

    assert not marker.exists(), "load() must not import contribution code"
    assert [c.id for c in reg_.contributions("alpha.widget")] == ["alpha.boom"]
    contribution = reg_.resolve("alpha.widget", "boom")
    with pytest.raises(ExtensionUnavailable) as caught:
        reg_.entry(contribution)
    assert caught.value.reason == "quarantined"
    assert "import failed" in str(caught.value)
    assert marker.exists()
    assert any(isinstance(p, Quarantined) and p.id == "alpha.boom" for p in reg_.problems())


def test_bad_signature_fails_the_protocol_check(roots, tmp_path):
    alpha = _pack(roots["installed"].path, "alpha")
    _write(alpha / "extensions" / "points" / "op.yaml", POINT_PROTOCOL)
    _write(alpha / "extensions" / "points" / "proto.py", PROTOCOL_PY)
    _write(
        alpha / "extensions" / "alpha.op" / "good.yaml",
        _contribution(effects="read", entry="good.py"),
    )
    _write(
        alpha / "extensions" / "alpha.op" / "good.py",
        "def apply(model, args, **extra):\n    return 'ok'\n",
    )
    _write(
        alpha / "extensions" / "alpha.op" / "bad.yaml",
        _contribution(effects="read", entry="bad.py"),
    )
    _write(alpha / "extensions" / "alpha.op" / "bad.py", "def apply(model):\n    return 'no'\n")
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("alpha", alpha))).load()

    good = reg_.resolve("alpha.op", "good")
    assert reg_.entry(good).apply(1, 2, x=3) == "ok"
    with pytest.raises(ExtensionUnavailable) as caught:
        reg_.entry(reg_.resolve("alpha.op", "bad"))
    assert caught.value.reason == "quarantined"
    assert "protocol" in str(caught.value)
    assert "(model, args)" in caught.value.fix


def test_incompatible_api_is_reported_as_incompatible(roots, tmp_path):
    alpha = _pack(roots["installed"].path, "alpha")
    _write(alpha / "extensions" / "points" / "widget.yaml", POINT_DATA)
    _write(alpha / "extensions" / "alpha.widget" / "future.yaml", _contribution(api=9))
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("alpha", alpha))).load()

    assert reg_.contributions("alpha.widget") == []
    with pytest.raises(ExtensionUnavailable) as caught:
        reg_.resolve("alpha.widget", "future")
    assert caught.value.reason == "incompatible"
    assert "api" in caught.value.fix


def test_unknown_point_lists_the_known_points(roots, tmp_path):
    alpha = _pack(roots["installed"].path, "alpha")
    _write(alpha / "extensions" / "points" / "widget.yaml", POINT_DATA)
    _write(alpha / "extensions" / "ghost.thing" / "lost.yaml", _contribution())
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("alpha", alpha))).load()

    lost = [p for p in reg_.problems() if isinstance(p, Quarantined) and p.id == "alpha.lost"]
    assert len(lost) == 1
    assert "unknown point 'ghost.thing'" in lost[0].error
    assert "known points: alpha.widget" in lost[0].error


def test_broken_point_quarantines_its_contributions(roots, tmp_path):
    alpha = _pack(roots["installed"].path, "alpha")
    _write(alpha / "extensions" / "points" / "widget.yaml", "description: missing fields\n")
    _write(alpha / "extensions" / "alpha.widget" / "one.yaml", _contribution())
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("alpha", alpha))).load()

    assert reg_.points() == []
    ids = {p.id for p in reg_.problems() if isinstance(p, Quarantined)}
    assert {"alpha.widget", "alpha.one"} <= ids


def test_project_shadows_installed_short_name_is_reported(roots, tmp_path):
    ia = _pack(roots["installed"].path, "ia")
    pa = _pack(roots["project"].path, "pa")
    _write(ia / "extensions" / "points" / "surface.yaml", POINT_DATA)
    _write(ia / "extensions" / "ia.surface" / "grain.yaml", _contribution())
    _write(pa / "extensions" / "ia.surface" / "grain.yaml", _contribution())
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("ia", ia), _Record("pa", pa))).load()

    assert reg_.resolve("ia.surface", "grain").id == "pa.grain"
    assert reg_.resolve("ia.surface", "ia.grain").id == "ia.grain"
    shadows = [p for p in reg_.problems() if isinstance(p, Shadowed)]
    assert shadows == [
        Shadowed(
            id="ia.grain",
            point="ia.surface",
            winner="pa.grain",
            loser_scope="installed",
            loser_path=ia / "extensions" / "ia.surface" / "grain.yaml",
        )
    ]


def test_contribution_named_like_a_builtin_keeps_the_builtin_short_name(roots, tmp_path):
    core = _pack(roots["built-in"].path, "core")
    proj = _pack(roots["project"].path, "proj")
    _write(core / "extensions" / "points" / "op.yaml", POINT_DATA)
    _write(core / "extensions" / "core.op" / "opacity.yaml", _contribution())
    _write(proj / "extensions" / "core.op" / "opacity.yaml", _contribution())
    reg_ = _registry(
        roots, tmp_path, _fake_discover(_Record("core", core), _Record("proj", proj))
    ).load()

    assert reg_.resolve("core.op", "opacity").id == "core.opacity"
    assert reg_.resolve("core.op", "proj.opacity").id == "proj.opacity"
    assert (
        Shadowed(
            id="proj.opacity",
            point="core.op",
            winner="core.opacity",
            loser_scope="project",
            loser_path=proj / "extensions" / "core.op" / "opacity.yaml",
        )
        in reg_.problems()
    )


def test_disabled_is_listed_and_refused(roots, tmp_path, monkeypatch):
    alpha = _pack(roots["installed"].path, "alpha")
    _write(alpha / "extensions" / "points" / "widget.yaml", POINT_DATA)
    _write(alpha / "extensions" / "alpha.widget" / "one.yaml", _contribution())
    _write(alpha / "extensions" / "alpha.widget" / "two.yaml", _contribution())
    monkeypatch.setattr(reg, "disabled_ids", lambda: frozenset({"alpha.one"}))
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("alpha", alpha))).load()

    assert [c.id for c in reg_.contributions("alpha.widget")] == ["alpha.two"]
    disabled = [p for p in reg_.problems() if isinstance(p, Disabled)]
    assert [d.id for d in disabled] == ["alpha.one"]
    with pytest.raises(ExtensionUnavailable) as caught:
        reg_.resolve("alpha.widget", "one")
    assert caught.value.reason == "disabled"
    assert "[extensions] disable" in caught.value.fix


def test_experimental_is_hidden_unless_asked_but_still_resolves(roots, tmp_path):
    alpha = _pack(roots["installed"].path, "alpha")
    _write(alpha / "extensions" / "points" / "widget.yaml", POINT_DATA)
    _write(alpha / "extensions" / "alpha.widget" / "steady.yaml", _contribution())
    _write(
        alpha / "extensions" / "alpha.widget" / "trial.yaml", _contribution(status="experimental")
    )
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("alpha", alpha))).load()

    assert [c.id for c in reg_.contributions("alpha.widget")] == ["alpha.steady"]
    both = reg_.contributions("alpha.widget", include_experimental=True)
    assert [c.id for c in both] == ["alpha.steady", "alpha.trial"]
    assert reg_.resolve("alpha.widget", "trial").id == "alpha.trial"


def test_resolve_by_short_and_qualified_name_and_unknown(roots, tmp_path):
    alpha = _pack(roots["installed"].path, "alpha")
    _write(alpha / "extensions" / "points" / "widget.yaml", POINT_DATA)
    _write(alpha / "extensions" / "alpha.widget" / "one.yaml", _contribution())
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("alpha", alpha))).load()

    assert reg_.resolve("alpha.widget", "one") is reg_.resolve("alpha.widget", "alpha.one")
    with pytest.raises(ExtensionUnavailable) as caught:
        reg_.resolve("alpha.widget", "nothing")
    assert caught.value.reason == "unknown"
    assert "one" in caught.value.fix


def test_entry_of_a_data_only_contribution_is_none(roots, tmp_path):
    alpha = _pack(roots["installed"].path, "alpha")
    _write(alpha / "extensions" / "points" / "widget.yaml", POINT_DATA)
    _write(alpha / "extensions" / "alpha.widget" / "one.yaml", _contribution())
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("alpha", alpha))).load()

    assert reg_.entry(reg_.resolve("alpha.widget", "one")) is None


def test_duplicate_qualified_id_across_scopes_keeps_the_highest_scope(roots, tmp_path):
    installed = _pack(roots["installed"].path, "dup")
    project = _pack(roots["project"].path, "dup")
    _write(installed / "extensions" / "points" / "widget.yaml", POINT_DATA)
    _write(installed / "extensions" / "dup.widget" / "one.yaml", _contribution(summary="old"))
    _write(project / "extensions" / "dup.widget" / "one.yaml", _contribution(summary="new"))
    reg_ = _registry(
        roots, tmp_path, _fake_discover(_Record("dup", installed), _Record("dup", project))
    ).load()

    found = reg_.contributions("dup.widget")
    assert [c.summary for c in found] == ["new"]
    assert found[0].scope == "project"
    assert any(isinstance(p, Shadowed) and p.id == "dup.one" for p in reg_.problems())


def test_mtime_cache_skips_reparse_until_a_manifest_changes(roots, tmp_path, monkeypatch):
    alpha = _pack(roots["installed"].path, "alpha")
    _write(alpha / "extensions" / "points" / "widget.yaml", POINT_DATA)
    one = _write(alpha / "extensions" / "alpha.widget" / "one.yaml", _contribution())
    _write(alpha / "extensions" / "alpha.widget" / "two.yaml", _contribution())
    counts = {"point": 0, "contribution": 0}
    real_point, real_contribution = reg.parse_point, reg.parse_contribution

    def counting_point(*args: Any, **kwargs: Any):
        counts["point"] += 1
        return real_point(*args, **kwargs)

    def counting_contribution(*args: Any, **kwargs: Any):
        counts["contribution"] += 1
        return real_contribution(*args, **kwargs)

    monkeypatch.setattr(reg, "parse_point", counting_point)
    monkeypatch.setattr(reg, "parse_contribution", counting_contribution)
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("alpha", alpha)))

    reg_.load()
    assert counts == {"point": 1, "contribution": 2}
    reg_.load()
    assert counts == {"point": 1, "contribution": 2}, "a second load() must not re-parse"

    stat = one.stat()
    os.utime(one, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    reg_.load()
    assert counts == {"point": 2, "contribution": 4}, "touching a manifest must re-parse the packs"

    reg_.load()
    assert counts == {"point": 2, "contribution": 4}, "and then the cache holds again"

    reg_.load(refresh=True)
    assert counts == {"point": 3, "contribution": 6}


def test_one_hundred_manifests_load_within_budget(roots, tmp_path):
    alpha = _pack(roots["installed"].path, "alpha")
    _write(alpha / "extensions" / "points" / "widget.yaml", POINT_DATA)
    for index in range(100):
        _write(
            alpha / "extensions" / "alpha.widget" / f"item-{index:03d}.yaml",
            _contribution(summary=f"item number {index}"),
        )
    discover = _fake_discover(_Record("alpha", alpha))
    reg_ = _registry(roots, tmp_path, discover)

    reg_.load()
    reg_.load()

    assert len(reg_.contributions("alpha.widget")) == 100
    assert len(discover.calls) == 2, "each load discovers the configured pack roots"


def test_discover_is_called_with_the_non_builtin_roots(roots, tmp_path):
    alpha = _pack(roots["user"].path, "alpha")
    _write(alpha / "extensions" / "points" / "widget.yaml", POINT_DATA)
    discover = _fake_discover(_Record("alpha", alpha))
    reg_ = _registry(roots, tmp_path, discover).load()

    assert str(roots["user"].path) in discover.calls[0]["extra_pack_roots"]
    assert str(roots["built-in"].path) not in discover.calls[0]["extra_pack_roots"]
    assert [p.id for p in reg_.points()] == ["alpha.widget"]
    assert reg_.points()[0].path == alpha / "extensions" / "points" / "widget.yaml"


def test_pack_shaped_points_cannot_claim_a_reserved_owner(roots, tmp_path):
    rogue = _pack(roots["installed"].path, "timeline")
    _write(rogue / "extensions" / "points" / "sneaky.yaml", POINT_DATA)
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("timeline", rogue))).load()

    assert reg_.points() == []
    reserved = [p for p in reg_.problems() if isinstance(p, Quarantined)]
    assert any("reserved namespace" in q.error for q in reserved)


def test_workflow_contribution_registers_ready_root_entries_build_and_after_outputs(roots, tmp_path):
    """Plan 32 T6: the vibecomfy.workflows point carries a ready root, thin entries, build and after_outputs."""
    from astrid.core.generation import workflows

    source = Path(__file__).resolve().parents[3] / "astrid/packs/vibecomfy/extensions/points"
    vibe = _pack(roots["installed"].path, "vibecomfy")
    for name in ("workflows.yaml", "workflows.schema.json"):
        _write(vibe / "extensions" / "points" / name, (source / name).read_text(encoding="utf-8"))
    (vibe / "ready" / "minimax").mkdir(parents=True)
    _write(vibe / "extensions" / "vibecomfy.workflows" / "minimax.yaml",
           _contribution() + "ready_root: ../../ready\n"
           "entries:\n"
           "  - {id: minimax-h3, kind: video, modes: [t2v], bundle: minimax, needs: [], binds: {}, defaults: {}}\n"
           "build: hooks.py:build\nafter_outputs: hooks.py:after_outputs\n")
    _write(vibe / "extensions" / "vibecomfy.workflows" / "hooks.py",
           "def build(inputs, files, out):\n    return {'workflow': 'W', 'bindings': {}, 'plan': 'p'}\n"
           "def after_outputs(job_dir, inputs, files, outputs):\n    return outputs\n")
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("vibecomfy", vibe))).load()
    assert reg_.problems() == []

    (contribution,) = workflows.contributions(reg_)
    registered = []
    api = SimpleNamespace(register_ready_root=registered.append)
    assert workflows.register_ready_roots(reg_, lambda: api) == [(vibe / "ready").resolve()]
    assert registered == [(vibe / "ready").resolve()]

    (entry,) = workflows.workflow_entries(reg_)
    assert (entry["id"], entry["kind"], entry["modes"]) == ("minimax-h3", "video", ["t2v"])
    assert entry["bundle"] == str((vibe / "ready" / "minimax").resolve())
    assert set(entry["hooks"]) == {"build", "after_outputs"}
    assert entry["hooks"]["build"]({}, {}, None)["plan"] == "p"
    assert entry["hooks"]["after_outputs"](None, {}, {}, ["o"]) == ["o"]
    assert workflows.hook(reg_, contribution, "build") is not None


def test_workflow_contribution_hooks_are_optional_and_refs_stay_in_the_pack(roots, tmp_path):
    from astrid.core.generation import workflows

    source = Path(__file__).resolve().parents[3] / "astrid/packs/vibecomfy/extensions/points"
    vibe = _pack(roots["installed"].path, "vibecomfy")
    for name in ("workflows.yaml", "workflows.schema.json"):
        _write(vibe / "extensions" / "points" / name, (source / name).read_text(encoding="utf-8"))
    _write(vibe / "extensions" / "vibecomfy.workflows" / "plain.yaml", _contribution() + "ready_root: ready\n")
    _write(vibe / "extensions" / "vibecomfy.workflows" / "escape.yaml",
           _contribution() + "ready_root: ../../../../outside\n")
    reg_ = _registry(roots, tmp_path, _fake_discover(_Record("vibecomfy", vibe))).load()
    by_key = {c.key: c for c in workflows.contributions(reg_)}
    assert workflows.hooks(reg_, by_key["plain"]) == {}
    with pytest.raises(Exception, match="escapes"):
        workflows.ready_root(by_key["escape"])


def test_h3_contribution_registers_build_and_after_outputs(roots, tmp_path):
    """Plan 32 T6b-1: h3_av is one pack with one vibecomfy.workflows contribution."""
    import yaml
    from astrid.core.generation import workflows

    repo = Path(__file__).resolve().parents[3] / "astrid/packs"
    h3_pack = yaml.safe_load((repo / "h3_av/pack.yaml").read_text(encoding="utf-8"))
    assert h3_pack["dependencies"]["packs"] == ["vibecomfy"]

    packs = {}
    for name in ("vibecomfy", "h3_av"):
        packs[name] = _pack(roots["installed"].path, name)
        src = repo / name / "extensions"
        for path in src.rglob("*"):
            if path.is_file() and "points" in path.parts or path.is_file() and name == "h3_av":
                _write(packs[name] / path.relative_to(repo / name), path.read_text(encoding="utf-8"))
    for trio in ("native_h3_continuation", "native_h3_continuation_refs", "lanpaint_h3_av_generalized"):
        (packs["h3_av"] / "workflows" / trio).mkdir(parents=True)
    reg_ = _registry(roots, tmp_path, _fake_discover(
        _Record("vibecomfy", packs["vibecomfy"]), _Record("h3_av", packs["h3_av"]))).load()
    assert reg_.problems() == []

    (contribution,) = workflows.contributions(reg_)
    assert contribution.id == "h3_av.h3"
    assert workflows.ready_root(contribution) == (packs["h3_av"] / "workflows").resolve()
    (entry,) = workflows.workflow_entries(reg_)
    assert (entry["id"], entry["kind"]) == ("h3", "video")
    assert entry["modes"] == ["i2v", "v2v", "video-edit"]
    assert set(entry["hooks"]) == {"build", "after_outputs"}
    assert entry["hooks"]["build"].__name__ == "build"
    assert entry["hooks"]["after_outputs"].__name__ == "after_outputs"
