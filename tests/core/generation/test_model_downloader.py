from __future__ import annotations

import hashlib
import http.server
import io
import os
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


def test_verified_mounted_cache_is_copied_locally_without_network_or_cache_writes(tmp_path, monkeypatch):
    payload = b"exact locked cache bytes"
    cache = tmp_path / "mounted-cache"
    cached = cache / downloader._cache_relative(spec(payload))
    cached.parent.mkdir(parents=True)
    cached.write_bytes(payload)
    cached.chmod(0o444)
    before = (cached.stat().st_ino, cached.stat().st_mtime_ns, cached.stat().st_mode)
    root = tmp_path / "private-engine/models"
    model = spec(payload)
    reads = []
    real_fdopen = downloader.os.fdopen

    class ObservedRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, count):
            block = self.stream.read(count)
            reads.append(block)
            assert not (root / model.destination).exists()
            return block

    def observed_fdopen(fd, mode):
        stream = real_fdopen(fd, mode)
        return ObservedRead(stream) if mode == "rb" else stream

    monkeypatch.setattr(downloader.os, "fdopen", observed_fdopen)
    result = downloader.download_model(
        model, models_root=str(root), model_cache_root=str(cache),
        deadline=time.monotonic() + 10,
        opener=lambda *_a, **_kw: pytest.fail("verified cache must avoid network"),
    )
    destination = root / model.destination
    assert b"".join(reads) == payload
    assert reads[-1] == b""
    assert result.verified is True
    assert result.path == str(destination)
    assert result.action == "downloaded"
    assert destination.read_bytes() == payload
    assert destination.stat().st_ino != cached.stat().st_ino
    assert destination.stat().st_nlink == 1
    assert (cached.stat().st_ino, cached.stat().st_mtime_ns, cached.stat().st_mode) == before
    assert cached.read_bytes() == payload
    assert sorted(cache.rglob("*")) == sorted([cached.parent.parent, cached.parent, cached])
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize("state", ["missing_root", "missing_parent", "missing_entry", "wrong_size", "wrong_digest"])
def test_missing_or_incompatible_cache_uses_existing_pinned_downloader(tmp_path, state):
    payload = b"locked payload"
    cache = tmp_path / "cache"
    cached = cache / downloader._cache_relative(spec(payload))
    if state != "missing_root":
        cache.mkdir()
    if state not in ("missing_root", "missing_parent"):
        cached.parent.mkdir(parents=True)
    if state == "wrong_size":
        cached.write_bytes(b"short")
    elif state == "wrong_digest":
        cached.write_bytes(b"x" * len(payload))
    # A matching basename at another cache path is never consulted.
    if cache.exists():
        (cache / "model.bin").write_bytes(payload)
    before = {str(p.relative_to(cache)): p.read_bytes() for p in cache.rglob("*") if p.is_file()}
    calls = []

    def opener(request, timeout):
        assert timeout > 0
        assert request.full_url == f"https://huggingface.co/owner/model/resolve/{'a' * 40}/weights/model.bin"
        start, end = map(int, request.headers["Range"].removeprefix("bytes=").split("-"))
        calls.append((start, end))
        return Response(payload[start:end + 1], start, end, len(payload))

    root = tmp_path / "local-models"
    result = downloader._download_model(
        spec(payload), models_root=str(root), model_cache_root=str(cache),
        deadline=time.monotonic() + 10, opener=opener, range_bytes=4,
    )
    assert result.verified is True
    assert result.action == "downloaded"
    assert calls == [(0, 3), (4, 7), (8, 11), (12, 13)]
    assert (root / "nested/model.bin").read_bytes() == payload
    assert cached.read_bytes() == payload
    assert {str(p.relative_to(cache)): p.read_bytes() for p in cache.rglob("*")
            if p.is_file() and p != cached} == {path: data for path, data in before.items()
                                              if path != str(cached.relative_to(cache))}
    assert list((root / "nested").iterdir()) == [root / "nested/model.bin"]


