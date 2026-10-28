from __future__ import annotations

import hashlib
import http.server
import io
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.generation.engine_contracts import (
    DeadlineExceeded,
    DownloadError,
    IntegrityError,
    ModelLockValidationError,
    ModelSpec,
    UnsafePathError,
)
from astrid.core.generation import model_downloader as downloader


def spec(payload: bytes, *, destination="nested/model.bin", **changes) -> ModelSpec:
    values = dict(name="tiny-model", repository="owner/model", revision="a" * 40,
                  path="weights/model.bin", url=None, destination=destination,
                  bytes=len(payload), sha256=hashlib.sha256(payload).hexdigest())
    values.update(changes)
    return ModelSpec(**values)


class Response(io.BytesIO):
    def __init__(self, body: bytes, start: int, end: int, total: int,
                 *, status=206, content_range=None):
        super().__init__(body)
        self.status = status
        self.headers = {"Content-Range": content_range or f"bytes {start}-{end}/{total}"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

class RangeHandler(http.server.BaseHTTPRequestHandler):
    payload = b""
    requests = []

    def do_GET(self):
        value = self.headers.get("Range", "")
        start, end = map(int, value.removeprefix("bytes=").split("-"))
        type(self).requests.append((start, end))
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(type(self).payload)}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        self.wfile.write(type(self).payload[start:end + 1])

    def log_message(self, *_args):
        pass


def test_local_http_interruption_resumes_from_verified_range(tmp_path):
    payload = bytes(range(256)) * 3
    RangeHandler.payload = payload
    RangeHandler.requests = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_open = urllib.request.urlopen
    calls = 0

    def interrupt_after_first_range(request, timeout):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("simulated process interruption")
        local = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/weights/model.bin",
            headers={"Range": request.headers["Range"]})
        return real_open(local, timeout=timeout)

    try:
        root = tmp_path / "models"
        model = spec(payload)
        with pytest.raises(KeyboardInterrupt, match="simulated"):
            downloader._download_model(model, models_root=str(root),
                                       deadline=time.monotonic() + 10,
                                       opener=interrupt_after_first_range, range_bytes=64)
        part = root / "nested/model.bin.part"
        assert part.read_bytes() == payload[:64]
        result = downloader._download_model(model, models_root=str(root),
                                            deadline=time.monotonic() + 10,
                                            opener=interrupt_after_first_range, range_bytes=64)
        assert result.action == "resumed"
        assert result.resumed_from_bytes == 64
        assert result.verified is True
        assert (root / "nested/model.bin").read_bytes() == payload
        assert not part.exists()
        assert RangeHandler.requests[:3] == [(0, 63), (64, 127), (128, 191)]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_verified_existing_model_is_skipped(tmp_path):
    payload = b"verified bytes"
    root = tmp_path / "models"
    destination = root / "nested/model.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)
    result = downloader.download_model(spec(payload), models_root=str(root),
                                       deadline=time.monotonic() + 10,
                                       opener=lambda *_a, **_kw: pytest.fail("must skip"))
    assert result.action == "skipped"
    assert result.bytes == len(payload)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.verified is True


def test_verified_complete_part_is_promoted_without_network(tmp_path):
    payload = b"verified bytes"
    destination = tmp_path / "nested/model.bin"
    destination.parent.mkdir()
    part = destination.with_name(destination.name + ".part")
    part.write_bytes(payload)
    result = downloader.download_model(spec(payload), models_root=str(tmp_path), deadline=time.monotonic() + 10,
                                       opener=lambda *_a, **_kw: pytest.fail("full part must not redownload"))
    assert result.action == "resumed"
    assert result.resumed_from_bytes == len(payload)
    assert result.verified is True
    assert destination.read_bytes() == payload
    assert not part.exists()


