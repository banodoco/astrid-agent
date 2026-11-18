"""LS-12: source identity reads Git inventory once, independent of file count."""
import os
import subprocess

from astrid.core.pack import source_setup


def test_source_identity_has_no_git_process_per_file_and_detects_content_edit(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    payload = root / "payload.py"
    payload.write_text("VALUE = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    calls = []
    original = source_setup._run_git

    def observe(repository, *args, **kwargs):
        calls.append(args)
        return original(repository, *args, **kwargs)

    monkeypatch.setattr(source_setup, "_run_git", observe)
    before = source_setup._tree_digest(root)
    first_calls = list(calls)
    assert [call[2:] for call in first_calls] == [("rev-parse", "--show-toplevel"), ("ls-files", "--full-name", "-z")]
    for index in range(25):
        (root / f"companion_{index}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    calls.clear()
    expanded = source_setup._tree_digest(root)
    assert expanded != before and calls == first_calls
    stamp = payload.stat()
    payload.write_text("VALUE = 'new'\n", encoding="utf-8")
    os.utime(payload, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    calls.clear()
    assert source_setup._tree_digest(root) != expanded
    assert calls == first_calls