@pytest.mark.parametrize("obstruction", [
    "root_ancestor_symlink", "root_symlink", "root_regular", "parent_symlink",
    "parent_regular", "leaf_symlink", "leaf_directory", "leaf_fifo", "leaf_hardlink",
])
def test_unsafe_mounted_cache_entries_fail_closed_without_network(tmp_path, obstruction):
    payload = b"locked payload"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(payload)
    cache = tmp_path / "cache"
    if obstruction == "root_ancestor_symlink":
        (tmp_path / "link").symlink_to(outside, target_is_directory=True)
        cache = tmp_path / "link/cache"
    elif obstruction == "root_symlink":
        cache.symlink_to(outside, target_is_directory=True)
    elif obstruction == "root_regular":
        cache.write_bytes(payload)
    else:
        cache.mkdir()
        key = cache / downloader._cache_relative(spec(payload)).parts[0]
        key.mkdir()
        if obstruction == "parent_symlink":
            (key / "nested").symlink_to(outside, target_is_directory=True)
        elif obstruction == "parent_regular":
            (key / "nested").write_bytes(payload)
        else:
            (key / "nested").mkdir()
            cached = cache / downloader._cache_relative(spec(payload))
            if obstruction == "leaf_symlink":
                cached.symlink_to(sentinel)
            elif obstruction == "leaf_directory":
                cached.mkdir()
            elif obstruction == "leaf_fifo":
                os.mkfifo(cached)
            elif obstruction == "leaf_hardlink":
                cached.hardlink_to(sentinel)
    root = tmp_path / "local-models"
    with pytest.raises(UnsafePathError, match="cache"):
        downloader.download_model(
            spec(payload), models_root=str(root), model_cache_root=str(cache),
            deadline=time.monotonic() + 10,
            opener=lambda *_a, **_kw: pytest.fail("unsafe cache must fail before network"),
        )
    assert sentinel.read_bytes() == payload
    assert not (root / "nested/model.bin").exists()
    assert not list(root.rglob("*.cache-*"))


@pytest.mark.parametrize("cache_root", ["relative/cache", "", "cache\0bad", "parent_traversal"])
def test_unsafe_cache_root_spelling_is_rejected(tmp_path, cache_root):
    if cache_root == "parent_traversal":
        cache_root = str(tmp_path / "cache/../outside")
    with pytest.raises(UnsafePathError, match="model_cache_root"):
        downloader.download_model(
            spec(b"payload"), models_root=str(tmp_path / "models"),
            model_cache_root=cache_root, deadline=time.monotonic() + 10,
            opener=lambda *_a, **_kw: pytest.fail("unsafe cache root"),
        )


@pytest.mark.parametrize("relationship", ["same", "local_under_cache", "cache_under_local"])
def test_cache_cannot_overlap_local_write_tree(tmp_path, relationship):
    cache = tmp_path / "cache"
    cache.mkdir()
    root = cache if relationship == "same" else (
        cache / "local-models" if relationship == "local_under_cache" else tmp_path
    )
    with pytest.raises(UnsafePathError, match="overlap"):
        downloader.download_model(
            spec(b"payload"), models_root=str(root), model_cache_root=str(cache),
            deadline=time.monotonic() + 10,
            opener=lambda *_a, **_kw: pytest.fail("cache must never be a local write tree"),
        )
    assert list(cache.iterdir()) == []


@pytest.mark.parametrize("destination", ["../outside/model.bin", "nested/../../outside/model.bin", "/outside/model.bin"])
def test_cache_consultation_cannot_escape_the_locked_relative_destination(tmp_path, destination):
    cache = tmp_path / "cache"
    cache.mkdir()
    root = tmp_path / "models"
    with pytest.raises(UnsafePathError):
        downloader.download_model(
            spec(b"payload", destination=destination), models_root=str(root),
            model_cache_root=str(cache), deadline=time.monotonic() + 10,
            opener=lambda *_a, **_kw: pytest.fail("escaped lock path must not download"),
        )
    assert not root.exists()
    assert list(cache.iterdir()) == []


