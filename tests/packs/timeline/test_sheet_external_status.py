"""Status reports exact external revision identities before any detail."""
import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from astrid.packs.timeline.cli_common import status_text
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.revisions import RuntimeRevisions


def test_status_first_line_names_unimported_external_revisions(tmp_path, capsys):
    tl = Checkout(json.loads((Path(__file__).parents[2] / 'fixtures/timeline_editing/tiny_publishable.json').read_text()))
    tl.save(tmp_path / 'draft.json', quiet=True)
    class Client:
        def list_parent_composition_revisions(self, *args, **kwargs):
            return SimpleNamespace(ok=True, data={'items': [
                {'revision_id': 'outside-original-2', 'previous_revision_id': 'outside-original-1', 'is_current_head': True},
                {'revision_id': 'outside-original-1', 'previous_revision_id': 'rev-0'}]})
    parsed = argparse.Namespace(project='p', ref='t', client=Client(), render=False, all=False)
    status_text(parsed, 'p', 't', 'outside-original-2', 'draft', tl.path)
    first = capsys.readouterr().out.splitlines()[0]
    assert 'unimported external revisions: outside-original-1, outside-original-2' in first


def test_runtime_revision_adapter_preserves_semantic_evidence():
    evidence = {'commands': [], 'digests': {'after': 'digest'}, 'toolchain': {'compiler_version': 'fixture'}}
    client = SimpleNamespace(open_composition=lambda *a, **k: SimpleNamespace(ok=True, data={'summary': {
        'revision_id': 'exact', 'previous_revision_id': 'base', 'evidence': evidence}}))
    assert RuntimeRevisions(client).get('p', 't', 'exact').evidence == evidence


def test_default_status_first_line_names_external_and_json_keeps_them(tmp_path, monkeypatch, capsys):
    from astrid.packs.timeline import cli
    from astrid.sdk.timeline import store
    from astrid.sdk.timeline import external
    from astrid.sdk.timeline.revisions import Revision
    tl = Checkout(json.loads((Path(__file__).parents[2] / 'fixtures/timeline_editing/tiny_publishable.json').read_text()))
    tl.save(tmp_path / 'draft.json', quiet=True)
    monkeypatch.setattr(store, 'resolve_ids', lambda *a, **k: ('p', 't', 'outside'))
    monkeypatch.setattr(store, 'current_draft', lambda *a: 'draft')
    monkeypatch.setattr(store, 'draft_path', lambda *a: tl.path)
    monkeypatch.setattr(external, 'unimported', lambda *a: [Revision('outside', 'app', None, 'rev-0', None)])
    parsed = argparse.Namespace(project='p', ref='t', draft=None, text=False, client=None, json=False)
    assert cli._cmd_status(parsed) == 0
    assert capsys.readouterr().out.splitlines()[0] == 'unimported external revisions: outside'
    parsed.json = True
    assert cli._cmd_status(parsed) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['data']['unimported_external_revisions'] == ['outside']
