"""Declaration-only role queries and structural edits without a renderer."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("clip,track,expected", [
    ({"clipType": "plate", "asset": "P"}, "fx", "picture"),
    ({"clipType": "plate", "params": {"src": "url"}}, "fx", "picture"),
    ({"clipType": "plate", "asset": "", "params": {}}, "plate", "background"),
    ({"clipType": "plate", "asset": None}, {"id": "plate"}, "background"),
    ({"clipType": "label"}, "plate", "content"),
    ({"clipType": "guide"}, "plate", "none"),
    ({"clipType": "missing"}, "plate", "picture"),
    ({"clipType": "missing"}, {"id": "plate"}, "picture"),
    ({"clipType": "missing"}, "fx", "content"),
    ({}, None, "content"),
])
def test_role_uses_declaration_then_legacy_track_convention(clip, track, expected):
    from astrid.core.timeline import element_roles, onscreen

    elements = {
        "plate": {"coverage": "picture", "background_unless": ["asset", "params.src"]},
        "label": {"coverage": "content"},
        "guide": {"coverage": "none"},
    }
    assert element_roles.role_of(clip, track, elements) == expected
    assert onscreen.role_of is element_roles.role_of
    assert onscreen.role_of(clip, track, elements) == expected


@pytest.mark.parametrize("layout", ["elements", "rendering/elements"])
def test_metadata_lookup_reads_both_manifest_layouts(tmp_path, monkeypatch, layout):
    from astrid.core.timeline import element_roles

    packs = tmp_path / "packs"
    declarations = {
        "declared": "metadata:\n  coverage: picture\n  background_unless: [asset]\n",
        "bare": "id: bare\nkind: effect\n",
        "broken": "metadata: [unterminated\n",
    }
    for element_id, content in declarations.items():
        path = packs / "demo" / layout / "effects" / element_id / "element.yaml"
        path.parent.mkdir(parents=True)
        path.write_text(content)
    monkeypatch.setattr(element_roles, "_PACKS_ROOT", packs)
    monkeypatch.setattr(element_roles, "_COVERAGE_CACHE", {})
    assert element_roles.element_coverage(["declared", "bare", "broken", "missing"]) == {
        "declared": {"coverage": "picture", "background_unless": ["asset"]},
        "bare": {}, "broken": {}, "missing": {},
    }


def test_frozen_compatibility_defaults_still_defer_to_a_declaration(tmp_path, monkeypatch):
    from astrid.core.timeline import element_roles

    path = tmp_path / "demo/elements/effects/am-snap-plate/element.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("metadata:\n  coverage: content\n")
    monkeypatch.setattr(element_roles, "_PACKS_ROOT", tmp_path)
    monkeypatch.setattr(element_roles, "_COVERAGE_CACHE", {})
    assert element_roles.element_coverage(["am-snap-plate", "am-footage"]) == {
        "am-snap-plate": {"coverage": "content"}, "am-footage": {"coverage": "picture"},
    }


def test_structural_edit_and_reflow_with_renderer_owner_unavailable():
    # A fresh interpreter prevents an already-imported renderer from hiding a
    # dependency. Any import, process launch, or socket call at these boundaries
    # fails deterministically; the test never starts an owner or a compiler.
    script = textwrap.dedent('''
        import json, pathlib, socket, subprocess, sys
        blocked = (
            'astrid.core.timeline.onscreen',
            'astrid.core.timeline.compiler',
            'astrid.core.rendering.remotion_runtime',
            'astrid.packs.rendering.rendering.renderers',
            'astrid.packs.rendering.actions.timeline_visualize.composed_frame',
        )
        class Unavailable:
            def find_spec(self, fullname, *args):
                if any(fullname == p or fullname.startswith(p + '.') for p in blocked):
                    raise AssertionError('unavailable renderer/compiler dependency: ' + fullname)
        sys.meta_path.insert(0, Unavailable())
        from astrid.sdk.timeline_checkout import Checkout
        def unavailable(*args, **kwargs):
            raise AssertionError('structural edit attempted process/owner I/O')
        subprocess.Popen = unavailable
        socket.socket = unavailable
        fixture = pathlib.Path('tests/fixtures/timeline_editing/tiny.json')
        tl = Checkout(json.loads(fixture.read_text()))
        assert tl._doomed_cuts('A', 0, 4, set()) == {'c1'}
        # A plate without media is background and remains a structural picture.
        tl.clip('a-plate').data.pop('asset')
        assert tl._doomed_cuts('A', 0, 4, set()) == {'c1'}
        assert tl._doomed_cuts('A', 0, 2, set()) == set()
        with tl.edit('offline edit', check=False, quiet=True) as edit:
            tl.clip('b-type').nudge(frames=1)
            tl.ripple_delete(2.6, 3.0)
        assert edit.changed and len(tl._journal) == 1
        assert abs(tl.clip('b-type').start - 3.633333) < 1e-6
        assert abs(tl.clip('b-plate').start - 3.6) < 1e-6
        assert not any(name in sys.modules for name in blocked)
        print('structural edit and reflow completed without owner/compiler access')
    ''')
    result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "completed without owner/compiler access" in result.stdout