@pytest.mark.parametrize("component", ["nested", "model.bin"])
def test_cache_symlink_replacement_between_stat_and_open_is_rejected(tmp_path, monkeypatch, component):
    payload = b"locked payload"
    cache = tmp_path / "cache"
    cached = cache / downloader._cache_relative(spec(payload))
    cached.parent.mkdir(parents=True)
    cached.write_bytes(payload)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "model.bin").write_bytes(payload)
    real_open = downloader.os.open
    replaced = False

    def raced_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if path == component and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            if component == "nested":
                cached.parent.rename(cache / "original")
                cached.parent.symlink_to(outside, target_is_directory=True)
            else:
                cached.unlink()
                cached.symlink_to(outside / "model.bin")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(downloader.os, "open", raced_open)
    with pytest.raises(UnsafePathError, match="cache"):
        downloader.download_model(
            spec(payload), models_root=str(tmp_path / "models"),
            model_cache_root=str(cache), deadline=time.monotonic() + 10,
            opener=lambda *_a, **_kw: pytest.fail("raced cache must not download"),
        )
    assert replaced
    assert not (tmp_path / "models/nested/model.bin").exists()
    assert (outside / "model.bin").read_bytes() == payload


@pytest.mark.parametrize("failure", ["interrupt", "deadline"])
def test_interrupted_cache_copy_never_publishes_or_leaves_a_temporary(tmp_path, monkeypatch, failure):
    payload = b"locked cache payload"
    cache = tmp_path / "cache"
    cached = cache / downloader._cache_relative(spec(payload))
    cached.parent.mkdir(parents=True)
    cached.write_bytes(payload)
    root = tmp_path / "local/models"
    part = root / "nested/model.bin.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"preserved prior range")
    clock = [0.0]
    monkeypatch.setattr(downloader.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(downloader, "_READ_SIZE", 3)
    real_fdopen = downloader.os.fdopen

    class InterruptedRead:
        def __init__(self, stream):
            self.stream = stream
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, count):
            self.calls += 1
            if self.calls == 2:
                if failure == "interrupt":
                    raise KeyboardInterrupt("interrupted cache copy")
                clock[0] = 11.0
            return self.stream.read(count)

    def interrupted_fdopen(fd, mode):
        stream = real_fdopen(fd, mode)
        return InterruptedRead(stream) if mode == "rb" else stream

    monkeypatch.setattr(downloader.os, "fdopen", interrupted_fdopen)
    with pytest.raises(KeyboardInterrupt if failure == "interrupt" else DeadlineExceeded):
        downloader.download_model(
            spec(payload), models_root=str(root), model_cache_root=str(cache), deadline=10.0,
            opener=lambda *_a, **_kw: pytest.fail("interrupted copy must stop"),
        )
    assert not (root / "nested/model.bin").exists()
    assert list(part.parent.iterdir()) == [part]
    assert part.read_bytes() == b"preserved prior range"
    assert cached.read_bytes() == payload


def test_installed_verified_local_bytes_remain_the_first_reuse_choice(tmp_path):
    payload = b"local verified payload"
    root = tmp_path / "private-engine/models"
    destination = root / "nested/model.bin"
    destination.parent.mkdir(parents=True)
    destination.write_bytes(payload)
    cache = tmp_path / "cache"
    cache.symlink_to(tmp_path / "outside", target_is_directory=True)
    result = downloader.download_model(
        spec(payload), models_root=str(root), model_cache_root=str(cache),
        deadline=time.monotonic() + 10,
        opener=lambda *_a, **_kw: pytest.fail("local verified reuse must skip"),
    )
    assert result.action == "skipped"
    assert result.path == str(destination)


def _pinned_response(payload, calls):
    def opener(request, timeout):
        assert timeout > 0
        start, end = map(int, request.headers['Range'].removeprefix('bytes=').split('-'))
        calls.append((request.full_url, start, end))
        return Response(payload[start:end + 1], start, end, len(payload))
    return opener


