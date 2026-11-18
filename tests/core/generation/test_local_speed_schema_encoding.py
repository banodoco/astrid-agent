"""The attached stand-in reads packaged JSON as UTF-8 on every platform."""
import json
from pathlib import Path

from tests.integrations import runpod_slot_pod as pod


def test_packaged_schema_survives_windows_default_encoding(tmp_path, monkeypatch):
    # U+0090 contains byte 0x90 in its UTF-8 representation: cp1252 cannot decode it.
    row = {"inputs": {"required": {}}, "description": "kept \u0090"}
    (tmp_path / "comfy_extras@runpod-snapshot.json").write_text(
        json.dumps({"ModelSamplingAuraFlow": row}, ensure_ascii=False), encoding="utf-8")
    original = Path.read_text
    reads = []
    def windows_default(path, *args, **kwargs):
        encoding = kwargs.get("encoding", args[0] if args else None)
        reads.append(encoding)
        if encoding is None:
            return path.read_bytes().decode("cp1252")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", windows_default)
    monkeypatch.setattr(pod, "cache_dir", lambda: tmp_path)
    pod._snapshots.cache_clear()
    try:
        assert pod._snapshots()["ModelSamplingAuraFlow"] == row
        assert reads == ["utf-8"]
        assert pod.real_class("ModelSamplingAuraFlow")["input"] == {"required": {}}
    finally:
        pod._snapshots.cache_clear()


def test_invalid_schema_does_not_hide_other_valid_utf8_snapshot(tmp_path, monkeypatch):
    (tmp_path / "broken.json").write_bytes(b"{not json}")
    row = {"inputs": {"required": {}}, "display_name": "Caf\u00e9"}
    (tmp_path / "source-valid.json").write_text(
        json.dumps({"ValidNode": row}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(pod, "cache_dir", lambda: tmp_path)
    pod._snapshots.cache_clear()
    try:
        assert pod._snapshots() == {"ValidNode": row}
    finally:
        pod._snapshots.cache_clear()
