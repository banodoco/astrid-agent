"""One project link is named the same way on every generation path: CLI, MCP, admitted or completed."""
import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from astrid import mcp_server
from astrid.core.generation import cli
from astrid.core.generation.delivery import deliver_generation

PROJECT = 'proj-1'
EXPECTED = 'https://astrid.haus/tools/video-editor?project=proj-1'


def _tools():
    return {**mcp_server.build_tools(), **mcp_server._extra_tools()}


def _completed_managed_result(tmp_path, project):
    data = b'\x89PNG parity bytes'
    local = tmp_path / 'runtime-object.png'
    local.write_bytes(data)
    row = {'object_id': 'obj-1', 'digest': 'sha256:' + hashlib.sha256(data).hexdigest(),
           'size': len(data), 'role': 'result', 'filename': 'first.png', 'project_id': project}
    client = SimpleNamespace(media=SimpleNamespace(open=lambda *a, **k: SimpleNamespace(
        ok=True, data={'local_path': str(local)}, error=None)))
    return {'task_id': 'task-1', 'manifest': {'outputs': [row]}}, client


def _mcp_run(stdout):
    def fake_run(argv, cwd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=stdout, stderr='')
    return fake_run


def _completed_task(project_id=PROJECT):
    return json.dumps({'ok': True, 'data': {
        'task_id': 'task-1', 'state': 'completed', 'project_id': project_id,
        'generation_intent': {'version': 1, 'modality': 'image'}}})


def _admitted_cli(monkeypatch, capsys, payload, argv):
    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(
        image=lambda **kwargs: SimpleNamespace(ok=True, to_dict=lambda: dict(payload))))
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(SimpleNamespace()))
    code = cli.main(argv)
    return code, json.loads(capsys.readouterr().out)


def test_four_paths_name_the_same_link_byte_for_byte(tmp_path, monkeypatch, capsys):
    # CLI waiting path: the completed result is delivered with its project.
    completed, client = _completed_managed_result(tmp_path, PROJECT)
    waited = deliver_generation(completed, client=client, out=tmp_path / 'out')['open_url']

    # CLI --no-wait admission: the admitted task names its project.
    _, admitted_cli = _admitted_cli(monkeypatch, capsys, {'kernel_task_id': 'task-1', 'project_id': PROJECT},
                                    ['image', 'portrait', '--placement', 'local', '--no-wait', '--json'])

    # MCP admission and MCP tasks_show, through the same CLI envelopes the subprocess prints.
    monkeypatch.setattr(mcp_server, '_run', _mcp_run(
        '{"ok": true, "data": {"kernel_task_id": "task-1", "project_id": "proj-1"}}'))
    admitted_mcp = mcp_server.call_tool(_tools()['generate_image'], {'prompt': 'portrait', 'project': PROJECT})
    monkeypatch.setattr(mcp_server, '_run', _mcp_run(_completed_task()))
    shown_mcp = mcp_server.call_tool(_tools()['tasks_show'], {'project': PROJECT, 'task_id': 'task-1'})

    links = [waited, admitted_cli['open_url'],
             admitted_mcp['structuredContent']['data']['open_url'],
             shown_mcp['structuredContent']['data']['open_url']]
    assert links == [EXPECTED] * 4
    assert all(link.encode() == EXPECTED.encode() for link in links)


def test_the_query_is_exactly_the_project_parameter(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mcp_server, '_run', _mcp_run(_completed_task('proj a/b&c')))
    shown = mcp_server.call_tool(_tools()['tasks_show'], {'project': 'proj a/b&c', 'task_id': 'task-1'})
    for link in (shown['structuredContent']['data']['open_url'],):
        parts = urlsplit(link)
        assert (parts.scheme, parts.netloc, parts.path, parts.fragment) == (
            'https', 'astrid.haus', '/tools/video-editor', '')
        assert parse_qs(parts.query, keep_blank_values=True, strict_parsing=True) == {'project': ['proj a/b&c']}


def test_no_project_gives_no_open_url(tmp_path, monkeypatch, capsys):
    # MCP admission with no project named anywhere in the admitted result.
    monkeypatch.setattr(mcp_server, '_run', _mcp_run('{"ok": true, "data": {"kernel_task_id": "task-1"}}'))
    admitted = mcp_server.call_tool(_tools()['generate_image'], {'prompt': 'portrait'})
    assert 'open_url' not in admitted['structuredContent']['data']

    # CLI --no-wait admission with no project selected anywhere.
    code, admitted_cli = _admitted_cli(monkeypatch, capsys, {'kernel_task_id': 'task-1'},
                                       ['image', 'portrait', '--placement', 'local', '--no-wait', '--json'])
    assert code == 0 and 'open_url' not in admitted_cli

    # A dry run admits nothing, so it never carries a link, even if a project name is in its payload.
    code, dry = _admitted_cli(monkeypatch, capsys, {'dry_run': True, 'project_id': PROJECT},
                              ['image', 'portrait', '--placement', 'local', '--dry-run', '--json'])
    assert code == 0 and 'open_url' not in dry

    # A completed task that is not a generation carries no link.
    monkeypatch.setattr(mcp_server, '_run', _mcp_run(json.dumps({'ok': True, 'data': {
        'task_id': 'task-2', 'state': 'completed', 'project_id': PROJECT}})))
    shown = mcp_server.call_tool(_tools()['tasks_show'], {'project': PROJECT, 'task_id': 'task-2'})
    assert 'open_url' not in shown['structuredContent']['data']

    # A generation task that has not completed carries no link yet.
    monkeypatch.setattr(mcp_server, '_run', _mcp_run(_completed_task().replace('completed', 'running')))
    running = mcp_server.call_tool(_tools()['tasks_show'], {'project': PROJECT, 'task_id': 'task-1'})
    assert 'open_url' not in running['structuredContent']['data']


def test_mcp_admission_without_a_project_argument_names_the_selected_project(monkeypatch, capsys):
    # Most agents pass no project: MCP runs the CLI child, which must name the Runtime's selection.
    selected = SimpleNamespace(ok=True, error=None, data={'project': {'project_id': PROJECT}})
    client = SimpleNamespace(projects=SimpleNamespace(current=lambda: selected))
    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(
        image=lambda **kwargs: SimpleNamespace(ok=True, to_dict=lambda: {'kernel_task_id': 'task-1'})))
    monkeypatch.setattr(cli, '_open_client', lambda: nullcontext(client))
    assert cli.main(['image', 'portrait', '--placement', 'local', '--no-wait', '--json']) == 0
    child_stdout = capsys.readouterr().out

    # The MCP side runs that child and returns its output; the arguments name no project.
    monkeypatch.setattr(mcp_server, '_run', _mcp_run(child_stdout))
    admitted = mcp_server.call_tool(_tools()['generate_image'], {'prompt': 'portrait'})

    assert admitted['structuredContent']['open_url'] == EXPECTED
    assert admitted['structuredContent']['kernel_task_id'] == 'task-1'
