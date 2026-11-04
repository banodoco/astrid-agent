"""Controls for bounded, path-free failed-install ACL observations."""
import json
import os
import sys
from pathlib import Path

import pytest

from scripts import journey_windows_acl_capture as capture
from scripts import ci_journey as journey


def test_candidate_selection_missing_multiple_and_alias(tmp_path):
    root = tmp_path / 'program'
    versions = root / 'versions'
    assert capture._candidate(root, tmp_path)[0] == 'missing'
    versions.mkdir(parents=True)
    assert capture._candidate(root, tmp_path)[0] == 'missing'
    one = versions / 'private-name'
    one.mkdir()
    assert capture._candidate(root, tmp_path) == ('selected', one)
    (versions / 'second').mkdir()
    assert capture._candidate(root, tmp_path)[0] == 'multiple'
    (versions / 'second').rmdir()
    one.rmdir()
    try:
        one.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('symlinks unavailable')
    assert capture._candidate(root, tmp_path)[0] == 'alias'
    assert capture._unalias(root / '..' / 'outside', tmp_path) == 'alias'


def test_collection_fixed_labels_missing_and_query_failure(tmp_path, monkeypatch):
    root = tmp_path / 'program'
    version = root / 'versions' / 'secret-version'
    leaf = version / 'py' / 'Lib' / 'site-packages' / capture.SCHEMA
    leaf.parent.mkdir(parents=True)
    leaf.touch()
    (tmp_path / 'tmp').mkdir()
    monkeypatch.setenv('UV_CACHE_DIR', str(tmp_path / '..' / 'secret-cache'))
    def query(path):
        if path == leaf:
            raise OSError('secret path or account')
        return {'status': 'ok', 'owner': 'other', 'aces': [{'trustee': 'users', 'type': 0, 'flags': 0, 'mask': 1}]}
    monkeypatch.setattr(capture, '_query', query)
    value = capture.collect(root, tmp_path)
    assert value['candidate'] == 'selected'
    assert value['objects']['installed'] == {'status': 'query-error'}
    assert value['objects']['source']['status'] == 'missing'
    assert value['objects']['cache']['status'] == 'alias'
    encoded = json.dumps(value)
    assert str(tmp_path) not in encoded and 'secret' not in encoded
    assert len(encoded) < 12000


def test_retention_and_capture_sink_failure(tmp_path, monkeypatch):
    evidence = tmp_path / 'run' / 'evidence' / 'fresh-install'
    evidence.mkdir(parents=True)
    monkeypatch.setattr(capture, 'collect', lambda *args: {'safe': True})
    capture.capture(tmp_path, tmp_path, evidence)
    journey.retain_named_evidence(evidence, 'before-cleanup')
    retained = tmp_path / 'run' / 'evidence-upload' / 'before-cleanup' / capture.OUTPUT
    assert json.loads(retained.read_text()) == {'safe': True}
    monkeypatch.setattr(capture, 'collect', lambda *args: (_ for _ in ()).throw(OSError('secret')))
    capture.capture(tmp_path, tmp_path, evidence)


@pytest.mark.skipif(sys.platform != 'win32', reason='requires Windows security APIs')
def test_windows_explicit_users_ace_and_hardlink(tmp_path):
    # Create fixture objects with descriptors at creation; no existing ACL is changed.
    import ctypes
    from ctypes import wintypes as w
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    P = ctypes.c_void_p
    class SA(ctypes.Structure):
        _fields_ = [('length', w.DWORD), ('descriptor', P), ('inherit', w.BOOL)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [w.LPCWSTR, w.DWORD, ctypes.POINTER(P), P]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = w.BOOL
    kernel.CreateDirectoryW.argtypes = [w.LPCWSTR, ctypes.POINTER(SA)]
    kernel.CreateDirectoryW.restype = w.BOOL
    kernel.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.POINTER(SA), w.DWORD, w.DWORD, w.HANDLE]
    kernel.CreateFileW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [P]
    def descriptor(sddl):
        sd = P()
        assert adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(sd), None)
        return sd, SA(ctypes.sizeof(SA), sd, False)
    # OW means owner rights; creator is granted full access through CREATOR OWNER inheritance.
    sd, sa = descriptor('D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)')
    parent = tmp_path / 'controlled'
    try:
        assert kernel.CreateDirectoryW(str(parent), ctypes.byref(sa))
    finally:
        kernel.LocalFree(sd)
    sd, sa = descriptor('D:P(A;;FA;;;OW)(A;;FR;;;BU)(A;;FA;;;SY)(A;;FA;;;BA)')
    leaf = parent / 'schema'
    try:
        handle = kernel.CreateFileW(str(leaf), 0x40000000, 7, ctypes.byref(sa), 1, 0x80, None)
        assert handle != P(-1).value
        kernel.CloseHandle(handle)
    finally:
        kernel.LocalFree(sd)
    parent_record, leaf_record = capture._query(parent), capture._query(leaf)
    assert parent_record['status'] == leaf_record['status'] == 'ok'
    assert parent_record['dacl_protected'] and leaf_record['dacl_protected']
    users = [ace for ace in leaf_record['aces'] if ace.get('trustee') == 'users']
    assert len(users) == 1 and users[0]['type'] == 0 and not users[0]['flags'] & 0x10
    try:
        os.link(leaf, parent / 'link')
    except OSError:
        pytest.skip('hard links unavailable')
    linked = capture._query(parent / 'link')['identity']
    identity = capture._query(leaf)['identity']
    assert identity['volume'] == linked['volume'] and identity['file_id'] == linked['file_id']
    assert identity['links'] >= 2


def test_aces_are_bounded_and_only_fixed_fields_are_serialized():
    import ctypes
    # A basic allow ACE containing a syntactically sized SID; no SID bytes escape.
    raw = bytes([0, 3, 20, 0]) + (1179817).to_bytes(4, 'little') + bytes([1, 1, 0, 0, 0, 0, 0, 5, 32, 0, 0, 0])
    storage = ctypes.create_string_buffer(raw)
    calls = []
    def get_ace(_dacl, index, output):
        calls.append(index)
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(storage)
        return True
    rows = capture._aces(1000, None, get_ace, lambda _: True, lambda _: 12, lambda _: 'users')
    assert len(rows) == capture.MAX_ACES == len(calls)
    assert rows[0] == {'type': 0, 'flags': 3, 'mask': 1179817, 'trustee': 'users'}
    assert len(json.dumps(rows)) < 3000
