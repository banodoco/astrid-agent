"""Calibration for test-only output hash accounting, not speed evidence."""
import hashlib
import io
import pytest
from tests._local_speed_hashes import OutputHashObserver

DATA = b"output 0"
DIGEST = hashlib.sha256(DATA).hexdigest()
CACHED_SHA256 = hashlib.sha256

def observe(monkeypatch):
    return OutputHashObserver({DIGEST:len(DATA)}).install(monkeypatch)

@pytest.mark.parametrize("method",["sha256","new","file_string","file_callable","file_cached","file_real"])
def test_supported_entry_points_are_counted_once(monkeypatch,tmp_path,method):
    meter=observe(monkeypatch)
    if method=="sha256": value=hashlib.sha256(DATA)
    elif method=="new": value=hashlib.new("sha256",DATA)
    elif method=="file_string": value=hashlib.file_digest(io.BytesIO(DATA),"sha256")
    elif method=="file_callable": value=hashlib.file_digest(io.BytesIO(DATA),hashlib.sha256)
    elif method=="file_cached": value=hashlib.file_digest(io.BytesIO(DATA),CACHED_SHA256)
    else:
        p=tmp_path/"output"; p.write_bytes(DATA)
        with p.open("rb") as f: value=hashlib.file_digest(f,"sha256",_bufsize=3)
    assert value.hexdigest()==DIGEST
    assert value.digest().hex()==DIGEST
    result=meter.snapshot()
    assert (result.completed_streams,result.bytes_hashed,result.coverage_gaps)==(1,len(DATA),())

def test_split_updates_are_one_full_stream(monkeypatch):
    meter=observe(monkeypatch); value=hashlib.sha256()
    for i in range(len(DATA)): value.update(memoryview(DATA)[i:i+1])
    assert value.hexdigest()==DIGEST
    assert meter.snapshot().completed_streams==1
    assert meter.snapshot().bytes_hashed==len(DATA)

def test_two_hashes_are_two_passes_not_one(monkeypatch):
    meter=observe(monkeypatch)
    hashlib.sha256(DATA).hexdigest()
    hashlib.file_digest(io.BytesIO(DATA),"sha256").hexdigest()
    assert meter.snapshot().completed_streams==2
    assert meter.snapshot().bytes_hashed==2*len(DATA)

def test_repeated_digest_and_copy_of_finished_hash_do_not_rehash(monkeypatch):
    meter=observe(monkeypatch); value=hashlib.sha256(DATA)
    assert value.hexdigest()==value.copy().hexdigest()==DIGEST
    assert meter.snapshot().completed_streams==1
    assert meter.snapshot().bytes_hashed==len(DATA)

def test_copied_prefix_only_counts_prefix_once(monkeypatch):
    meter=observe(monkeypatch); value=hashlib.sha256(DATA[:3]); other=value.copy()
    value.update(DATA[3:]); other.update(DATA[3:])
    assert value.hexdigest()==other.hexdigest()==DIGEST
    assert meter.snapshot().completed_streams==2
    assert meter.snapshot().bytes_hashed==3+2*(len(DATA)-3)

def test_unrelated_content_and_other_algorithms_are_not_output_hashes(monkeypatch):
    meter=observe(monkeypatch)
    hashlib.sha256(b"unrelated").hexdigest()
    hashlib.file_digest(io.BytesIO(DATA),"sha512").hexdigest()
    assert meter.snapshot().completed_streams==0

def test_preseeded_file_digest_callable_reports_unknown_not_zero_coverage(monkeypatch):
    meter=observe(monkeypatch); value=CACHED_SHA256(DATA[:3])
    assert hashlib.file_digest(io.BytesIO(DATA[3:]),lambda:value).hexdigest()==DIGEST
    assert meter.snapshot().completed_streams==0
    assert meter.snapshot().coverage_gaps

def test_failed_update_is_not_counted(monkeypatch):
    meter=observe(monkeypatch); value=hashlib.sha256()
    with pytest.raises(TypeError): value.update("not bytes")
    value.update(DATA)
    assert value.hexdigest()==DIGEST
    assert meter.snapshot().bytes_hashed==len(DATA)

def test_hash_not_explicitly_finalized_is_observed_at_snapshot(monkeypatch):
    meter=observe(monkeypatch); hashlib.sha256(DATA)
    assert meter.snapshot().completed_streams==1

def test_mismatched_declared_size_is_not_a_complete_pass(monkeypatch):
    meter=OutputHashObserver({DIGEST:len(DATA)+1}).install(monkeypatch)
    hashlib.sha256(DATA).hexdigest()
    assert meter.snapshot().completed_streams==0


def test_megabyte_file_is_counted_across_real_readinto_chunks(monkeypatch,tmp_path):
    data=bytes(range(256))*4096
    digest=hashlib.sha256(data).hexdigest()
    path=tmp_path/"megabyte.bin"; path.write_bytes(data)
    meter=OutputHashObserver({digest:len(data)}).install(monkeypatch)
    with path.open("rb") as stream:
        assert hashlib.file_digest(stream,"sha256",_bufsize=65536).hexdigest()==digest
    assert meter.snapshot().completed_streams==1
    assert meter.snapshot().bytes_hashed==1048576