@pytest.mark.parametrize("response_factory,match", [
    (lambda data, start, end: Response(data[start:end + 1], start, end, len(data), status=200), "range failed"),
    (lambda data, start, end: Response(data[start:end + 1], start, end, len(data),
                                       content_range="bytes 1-2/3"), "range failed"),
    (lambda data, start, end: Response(data[start:end], start, end, len(data)), "range failed"),
    (lambda data, start, end: Response(data[start:end + 1] + b"x", start, end, len(data)), "range failed"),
])
def test_rejects_truncated_oversized_and_wrong_ranges(tmp_path, monkeypatch, response_factory, match):
    monkeypatch.setattr(downloader.time, "sleep", lambda _seconds: None)
    payload = b"sixteen bytes!!"
    root = tmp_path / "models"

    def opener(request, timeout):
        assert timeout > 0
        start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
        return response_factory(payload, start, end)

    with pytest.raises(DownloadError, match=match):
        downloader._download_model(spec(payload), models_root=str(root),
                                   deadline=time.monotonic() + 10,
                                   opener=opener, range_bytes=len(payload))
    assert not (root / "nested/model.bin").exists()
    assert not (root / "nested/model.bin.part").exists()


def test_corrupt_complete_destination_fails_and_corrupt_partial_is_removed(tmp_path):
    payload = b"locked payload"
    root = tmp_path / "models"
    destination = root / "nested/model.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"wrong complete file")
    with pytest.raises(IntegrityError, match="existing model"):
        downloader.download_model(spec(payload), models_root=str(root),
                                  deadline=time.monotonic() + 10)
    destination.unlink()
    part = destination.with_name(destination.name + ".part")
    part.write_bytes(b"x" * len(payload))
    with pytest.raises(IntegrityError, match="SHA-256 mismatch"):
        downloader.download_model(spec(payload), models_root=str(root),
                                  deadline=time.monotonic() + 10,
                                  opener=lambda *_a, **_kw: pytest.fail("bad full part must not append"))
    assert not part.exists()
    assert not destination.exists()


def test_oversized_partial_is_quarantined_then_restarted(tmp_path):
    payload = b"good model"
    root = tmp_path / "models"
    destination = root / "nested/model.bin"
    destination.parent.mkdir(parents=True)
    part = destination.with_name(destination.name + ".part")
    part.write_bytes(b"too many bytes")
    requests = []

    def opener(request, timeout):
        assert timeout > 0
        start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
        requests.append(start)
        return Response(payload[start:end + 1], start, end, len(payload))

    result = downloader.download_model(spec(payload), models_root=str(root),
                                       deadline=time.monotonic() + 10, opener=opener)
    assert result.action == "downloaded"
    assert requests == [0]
    assert part.with_name(part.name + ".quarantine").read_bytes() == b"too many bytes"
    assert destination.read_bytes() == payload


def test_deadline_and_unsafe_paths_fail_before_network(tmp_path):
    payload = b"payload"
    root = tmp_path / "models"
    with pytest.raises(DeadlineExceeded):
        downloader.download_model(spec(payload), models_root=str(root),
                                  deadline=time.monotonic() - 1,
                                  opener=lambda *_a, **_kw: pytest.fail("expired deadline"))
    for invalid in ("../escape", "/absolute", "a//b", "a\\b", "C:/drive", "a/C:drive", "a\0b"):
        with pytest.raises(UnsafePathError):
            downloader.download_model(spec(payload, destination=invalid),
                                      models_root=str(root), deadline=time.monotonic() + 10,
                                      opener=lambda *_a, **_kw: pytest.fail("unsafe path"))
    for invalid_root in ("relative/root", str(root) + "\0"):
        with pytest.raises(UnsafePathError):
            downloader.download_model(spec(payload), models_root=invalid_root,
                                      deadline=time.monotonic() + 10)


def test_symlinked_model_parent_and_temporary_file_are_rejected(tmp_path):
    payload = b"payload"
    root = tmp_path / "models"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.mkdir()
    (root / "nested").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        downloader.download_model(spec(payload), models_root=str(root),
                                  deadline=time.monotonic() + 10)
    (root / "nested").unlink()
    (root / "nested").mkdir()
    (root / "nested/model.bin.part").symlink_to(outside / "target")
    with pytest.raises(UnsafePathError):
        downloader.download_model(spec(payload), models_root=str(root),
                                  deadline=time.monotonic() + 10)


