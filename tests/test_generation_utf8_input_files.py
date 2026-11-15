"""JSON input files are read as UTF-8 even where the default code page is ANSI (Windows)."""

import json
from pathlib import Path

import pytest

from astrid.core.generation import job_inputs


@pytest.fixture
def ansi_default(monkeypatch):
    """Simulate a Windows default code page: a read with no encoding decodes as cp1252."""
    real_read_text = Path.read_text

    def read_text(self, encoding=None, errors=None):
        return real_read_text(self, encoding=encoding or "cp1252", errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)


NON_ASCII = {"prompt": "Ein Hund über dem Meer — 日本語"}


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_inputs_json_is_read_as_utf8(tmp_path, ansi_default, monkeypatch, bom):
    from astrid.core.generation import cli

    source = tmp_path / "inputs.json"
    source.write_bytes(bom + json.dumps(NON_ASCII, ensure_ascii=False).encode("utf-8"))

    assert cli._json_inputs(str(source)) == NON_ASCII


@pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
def test_file_map_document_is_read_as_utf8(tmp_path, ansi_default, bom):
    image = tmp_path / "frame é.png"
    image.write_bytes(b"png")
    document = tmp_path / "map.json"
    document.write_bytes(bom + json.dumps({"frame": image.name}, ensure_ascii=False).encode("utf-8"))
    ports = [{"name": "frames", "type": "file_map"}]

    archive = job_inputs.write_archive(tmp_path / "inputs.zip", {"frames": str(document)}, ports)

    import zipfile
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.read("files/0.png") == b"png"