def test_first_download_publishes_verified_atomic_cache_and_second_launch_rehashes(tmp_path, monkeypatch):
    payload = b'locked first launch bytes'
    model, cache, calls = spec(payload), tmp_path / 'volume-cache', []
    cached = cache / downloader._cache_relative(model)
    real_replace = downloader.os.replace
    publications = []

    def observed_replace(source, target, **kwargs):
        if kwargs.get('dst_dir_fd') is not None:
            assert target == cached.name
            directory = kwargs['dst_dir_fd']
            assert os.fstat(directory).st_dev == cache.stat().st_dev
            assert not cached.exists()
            source_fd = os.open(source, os.O_RDONLY, dir_fd=directory)
            with os.fdopen(source_fd, 'rb') as stream:
                assert stream.read() == payload
            publications.append(source)
        return real_replace(source, target, **kwargs)

    monkeypatch.setattr(downloader.os, 'replace', observed_replace)
    result = downloader.download_model(model, models_root=str(tmp_path / 'first/models'),
        model_cache_root=str(cache), deadline=time.monotonic() + 10, opener=_pinned_response(payload, calls))
    assert result.verified and calls and len(publications) == 1
    assert cached.read_bytes() == payload and cached.stat().st_nlink == 1
    monkeypatch.setattr(downloader.os, 'replace', real_replace)
    second = downloader.download_model(model, models_root=str(tmp_path / 'second/models'),
        model_cache_root=str(cache), deadline=time.monotonic() + 10,
        opener=lambda *a, **kw: pytest.fail('second launch must reuse hashed persistent bytes'))
    assert second.verified and Path(second.path).read_bytes() == payload
    assert Path(second.path).stat().st_ino != cached.stat().st_ino
    assert len(calls) == 1 and not list(cache.rglob('*.cache-*'))
    assert not (cache / '.astrid-engine').exists()


@pytest.mark.parametrize('change', [
    {'name': 'other'}, {'repository': 'other/model'}, {'revision': 'b' * 40},
    {'path': 'other/model.bin'}, {'destination': 'other/model.bin'},
    {'bytes': 999}, {'sha256': 'b' * 64},
    {'url': 'https://example.test/model', 'repository': None, 'revision': None, 'path': None},
])
def test_cache_key_binds_every_exact_lock_entry_field(change):
    model = spec(b'locked model')
    assert downloader._cache_relative(model).parts[0] != downloader._cache_relative(spec(b'locked model', **change)).parts[0]


@pytest.mark.parametrize('failure', ['interrupt', 'deadline', 'size', 'hash'])
def test_failed_writeback_cannot_publish_a_valid_cache_entry(tmp_path, monkeypatch, failure):
    payload = b'locked downloaded model'
    model, cache = spec(payload), tmp_path / 'cache'
    source = tmp_path / 'verified/model.bin'
    source.parent.mkdir()
    source.write_bytes(payload)
    cached = cache / downloader._cache_relative(model)
    binding = downloader._CacheBinding(str(cache))
    real_fdopen = downloader.os.fdopen
    clock = [0.0]
    monkeypatch.setattr(downloader.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(downloader, '_READ_SIZE', 3)
    if failure == 'size':
        source.write_bytes(b'short')
    elif failure == 'hash':
        source.write_bytes(b'x' * len(payload))

    class InterruptedRead:
        def __init__(self, stream):
            self.stream, self.calls = stream, 0
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, count):
            self.calls += 1
            if self.calls == 2:
                if failure == 'interrupt':
                    raise KeyboardInterrupt('interrupted writeback')
                if failure == 'deadline':
                    clock[0] = 11.0
            return self.stream.read(count)

    monkeypatch.setattr(downloader.os, 'fdopen', lambda fd, mode:
        InterruptedRead(real_fdopen(fd, mode)) if mode == 'rb' else real_fdopen(fd, mode))
    error = {'interrupt': KeyboardInterrupt, 'deadline': DeadlineExceeded,
             'size': UnsafePathError, 'hash': IntegrityError}[failure]
    with pytest.raises(error):
        downloader._write_cached_model(model, binding=binding, relative=downloader._cache_relative(model),
                                       destination=source, deadline=10.0)
    assert not cached.exists() and not list(cache.rglob('*.cache-*'))


