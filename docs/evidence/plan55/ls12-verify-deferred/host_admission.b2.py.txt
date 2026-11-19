"""One host admission shares verified digests; every later admission is fresh."""
import os
from collections import Counter
from pathlib import Path

import pytest

from astrid.core.execution import generic_host
from astrid.core.pack.identity import pack_digest
from astrid.core.pack.imports import validate_selected_pack_map


@pytest.mark.parametrize("additional_source", [False, True])
def test_host_admission_reads_each_selected_source_once(tmp_path, monkeypatch, additional_source):
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
    reads = Counter()
    read_bytes = Path.read_bytes

    def observe(path):
        if path.parent in roots:
            reads[path] += 1
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", observe)
    generic_host._verify_admitted_source(admission)
    included = roots if additional_source else roots[:2]
    assert reads == {root / name: 1 for root in included for name in ("helper.py", "resource.txt")}

    reads.clear()
    validate_selected_pack_map(selected)
    assert reads == {root / name: 1 for root in roots[:2] for name in ("helper.py", "resource.txt")}
    helper = roots[0] / "helper.py"
    stamp = helper.stat()
    helper.write_bytes(b"VALUE = 'new'\n")
    os.utime(helper, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert helper.stat().st_size == stamp.st_size
    assert helper.stat().st_mtime_ns == stamp.st_mtime_ns
    with pytest.raises(generic_host.HostError, match="source digest changed"):
        generic_host._verify_admitted_source(admission)
    with pytest.raises(ValueError, match="source digest changed"):
        validate_selected_pack_map(selected)


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
