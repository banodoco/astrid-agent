"""Remote SDK coverage for runtime-owned immutable shot text bindings."""

from __future__ import annotations

from astrid.sdk.remote import RemoteShots


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def list_project_shot_text_bindings(self, *args, **kwargs):
        self.calls.append(("list", args, kwargs))
        return ([{"binding_id": "b1"}], None)

    def get_project_shot_text_binding(self, *args, **kwargs):
        self.calls.append(("get", args, kwargs))
        return {"binding_id": "b1"}

    def set_project_shot_text_binding(self, *args, **kwargs):
        self.calls.append(("set", args, kwargs))
        return {"data": {"binding_id": "b1"}, "receipt": None}

    def set_project_shot_text_binding_by_id(self, *args, **kwargs):
        self.calls.append(("set_by_id", args, kwargs))
        return {"data": {"binding_id": "b1"}, "receipt": None}

    def rebind_project_shot_text_binding(self, *args, **kwargs):
        self.calls.append(("rebind", args, kwargs))
        return {"data": {"binding_id": "b1"}, "receipt": None}


def test_text_binding_methods_are_thin_runtime_adapters() -> None:
    transport = _Transport()
    shots = RemoteShots(transport)

    assert shots.list_text_bindings("p1", shot_id="s1", kind="prompt", slot="hero").ok
    assert shots.show_text_binding("p1", "b1").ok
    assert shots.set_text_binding(
        "p1", shot_id="s1", kind="prompt", slot="hero", text="hello", expected_head=0,
        idempotency_key="set-1",
    ).ok
    assert shots.set_text_binding(
        "p1", shot_id="ignored", kind="prompt", text="world", expected_head=1,
        binding_id="b1", idempotency_key="set-2",
    ).ok
    assert shots.rebind_text_binding(
        "p1", "b1", media_id="sha256:" + "a" * 64, expected_head=1,
        idempotency_key="rebind-1",
    ).ok

    assert transport.calls == [
        ("list", ("p1",), {"shot_id": "s1", "kind": "prompt", "slot": "hero"}),
        ("get", ("p1", "b1"), {}),
        (
            "set", ("p1", {"shot_id": "s1", "kind": "prompt", "text": "hello", "expected_head": 0, "slot": "hero"}),
            {"idempotency_key": "set-1"},
        ),
        (
            "set_by_id", ("p1", "b1", {"binding_id": "b1", "text": "world", "expected_head": 1}),
            {"idempotency_key": "set-2"},
        ),
        (
            "rebind", ("p1", "b1"),
            {"media_id": "sha256:" + "a" * 64, "expected_head": 1, "idempotency_key": "rebind-1"},
        ),
    ]


def test_readable_binding_fetches_exact_utf8_and_checks_integrity():
    import hashlib
    from types import SimpleNamespace
    raw = 'Narration — café'.encode()
    row = {'binding_id': 'b1', 'media_id': 'text-object', 'content_hash': 'sha256:' + hashlib.sha256(raw).hexdigest(), 'byte_size': len(raw)}
    transport = SimpleNamespace(get_project_shot_text_binding=lambda *a: row,
                                list_project_shot_text_bindings=lambda *a, **k: ([row], None),
                                get_object=lambda ref: {'data': raw})
    shots = RemoteShots(transport)
    assert shots.show_text_binding('p', 'b1', include_text=True).data['text'] == raw.decode()
    assert shots.list_text_bindings('p', include_text=True).data[0][0]['text'] == raw.decode()
    transport.get_object = lambda ref: {'data': b'wrong'}
    failure = shots.show_text_binding('p', 'b1', include_text=True)
    assert not failure.ok and failure.error.code == 'integrity_error'


def test_text_cli_reads_and_sets_through_sdk(tmp_path):
    from types import SimpleNamespace
    from astrid.core.cli.domain_shots import build_parser
    from astrid.sdk.contracts import DomainResult
    calls = []
    def capture(name):
        def f(*a, **kw):
            calls.append((name, a, kw))
            return DomainResult.success({})
        return f
    client = SimpleNamespace(shots=SimpleNamespace(list_text_bindings=capture('list'), show_text_binding=capture('show'), set_text_binding=capture('set')))
    parser = build_parser(client)
    source = tmp_path / 'voice.txt'; source.write_text('Exact narration.\n', encoding='utf-8')
    for args in [
        ['text', 'list', '--project', 'p', '--shot', 's', '--kind', 'voiceover_script'],
        ['text', 'show', 'b', '--project', 'p'],
        ['text', 'set', 's', '--project', 'p', '--kind', 'voiceover_script', '--text-file', str(source), '--expected-head', '0', '--idempotency-key', 'test'],
    ]:
        parsed = parser.parse_args(args); assert parsed.handler(parsed) == 0
    assert calls == [
        ('list', ('p',), {'shot_id':'s', 'kind':'voiceover_script', 'slot':None, 'include_text':True}),
        ('show', ('p','b'), {'include_text':True}),
        ('set', ('p',), {'shot_id':'s', 'kind':'voiceover_script', 'slot':None, 'text':'Exact narration.\n', 'expected_head':0, 'idempotency_key':'test'}),
    ]
