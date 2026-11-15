"""The caller accounts from its entry stamp through the final print boundary."""
from types import SimpleNamespace

from astrid.core.execution import phase_timing as pt


def test_caller_clock_and_exclusive_delivery_phases(monkeypatch):
    now = [10.0]
    monkeypatch.setattr(pt, 'clock_id', lambda: 'caller')
    timing = pt.CliTiming(clock=lambda: now[0], started=2.0)
    token = pt._ACTIVE_CLI.set(timing)
    try:
        pt.cli_stamp('runtime')
        pt.cli_stamp('submit')
        now[0] = 12.0
        pt.cli_stamp('submitted')
        now[0] = 20.0
        pt.cli_stamp('result')
        timing.host = {'clock_id': 'caller', 'started_monotonic': 13.0,
                       'ended_monotonic': 19.0, 'total': {'seconds': 6.0}}
        with pt.cli_phase('delivery'):
            now[0] = 21.0
            with pt.cli_phase('materialisation'):
                now[0] = 24.0
            with pt.cli_phase('output_hashing'):
                now[0] = 26.0
            with pt.cli_phase('project_open_url'):
                now[0] = 27.0
            now[0] = 28.0
        now[0] = 30.0  # context close / formatting before print
        payload = {}
        pt.attach_cli(payload)
        block = payload['timing']['cli']
        assert block['total']['seconds'] == 28.0
        phases = block['phases']
        assert phases['cli_startup']['seconds'] == 8.0
        for name, seconds in {'delivery': 2, 'materialisation': 3,
                              'output_hashing': 2, 'project_open_url': 1}.items():
            assert phases[name]['seconds'] == seconds
        assert sum(p['seconds'] or 0 for p in phases.values()) + block['unaccounted']['raw_seconds'] == 28.0
    finally:
        pt._ACTIVE_CLI.reset(token)


def test_cli_main_uses_gateway_entry_stamp(monkeypatch):
    from astrid.core.generation import cli
    monkeypatch.setenv('ASTRID_CLI_START_MONO', '2.0')
    monkeypatch.setattr(pt, 'time', SimpleNamespace(monotonic=lambda: 10.0))
    def run(_):
        assert pt._ACTIVE_CLI.get().started == 2.0
        return 0
    monkeypatch.setattr(cli, '_timed_main', run)
    assert cli.main([]) == 0


def test_entry_stamp_precedes_heavy_imports():
    import ast
    from pathlib import Path
    module = ast.parse(Path('astrid/__init__.py').read_text())
    assignments = [i for i, node in enumerate(module.body) if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == '_PROCESS_ENTRY_MONOTONIC' for t in node.targets)]
    assert assignments, 'package entry must stamp before imports shared by both console routes'
    heavy = next(i for i, node in enumerate(module.body) if isinstance(node, ast.Import)
                 and node.names[0].name == 'importlib')
    assert assignments[0] < heavy


def test_real_cli_delivery_phases_end_after_client_close_before_print(monkeypatch, tmp_path, capsys):
    import hashlib
    from contextlib import contextmanager

    from astrid.core.generation import cli, delivery
    now = [10.0]
    monkeypatch.setenv('ASTRID_CLI_START_MONO', '2.0')
    monkeypatch.setattr(pt, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(pt, 'clock_id', lambda: 'caller')
    source = tmp_path / 'output.png'
    source.write_bytes(b'cpu-image')
    digest = hashlib.sha256(b'cpu-image').hexdigest()
    def media_open(*args, **kwargs):
        now[0] += 3
        return SimpleNamespace(ok=True, data={'local_path': str(source)})
    @contextmanager
    def open_client():
        yield SimpleNamespace(media=SimpleNamespace(open=media_open))
        now[0] += 2
    real_hash = hashlib.sha256
    def hash_output(*args):
        now[0] += 2
        return real_hash(*args)
    monkeypatch.setattr(delivery, 'hashlib', SimpleNamespace(sha256=hash_output))
    real_encode = delivery.urlencode
    def encode(*args):
        now[0] += 1
        return real_encode(*args)
    monkeypatch.setattr(delivery, 'urlencode', encode)
    def generate(**kwargs):
        pt.cli_stamp('submit')
        now[0] = 12
        pt.cli_stamp('submitted')
        now[0] = 20
        pt.cli_stamp('result')
        return {'manifest': {'outputs': [{'object_id': 'sha256:' + digest, 'digest': digest,
                    'bytes': 9, 'filename': 'output.png', 'project_id': 'demo'}]},
                'timing': {'schema': pt.TIMING_SCHEMA, 'host': {'clock_id': 'caller',
                    'started_monotonic': 13, 'ended_monotonic': 19, 'total': {'seconds': 6}}}}
    monkeypatch.setattr(cli, '_open_client', open_client)
    monkeypatch.setattr(cli, '_generate_facade', lambda: SimpleNamespace(image=generate))
    assert cli.main(['image', 'cpu', '--model', 'flux-schnell', '--placement', 'managed',
                     '--project', 'demo', '--out', str(tmp_path / 'delivered'), '--json']) == 0
    import json
    payload = json.loads(capsys.readouterr().out)
    block = payload['timing']['cli']
    assert block['total']['seconds'] == now[0] - 2 == 26
    phases = block['phases']
    assert phases['cli_startup']['seconds'] == 8
    assert phases['materialisation']['seconds'] == 3
    assert phases['output_hashing']['seconds'] == 2
    assert phases['project_open_url']['seconds'] == 1
    assert phases['delivery']['seconds'] == 0
    assert block['unaccounted']['raw_seconds'] == 0
