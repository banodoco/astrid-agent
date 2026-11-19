"""Host admission binds all roots; later edits require fresh validation."""
import os

import pytest

from astrid.core.execution import generic_host
from astrid.core.pack.identity import pack_digest
from astrid.core.pack.imports import validate_selected_pack_map


@pytest.mark.parametrize("additional_source", [False, True])
def test_host_admission_binds_all_roots_and_rechecks_later_edits(tmp_path, additional_source):
    roots = []
    for name in ("ls12_action", "ls12_dependency", "ls12_extra"):
        root = tmp_path / name
        root.mkdir()
        (root / "helper.py").write_bytes(b"VALUE = 'old'\n")
        (root / "resource.txt").write_bytes(b"included in the content identity\n")
        roots.append(root.resolve())
    selected = {root.name: {"root": str(root), "source_digest": pack_digest(root)} for root in roots[:2]}
    source_roots = [roots[0], *([roots[2]] if additional_source else [])]
    admission = {"selected_pack_map": selected, "source_roots": list(map(str, source_roots)),
                 "source_digest": generic_host._source_digest_for_roots(source_roots)}
    generic_host._verify_admitted_source(admission)
    assert validate_selected_pack_map(selected) == selected

    included = roots if additional_source else roots[:2]
    for root in included:
        helper = root / "helper.py"
        original = helper.read_bytes()
        stamp = helper.stat()
        changed = original.replace(b"old", b"new", 1)
        assert changed != original and len(changed) == len(original)
        helper.write_bytes(changed)
        os.utime(helper, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        assert helper.stat().st_size == stamp.st_size
        assert helper.stat().st_mtime_ns == stamp.st_mtime_ns
        with pytest.raises(generic_host.HostError, match="source digest changed"):
            generic_host._verify_admitted_source(admission)
        if root.name in selected:
            with pytest.raises(ValueError, match="source digest changed"):
                validate_selected_pack_map(selected)
        else:
            assert validate_selected_pack_map(selected) == selected
        helper.write_bytes(original)
        os.utime(helper, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    generic_host._verify_admitted_source(admission)


def test_host_admission_does_not_substitute_another_selected_root_digest(tmp_path):
    roots = []
    for name, content in (("ls12_first", b"first"), ("ls12_second", b"other")):
        root = tmp_path / name
        root.mkdir()
        (root / "resource.txt").write_bytes(content)
        roots.append(root.resolve())
    selected = {root.name: {"root": str(root), "source_digest": pack_digest(root)} for root in roots}
    admission = {"selected_pack_map": selected, "source_roots": [str(roots[0])],
                 "source_digest": selected[roots[1].name]["source_digest"]}
    with pytest.raises(generic_host.HostError, match="source digest changed"):
        generic_host._verify_admitted_source(admission)


@pytest.mark.parametrize("member", ["helper.py", "resource.txt"])
def test_fresh_host_admission_detects_edited_bytes_without_a_read_count_gate(tmp_path, member):
    """Keep the security probe reachable while verify-once is deferred."""
    root = (tmp_path / "ls12_fresh").resolve()
    root.mkdir()
    (root / "helper.py").write_bytes(b"VALUE = 'old'\n")
    (root / "resource.txt").write_bytes(b"resource old\n")
    selected = {root.name: {"root": str(root), "source_digest": pack_digest(root)}}
    admission = {"selected_pack_map": selected, "source_roots": [str(root)],
                 "source_digest": selected[root.name]["source_digest"]}
    generic_host._verify_admitted_source(admission)
    validate_selected_pack_map(selected)
    path = root / member
    stamp = path.stat()
    original = path.read_bytes()
    changed = original.replace(b"old", b"new", 1)
    assert changed != original and len(changed) == len(original)
    path.write_bytes(changed)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert path.stat().st_size == stamp.st_size
    assert path.stat().st_mtime_ns == stamp.st_mtime_ns
    with pytest.raises(generic_host.HostError, match="source digest changed"):
        generic_host._verify_admitted_source(admission)
    with pytest.raises(ValueError, match="source digest changed"):
        validate_selected_pack_map(selected)
