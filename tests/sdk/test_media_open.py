from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

from astrid.core.cli.domain_media import build_parser
from astrid.sdk.contracts import DomainResult
from astrid.sdk.remote import RemoteMedia


class _Runtime:
    def __init__(self, payload: bytes = b"hello world") -> None:
        self.payload = payload
        self.reads: list[str] = []
        self.tasks = 0
        self.objects = {
            "sha256:" + hashlib.sha256(payload).hexdigest(): payload,
        }

    def list_project_objects(self, project: str, *, cursor=None, limit=50):
        assert project == "demo"
        return [[{
            "object_id": next(iter(self.objects)),
            "digest": next(iter(self.objects)),
            "media_type": "text/plain",
            "size": len(self.payload),
            "filename": "note.txt",
        }], None]

    def get_object(self, object_id: str):
        self.reads.append(object_id)
        return {"data": self.objects[object_id]}


def test_open_returns_bounded_identity_preview_without_mutation() -> None:
    runtime = _Runtime()
    ref = next(iter(runtime.objects))
    result = RemoteMedia(runtime).open("demo", {"kind": "source", "object_id": ref})
    assert result.ok
    assert result.data["kind"] == "source"
    assert result.data["content_identity"] == ref
    assert result.data["preview"]["text"] == "hello world"
    assert result.data["local_path"] is None
    assert runtime.reads == [ref]


def test_open_allows_a_zero_byte_inline_preview() -> None:
    runtime = _Runtime(b"no inline bytes")
    ref = next(iter(runtime.objects))

    result = RemoteMedia(runtime).open("demo", ref, preview_bytes=0)

    assert result.ok
    assert result.data["preview"] is None
    assert runtime.reads == [ref]


def test_open_materializes_only_when_requested_and_preserves_extension(tmp_path: Path) -> None:
    runtime = _Runtime(b"a" * 64)
    ref = next(iter(runtime.objects))
    result = RemoteMedia(runtime).open(
        "demo", ref, materialize=True, cache_root=tmp_path, max_bytes=128, preview_bytes=32
    )
    assert result.ok
    path = Path(result.data["local_path"])
    assert path.parent == tmp_path
    assert path.name == "note.txt"
    assert path.read_bytes() == b"a" * 64


def test_open_materialize_infers_extension_from_mime_type(tmp_path: Path) -> None:
    runtime = _Runtime(b'{"ok":true}')
    digest = next(iter(runtime.objects))
    runtime.list_project_objects = lambda project, *, cursor=None, limit=50: [[{
        "object_id": digest, "digest": digest, "media_type": "application/json",
        "size": len(runtime.payload), "filename": "report",
    }], None]
    result = RemoteMedia(runtime).open("demo", digest, materialize=True, cache_root=tmp_path)
    assert result.ok
    assert Path(result.data["local_path"]).name == "report.json"


def test_media_open_reuses_a_verified_deterministic_view(tmp_path: Path) -> None:
    runtime = _Runtime(b"repeatable materialization")
    ref = next(iter(runtime.objects))
    first = RemoteMedia(runtime).open("demo", ref, materialize=True, cache_root=tmp_path, max_bytes=128, preview_bytes=64)
    second = RemoteMedia(runtime).open("demo", ref, materialize=True, cache_root=tmp_path, max_bytes=128, preview_bytes=64)
    assert first.ok and second.ok
    assert first.data["local_path"] == second.data["local_path"]
    assert second.data["local_path_storage"] == "verified-reuse"
    assert list(tmp_path.glob("*.txt")) == [Path(first.data["local_path"])]


def test_open_rejects_unscoped_alias_and_oversize_without_download() -> None:
    runtime = _Runtime(b"0123456789")
    result = RemoteMedia(runtime).open("demo", {"kind": "timeline_asset", "asset_id": "alias"})
    assert not result.ok
    assert result.error.code == "validation_error"
    assert runtime.reads == []

    runtime = _Runtime(b"0123456789")
    ref = next(iter(runtime.objects))
    result = RemoteMedia(runtime).open("demo", ref, max_bytes=4, preview_bytes=2)
    assert result.ok
    assert result.data["availability"] == "oversize"
    assert runtime.reads == []