@pytest.mark.parametrize('phase', ['read', 'write'])
@pytest.mark.parametrize('change', ['mount', 'device', 'directory'])
def test_cache_revalidates_mount_device_and_path_during_copy(tmp_path, monkeypatch, phase, change):
    payload, cache = b'locked cache bytes', tmp_path / 'cache'
    model = spec(payload)
    cached = cache / downloader._cache_relative(model)
    cached.parent.mkdir(parents=True)
    if phase == 'read':
        cached.write_bytes(payload)
    source = tmp_path / 'local/model.bin'
    source.parent.mkdir()
    source.write_bytes(payload)
    identity = [71, cache.stat().st_dev, '/workspace']
    monkeypatch.setattr(downloader, '_mount_identity', lambda root: tuple(identity))
    binding = downloader._CacheBinding(str(cache), device=identity[1], mount_id=identity[0])
    real_fdopen = downloader.os.fdopen
    changed = []

    class ChangedRead:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def read(self, count):
            block = self.stream.read(count)
            if not changed:
                changed.append(True)
                if change == 'directory':
                    cache.rename(tmp_path / 'retired-cache')
                    cache.mkdir()
                elif change == 'mount':
                    identity[0] += 1
                else:
                    identity[1] += 1
            return block

    monkeypatch.setattr(downloader.os, 'fdopen', lambda fd, mode:
        ChangedRead(real_fdopen(fd, mode)) if mode == 'rb' else real_fdopen(fd, mode))
    with pytest.raises(UnsafePathError, match='binding changed'):
        if phase == 'read':
            downloader._copy_cached_model(model, binding=binding, relative=downloader._cache_relative(model),
                                          destination=source.parent / 'installed.bin', deadline=time.monotonic() + 10)
        else:
            downloader._write_cached_model(model, binding=binding, relative=downloader._cache_relative(model),
                                           destination=source, deadline=time.monotonic() + 10)
    assert changed and not (source.parent / 'installed.bin').exists()
    assert not list(tmp_path.rglob('*.cache-*'))


@pytest.mark.parametrize('device,mount_id', [(100, 71), (99, 72), (99, None), (None, 71), (True, 71)])
def test_authenticated_cache_binding_refuses_changed_or_incomplete_evidence_before_download(tmp_path, monkeypatch, device, mount_id):
    cache = tmp_path / 'cache'
    monkeypatch.setattr(downloader, '_mount_identity', lambda root: (71, 99, '/workspace'))
    with pytest.raises(UnsafePathError):
        downloader.download_model(spec(b'locked bytes'), models_root=str(tmp_path / 'models'),
            model_cache_root=str(cache), model_cache_device=device, model_cache_mount_id=mount_id,
            deadline=time.monotonic() + 10, opener=lambda *a, **kw: pytest.fail('unbound download'))
    assert not list(tmp_path.rglob('*.cache-*'))


@pytest.mark.parametrize('case', ['valid', 'duplicate', 'nested', 'device', 'malformed', 'missing'])
def test_cache_mountinfo_requires_unique_live_mount_and_device(tmp_path, monkeypatch, case):
    cache = tmp_path / 'cache'
    cache.mkdir()
    device = cache.stat().st_dev
    major_minor = f'{os.major(device)}:{os.minor(device)}'
    line = f'71 1 {major_minor} / {cache} rw - fuse opaque rw\n'
    table = f'1 0 {major_minor} / / rw - overlay opaque rw\n' + line
    if case == 'duplicate':
        table += line.replace('71 ', '72 ', 1)
    elif case == 'nested':
        table += line.replace('71 ', '72 ', 1).replace(f' {cache} ', f' {cache}/nested ')
    elif case == 'device':
        table = table.replace(major_minor, '100:100')
    elif case == 'malformed':
        table += 'malformed mount evidence\n'
    elif case == 'missing':
        table = None
    monkeypatch.setattr(downloader, '_read_mountinfo', lambda: table)
    if case == 'valid':
        assert downloader._mount_identity(cache) == (71, device, str(cache))
    elif case == 'missing':
        with pytest.raises(UnsafePathError, match='authenticated'):
            downloader._CacheBinding(str(cache), device=device, mount_id=71)
    else:
        with pytest.raises(UnsafePathError, match='model cache'):
            downloader._mount_identity(cache)


def test_writeback_refuses_symlinked_local_source_ancestor(tmp_path):
    payload = b'locked bytes'
    actual = tmp_path / 'actual'
    actual.mkdir()
    (actual / 'model.bin').write_bytes(payload)
    linked = tmp_path / 'linked'
    linked.symlink_to(actual, target_is_directory=True)
    cache = tmp_path / 'cache'
    model = spec(payload)
    with pytest.raises(UnsafePathError, match='symlink'):
        downloader._write_cached_model(model, binding=downloader._CacheBinding(str(cache)),
            relative=downloader._cache_relative(model), destination=linked / 'model.bin', deadline=time.monotonic() + 10)
    assert not list(cache.rglob('*.cache-*'))
    assert not (cache / downloader._cache_relative(model)).exists()


