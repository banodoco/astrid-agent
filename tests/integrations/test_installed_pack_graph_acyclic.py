"""PK-12 (new): the installed pack graph has no cycle.

The release wheel is built and extracted into a disposable interpreter. From
the installed packs only, two graphs are read: the declared
``dependencies.packs`` graph, and the code-reference graph (Python imports of
``astrid.packs.<other>`` plus module paths a manifest names, such as
``metadata.command_builder``, which are imported at run time). Both must be
acyclic; ``video_editing`` depends on ``editorial`` and ``iteration`` and
``music`` on ``chiptune``; every cross-pack code reference must be a declared
dependency (core packs excepted).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from tests.packs.rendering.test_installed_render_composition import build_installed_render

PROBE = Path(__file__).with_name("installed_pack_graph_probe.py")
CORE = {"generation", "rendering", "timeline", "vibecomfy", "hivemind"}


def _cycles(graph: dict[str, list[str]]) -> list[list[str]]:
    state: dict[str, int] = {}
    stack: list[str] = []
    found: list[list[str]] = []

    def visit(node: str) -> None:
        state[node] = 1
        stack.append(node)
        for child in graph.get(node, ()):
            if state.get(child) == 1:
                found.append(stack[stack.index(child):] + [child])
            elif not state.get(child):
                visit(child)
        stack.pop()
        state[node] = 2

    for node in sorted(graph):
        if not state.get(node):
            visit(node)
    return found


@pytest.fixture(scope="module")
def installed_graph(tmp_path_factory):
    for root, python, site, environment in build_installed_render(tmp_path_factory.mktemp("installed-pack-graph")):
        result = subprocess.run([str(python), "-I", str(PROBE), str(root), str(site)], cwd=root,
                                env=environment, text=True, capture_output=True, timeout=300)
        assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
        yield json.loads((root / "graph.json").read_text())


def test_installed_declared_graph_is_acyclic_with_the_required_edges(installed_graph):
    declared = installed_graph["declared"]
    assert set(declared["video_editing"]) == {"editorial", "iteration"}
    assert declared["music"] == ["chiptune"]
    assert declared["editorial"] == []
    assert _cycles(declared) == []


def test_installed_code_references_are_declared_and_acyclic(installed_graph):
    declared = installed_graph["declared"]
    references = installed_graph["references"]
    undeclared = {pack: sorted(set(refs) - set(declared.get(pack, ())) - CORE)
                  for pack, refs in references.items() if pack not in CORE}
    assert {pack: refs for pack, refs in undeclared.items() if refs} == {}
    # Core packs ship together inside Agent; only the packs that install
    # separately (PK-15/PK-16) need an install order, so the cycle check is over them.
    combined = {pack: sorted((set(declared.get(pack, ())) | set(references.get(pack, ()))) - CORE)
                for pack in declared if pack not in CORE}
    assert _cycles(combined) == []
