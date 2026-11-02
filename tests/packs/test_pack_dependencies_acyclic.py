"""Pack-level dependencies must resolve and remain acyclic."""

from __future__ import annotations

from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
PACKS_ROOT = REPO_ROOT / "astrid" / "packs"


def _pack_dependencies() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for manifest in sorted(PACKS_ROOT.glob("*/pack.yaml")):
        data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        pack_id = data.get("id", manifest.parent.name)
        assert pack_id not in graph, f"duplicate pack id: {pack_id}"
        graph[pack_id] = set((data.get("dependencies") or {}).get("packs") or [])
    return graph


def _dependency_cycles(graph: dict[str, set[str]]) -> list[tuple[str, ...]]:
    state: dict[str, int] = {}
    path: list[str] = []
    cycles: set[tuple[str, ...]] = set()

    def visit(pack_id: str) -> None:
        state[pack_id] = 1
        path.append(pack_id)
        for dependency in sorted(graph[pack_id]):
            assert dependency in graph, f"{pack_id} depends on missing pack {dependency}"
            if state.get(dependency) == 1:
                cycle = path[path.index(dependency):]
                rotations = [tuple(cycle[index:] + cycle[:index]) for index in range(len(cycle))]
                cycles.add(min(rotations))
            elif state.get(dependency, 0) == 0:
                visit(dependency)
        path.pop()
        state[pack_id] = 2

    for pack_id in sorted(graph):
        if state.get(pack_id, 0) == 0:
            visit(pack_id)
    return sorted(cycles)


def test_declared_pack_dependencies_are_resolvable_and_acyclic() -> None:
    graph = _pack_dependencies()

    assert graph["editorial"] == set()
    assert graph["video_editing"] == {"editorial", "iteration"}
    assert graph["music"] == {"chiptune"}
    assert _dependency_cycles(graph) == []


def test_editorial_and_video_editing_cross_only_through_public_api() -> None:
    editorial_sources = (PACKS_ROOT / "editorial").rglob("*.py")
    for source_path in editorial_sources:
        source = source_path.read_text(encoding="utf-8")
        assert "astrid.packs.video_editing" not in source, source_path

    video_sources = (PACKS_ROOT / "video_editing").rglob("*.py")
    for source_path in video_sources:
        source = source_path.read_text(encoding="utf-8")
        for line in source.splitlines():
            if "astrid.packs.editorial" in line:
                assert "astrid.packs.editorial.api" in line, f"{source_path}: {line}"