def test_model_lock_rejects_mutable_or_conflicting_source_forms(tmp_path):
    payload = b"payload"
    with pytest.raises(ModelLockValidationError):
        downloader.download_model(spec(payload, revision="main"), models_root=str(tmp_path / "m"),
                                  deadline=time.monotonic() + 10)
    with pytest.raises(ModelLockValidationError):
        downloader.download_model(spec(payload, url="https://example.test/model"),
                                  models_root=str(tmp_path / "m"), deadline=time.monotonic() + 10)


@pytest.mark.parametrize("state", ["missing", "existing", "complete_part"])
def test_expired_deadline_never_skips_promotes_or_creates_directories(tmp_path, state):
    payload = b"locked model"
    root = tmp_path / "models"
    destination = root / "nested/model.bin"
    path = destination if state == "existing" else destination.with_name("model.bin.part")
    if state != "missing":
        path.parent.mkdir(parents=True)
        path.write_bytes(payload)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    with pytest.raises(DeadlineExceeded):
        downloader.download_model(spec(payload), models_root=str(root), deadline=time.monotonic() - 1,
                                  opener=lambda *_a, **_kw: pytest.fail("expired transfer"))
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")) == before
    if state != "missing":
        assert path.read_bytes() == payload


@pytest.mark.parametrize("state", ["existing", "complete_part", "download"])
def test_deadline_expiring_during_full_hash_never_reports_success(tmp_path, monkeypatch, state):
    payload = b"locked model"
    root = tmp_path / "models"
    destination = root / "nested/model.bin"
    part = destination.with_name("model.bin.part")
    destination.parent.mkdir(parents=True)
    hashed = destination if state == "existing" else part
    if state != "download":
        hashed.write_bytes(payload)
    clock = [0.0]
    monkeypatch.setattr(downloader, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original_open = Path.open

    class SlowRead:
        def __enter__(self):
            self.stream = original_open(hashed, "rb")
            return self

        def __exit__(self, *_args):
            self.stream.close()

        def read(self, count):
            block = self.stream.read(count)
            clock[0] = 11.0
            return block

    def slow_hash(path, *args, **kwargs):
        # The download's .chunk read and append are fast; only the full-file
        # hash crosses the deadline, including the last short read.
        if path == hashed and args == ("rb",):
            return SlowRead()
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", slow_hash)
    with pytest.raises(DeadlineExceeded):
        downloader.download_model(spec(payload), models_root=str(root), deadline=10.0,
                                  opener=lambda *_a, **_kw: Response(payload, 0, len(payload) - 1, len(payload)))
    assert destination.exists() is (state == "existing")
    with original_open(hashed, "rb") as stream:
        assert stream.read() == payload
    assert not destination.with_name("model.bin.chunk").exists()


@pytest.mark.parametrize("phase", ["open", "read"])
def test_transfer_deadline_is_not_retried_or_wrapped(tmp_path, monkeypatch, phase):
    payload = b"locked model"
    clock = [0.0]
    monkeypatch.setattr(downloader, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []

    class SlowResponse(Response):
        def read(self, count):
            block = super().read(count)
            clock[0] = 11.0
            return block

    def opener(*_args, **_kwargs):
        calls.append(phase)
        if phase == "open":
            raise DeadlineExceeded("opener deadline exceeded")
        return SlowResponse(payload, 0, len(payload) - 1, len(payload))

    with pytest.raises(DeadlineExceeded):
        downloader.download_model(spec(payload), models_root=str(tmp_path), deadline=10.0, opener=opener)
    assert calls == [phase]
    assert not (tmp_path / "nested/model.bin").exists()
    assert not (tmp_path / "nested/model.bin.chunk").exists()


def test_deadline_on_last_network_failure_retains_deadline_error(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(downloader, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda _s: None))
    attempts = []

    def opener(*_args, **_kwargs):
        attempts.append(True)
        if len(attempts) == 3:
            clock[0] = 11.0
        raise TimeoutError("network timed out")

    with pytest.raises(DeadlineExceeded):
        downloader.download_model(spec(b"model"), models_root=str(tmp_path), deadline=10.0, opener=opener)
    assert len(attempts) == 3


@pytest.mark.parametrize("deadline", [float("inf"), float("nan")])
def test_nonfinite_deadlines_are_refused_before_mutation(tmp_path, deadline):
    root = tmp_path / "models"
    with pytest.raises(DeadlineExceeded):
        downloader.download_model(spec(b"model"), models_root=str(root), deadline=deadline)
    assert not root.exists()


@pytest.mark.parametrize("obstruction", [
    "root_ancestor_symlink", "root_symlink", "root_parent_traversal", "root_regular",
    "parent_regular", "destination_directory", "part_directory", "chunk_directory",
])
def test_unsafe_target_trees_fail_before_network_or_outside_writes(tmp_path, obstruction):
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"preserve")
    root = tmp_path / "models"
    if obstruction == "root_ancestor_symlink":
        link = tmp_path / "link"
        link.symlink_to(outside, target_is_directory=True)
        root = link / "models"
    elif obstruction == "root_symlink":
        root.symlink_to(outside, target_is_directory=True)
    elif obstruction == "root_parent_traversal":
        root = tmp_path / "models" / ".." / "outside"
    elif obstruction == "root_regular":
        root.write_bytes(b"preserve")
    else:
        root.mkdir()
        if obstruction == "parent_regular":
            (root / "nested").write_bytes(b"preserve")
        else:
            (root / "nested").mkdir()
            suffix = {"destination_directory": "", "part_directory": ".part", "chunk_directory": ".chunk"}[obstruction]
            (root / "nested" / ("model.bin" + suffix)).mkdir()
    with pytest.raises(UnsafePathError):
        downloader.download_model(spec(b"model"), models_root=str(root), deadline=time.monotonic() + 10,
                                  opener=lambda *_a, **_kw: pytest.fail("unsafe target network access"))
    assert list(outside.iterdir()) == [sentinel]
    assert sentinel.read_bytes() == b"preserve"


@pytest.mark.parametrize("suffix", ["", ".part", ".chunk"])
def test_nonregular_target_files_are_rejected_without_opening(tmp_path, suffix):
    import os

    destination = tmp_path / "nested" / ("model.bin" + suffix)
    destination.parent.mkdir()
    os.mkfifo(destination)
    with pytest.raises(UnsafePathError):
        downloader.download_model(spec(b"model"), models_root=str(tmp_path), deadline=time.monotonic() + 10,
                                  opener=lambda *_a, **_kw: pytest.fail("nonregular target"))


def test_hardlinked_partial_cannot_append_to_a_file_outside_models_root(tmp_path):
    outside = tmp_path / "outside-model"
    outside.write_bytes(b"prefix")
    root = tmp_path / "models"
    part = root / "nested/model.bin.part"
    part.parent.mkdir(parents=True)
    part.hardlink_to(outside)
    with pytest.raises(UnsafePathError, match="hard links"):
        downloader.download_model(spec(b"prefix and suffix"), models_root=str(root), deadline=time.monotonic() + 10,
                                  opener=lambda *_a, **_kw: pytest.fail("hardlinked partial"))
    assert outside.read_bytes() == b"prefix"
    assert part.read_bytes() == b"prefix"


def test_windows_stream_destination_guard_without_windows_execution(tmp_path, monkeypatch):
    # Exercise spelling refusal only. Actual Windows path/IO qualification
    # remains outside this Linux test suite.
    monkeypatch.setattr(downloader, "os", SimpleNamespace(name="nt"))
    with pytest.raises(UnsafePathError, match="alternate data stream"):
        downloader.download_model(spec(b"model", destination="nested/model.bin:stream"),
                                  models_root=str(tmp_path), deadline=time.monotonic() + 10)