def test_open_text_binding_verifies_project_and_utf8() -> None:
    raw = "Narration — café".encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()

    class Runtime(_Runtime):
        def __init__(self):
            super().__init__(raw)
            self.objects = {digest: raw}

        def get_project_shot_text_binding(self, project, binding_id):
            return {
                "binding_id": binding_id,
                "media_id": digest,
                "content_hash": digest,
                "byte_size": len(raw),
                "slot": "voiceover",
            }

    result = RemoteMedia(Runtime()).open("demo", {"kind": "text_binding", "binding_id": "b1"})
    assert result.ok
    assert result.data["kind"] == "text"
    assert result.data["preview"]["text"] == "Narration — café"


def test_cli_open_is_one_sdk_call_and_accepts_structured_reference(tmp_path: Path, capsys) -> None:
    calls = []

    def opened(*args, **kwargs):
        calls.append((args, kwargs))
        return DomainResult.success({"kind": "source", "availability": "available"})

    client = SimpleNamespace(media=SimpleNamespace(open=opened))
    parser = build_parser(client)
    parsed = parser.parse_args([
        "open", '{"kind":"source","object_id":"sha256:abc"}',
        "--project", "demo", "--materialize", "--cache-root", str(tmp_path),
        "--json",
    ])
    assert parsed.handler(parsed) == 0
    assert calls == [
        (("demo", {"kind": "source", "object_id": "sha256:abc"}), {
            "materialize": True, "cache_root": str(tmp_path),
            "preview_bytes": 16 * 1024, "max_bytes": 10 * 1024 * 1024,
        })
    ]
    assert '"kind":"source"' in capsys.readouterr().out


def test_media_open_defaults_to_human_output() -> None:
    parser = build_parser(SimpleNamespace())
    parsed = parser.parse_args(["open", "media-1", "--project", "demo"])
    assert parsed.json is False
    assert parser.parse_args(["open", "media-1", "--project", "demo", "--json"]).json is True


def test_materialize_returns_an_independent_copy_of_verified_response(tmp_path: Path, monkeypatch) -> None:
    payload = b"unique output bytes for custody proof" * 1024
    runtime = _Runtime(payload)
    reference = next(iter(runtime.objects))
    source = tmp_path / "runtime-cas"
    source.write_bytes(payload)
    runtime.get_project_object_location = lambda *_args: {"storage": "runtime_cas", "verified": True,
                                                          "digest": reference, "local_path": str(source)}
    original = hashlib.sha256
    observed = []

    def counted(data=b"", *args, **kwargs):
        if data == payload:
            observed.append(len(data))
        return original(data, *args, **kwargs)

    monkeypatch.setattr(hashlib, "sha256", counted)
    result = RemoteMedia(runtime).open("demo", reference, materialize=True, cache_root=tmp_path / "view")
    assert result.ok
    assert observed == [len(payload), len(payload)]  # response verification and view naming/checking
    assert runtime.reads == [reference]
    view = Path(result.data["local_path"])
    assert view.read_bytes() == payload
    view.write_bytes(b"caller mutation")
    assert source.read_bytes() == payload


def test_truncated_runtime_response_does_not_replace_existing_view(tmp_path: Path) -> None:
    runtime = _Runtime(b"complete content")
    reference = next(iter(runtime.objects))
    runtime.get_object = lambda _id: {"data": b"truncated"}
    existing = tmp_path / "note.txt"
    existing.write_bytes(b"original")
    result = RemoteMedia(runtime).open("demo", reference, materialize=True, cache_root=tmp_path)
    assert not result.ok and result.error.code == "integrity_error"
    assert existing.read_bytes() == b"original"


def test_existing_hardlink_to_cas_is_never_returned_as_a_caller_view(tmp_path: Path) -> None:
    import os

    payload = b"retained immutable bytes"
    runtime = _Runtime(payload)
    reference = next(iter(runtime.objects))
    bare = reference.removeprefix("sha256:")
    cas = tmp_path / "cas" / "sha256" / bare[:2] / bare[2:]
    cas.parent.mkdir(parents=True)
    cas.write_bytes(payload)
    runtime.get_project_object_location = lambda *_args: {"storage": "runtime_cas", "verified": True,
                                                          "digest": reference, "local_path": str(cas)}
    cache = tmp_path / "views"
    cache.mkdir()
    existing = cache / "note.txt"
    os.link(cas, existing)

    result = RemoteMedia(runtime).open("demo", reference, materialize=True, cache_root=cache)
    assert result.ok
    view = Path(result.data["local_path"])
    assert view == existing and view.read_bytes() == payload
    assert not os.path.samefile(view, cas)
    view.write_bytes(b"caller mutation")
    assert cas.read_bytes() == payload
    assert existing.read_bytes() == b"caller mutation"