def test_writeback_refuses_temporary_acquiring_a_hardlink(tmp_path, monkeypatch):
    payload = b'locked model bytes'
    model, cache = spec(payload), tmp_path / 'cache'
    source = tmp_path / 'verified/model.bin'
    source.parent.mkdir()
    source.write_bytes(payload)
    real_open = downloader.os.open
    linked = []

    def raced_open(path, flags, *args, **kwargs):
        descriptor = real_open(path, flags, *args, **kwargs)
        if isinstance(path, str) and '.cache-' in path and flags & os.O_CREAT:
            directory = kwargs['dir_fd']
            os.link(path, 'unsafe-alias', src_dir_fd=directory, dst_dir_fd=directory)
            linked.append(True)
        return descriptor

    monkeypatch.setattr(downloader.os, 'open', raced_open)
    with pytest.raises(UnsafePathError, match='temporary'):
        downloader._write_cached_model(model, binding=downloader._CacheBinding(str(cache)),
            relative=downloader._cache_relative(model), destination=source, deadline=time.monotonic() + 10)
    assert linked and not (cache / downloader._cache_relative(model)).exists()
    assert not list(cache.rglob('*.cache-*'))
    assert source.read_bytes() == payload


@pytest.mark.parametrize('state', ['missing', 'wrong_size', 'wrong_hash'])
def test_insufficient_cache_capacity_refuses_cold_or_repair_before_pinned_download(tmp_path, monkeypatch, state):
    payload, cache = b'locked model payload', tmp_path / 'volume-cache'
    model = spec(payload)
    cached = cache / downloader._cache_relative(model)
    old = None
    if state != 'missing':
        cached.parent.mkdir(parents=True)
        old = b'short' if state == 'wrong_size' else b'x' * len(payload)
        cached.write_bytes(old)
    monkeypatch.setattr(downloader.os, 'fstatvfs', lambda descriptor:
                        SimpleNamespace(f_bavail=len(payload) - 1, f_frsize=1))
    root = tmp_path / 'local/models'
    with pytest.raises(DownloadError, match='capacity'):
        downloader.download_model(model, models_root=str(root), model_cache_root=str(cache),
            deadline=time.monotonic() + 10,
            opener=lambda *a, **kw: pytest.fail('insufficient cache capacity must precede pinned network I/O'))
    assert not (root / model.destination).exists()
    assert not list(root.rglob('*.part')) and not list(tmp_path.rglob('*.cache-*'))
    assert cached.read_bytes() == old if old is not None else not cached.exists()


def test_warm_verified_cache_reuse_requires_no_new_cache_capacity(tmp_path, monkeypatch):
    payload, cache = b'locked cached payload', tmp_path / 'volume-cache'
    model = spec(payload)
    cached = cache / downloader._cache_relative(model)
    cached.parent.mkdir(parents=True)
    cached.write_bytes(payload)
    checked = []
    def unavailable(descriptor):
        checked.append(descriptor)
        return SimpleNamespace(f_bavail=0, f_frsize=4096)
    monkeypatch.setattr(downloader.os, 'fstatvfs', unavailable)
    result = downloader.download_model(model, models_root=str(tmp_path / 'local/models'),
        model_cache_root=str(cache), deadline=time.monotonic() + 10,
        opener=lambda *a, **kw: pytest.fail('valid warm cache must avoid pinned network I/O'))
    assert result.verified and Path(result.path).read_bytes() == payload
    assert cached.read_bytes() == payload and checked == []


def test_capacity_consumed_during_download_refuses_writeback_without_cache_publication(tmp_path, monkeypatch):
    payload, cache = b'locked downloaded payload', tmp_path / 'volume-cache'
    model, checks, downloads = spec(payload), [], []
    def available(descriptor):
        checks.append(descriptor)
        return SimpleNamespace(f_bavail=len(payload) if len(checks) == 1 else 0, f_frsize=1)
    monkeypatch.setattr(downloader.os, 'fstatvfs', available)
    root = tmp_path / 'local/models'
    with pytest.raises(DownloadError, match='capacity'):
        downloader.download_model(model, models_root=str(root), model_cache_root=str(cache),
            deadline=time.monotonic() + 10, opener=_pinned_response(payload, downloads))
    assert downloads and len(checks) == 2
    assert (root / model.destination).read_bytes() == payload
    assert not (cache / downloader._cache_relative(model)).exists()
    assert not list(tmp_path.rglob('*.cache-*'))
