"""Immutable producer pin and artifact checks for Astrid's Runtime bundle."""
from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "vendor_lock.py"
_spec = importlib.util.spec_from_file_location("vendor_lock", SCRIPT)
vendor_lock = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vendor_lock)
LOCK = ROOT / "runtime.lock.json"


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def _producer(tmp_path: Path, *, version: str = "sha256:" + "1" * 64, identity: str = "astrid-runtime") -> tuple[Path, dict]:
    producer = tmp_path / "producer"
    (producer / "contract").mkdir(parents=True)
    (producer / "src").mkdir()
    (producer / "src/a.txt").write_text("first artifact\n")
    (producer / "src/b.txt").write_text("second artifact\n")
    artifacts = {name: vendor_lock.digest(producer / name) for name in ("src/a.txt", "src/b.txt")}
    manifest = {"producer": identity, "version": version, "artifacts": artifacts}
    (producer / "contract/artifacts.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    _git(producer, "init", "-q")
    _git(producer, "add", "contract/artifacts.json", "src")
    subprocess.run(["git", "-C", str(producer), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "producer"], check=True)
    return producer, manifest


def _consumer(tmp_path: Path, producer: Path, manifest: dict) -> tuple[Path, Path]:
    root = tmp_path / "consumer"
    root.mkdir()
    (root / "copy").mkdir()
    (root / "copy/a.txt").write_text((producer / "src/a.txt").read_text())
    (root / "copy/b.txt").write_text((producer / "src/b.txt").read_text())
    lock = root / "runtime.lock.json"
    lock.write_text(json.dumps({
        "producer": "astrid-runtime", "manifest": "contract/artifacts.json",
        "producer_version": manifest["version"], "source_commit": _git(producer, "rev-parse", "HEAD"),
        "files": {
            "copy/a.txt": {"from": "src/a.txt", "sha256": manifest["artifacts"]["src/a.txt"]},
            "copy/b.txt": {"from": "src/b.txt", "sha256": manifest["artifacts"]["src/b.txt"]},
        },
    }, sort_keys=True))
    return root, lock


def _replace_manifest(producer: Path, manifest: dict) -> None:
    (producer / "contract/artifacts.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")


def test_one_root_lock_covers_all_known_runtime_paths() -> None:
    expected = {
        "astrid_runtime_client/__init__.py",
        "astrid_runtime_client/contract_metadata.py",
        "astrid_runtime_client/generated.py",
        "astrid/core/timeline/schema/timeline-config.schema.json",
        "tests/fixtures/visual-boundary-v1.json",
        *[f"tests/fixtures/runtime-responses/{name}.json" for name in (
            "events_page_1_200", "events_page_2_200", "invalid_cursor_400", "managed_outputs_200",
            "not_found_404", "publish_conflict_409", "task_admission_201", "task_read_200", "validation_error_422",
        )],
    }
    bundle = json.loads(LOCK.read_text())
    assert set(bundle["files"]) == expected
    assert len(bundle["files"]) == 14
    assert re.fullmatch(r"[0-9a-f]{40}", bundle["source_commit"])
    assert not any((ROOT / path).exists() for path in (
        "astrid_runtime_client/vendor.lock.json", "astrid/core/timeline/schema/vendor.lock.json",
        "tests/fixtures/vendor.lock.json", "tests/fixtures/runtime-responses/vendor.lock.json",
    ))
    assert vendor_lock.check(LOCK) == []


def test_checkout_with_same_bytes_but_wrong_commit_is_rejected(tmp_path: Path) -> None:
    producer, manifest = _producer(tmp_path)
    root, lock = _consumer(tmp_path, producer, manifest)
    # Advance producer HEAD without changing manifest or artifact bytes.
    (producer / "notes.txt").write_text("same bytes, different commit\n")
    _git(producer, "add", "notes.txt")
    subprocess.run(["git", "-C", str(producer), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "advance"], check=True)
    errors = vendor_lock.check(lock, producer, root)
    assert any(item.startswith("producer commit:") for item in errors)


@pytest.mark.parametrize("damage", ["manifest_missing", "file_missing", "unlisted"])
def test_missing_or_unlisted_producer_artifacts_fail(tmp_path: Path, damage: str) -> None:
    producer, manifest = _producer(tmp_path)
    root, lock = _consumer(tmp_path, producer, manifest)
    if damage == "manifest_missing":
        del manifest["artifacts"]["src/b.txt"]
        _replace_manifest(producer, manifest)
    elif damage == "file_missing":
        (producer / "src/b.txt").unlink()
    else:
        state = json.loads(lock.read_text())
        state["files"]["copy/b.txt"]["from"] = "src/unlisted.txt"
        lock.write_text(json.dumps(state))
    errors = vendor_lock.check(lock, producer, root)
    expected = {
        "manifest_missing": "producer artifact missing from manifest: src/b.txt",
        "file_missing": "producer artifact missing: src/b.txt",
        "unlisted": "producer artifact missing from manifest: src/unlisted.txt",
    }[damage]
    assert expected in errors


@pytest.mark.parametrize("field,value", [
    ("version", "sha256:" + "2" * 64),
    ("producer", "different-producer"),
    ("artifacts", {"src/a.txt": "sha256:" + "0" * 64, "src/b.txt": "sha256:" + "0" * 64}),
])
def test_stale_manifest_metadata_or_hash_is_rejected(tmp_path: Path, field: str, value: object) -> None:
    producer, manifest = _producer(tmp_path)
    root, lock = _consumer(tmp_path, producer, manifest)
    manifest[field] = value
    _replace_manifest(producer, manifest)
    errors = vendor_lock.check(lock, producer, root)
    expected = {
        "version": "producer manifest version differs from lock",
        "producer": "producer identity: expected astrid-runtime, got different-producer",
        "artifacts": "producer artifact hash is stale: src/a.txt",
    }[field]
    assert expected in errors


def test_uncommitted_manifest_tamper_is_rejected_even_when_bytes_would_match(tmp_path: Path) -> None:
    producer, manifest = _producer(tmp_path)
    root, lock = _consumer(tmp_path, producer, manifest)
    manifest["note"] = "tampered"
    _replace_manifest(producer, manifest)
    assert any("differs from committed HEAD" in item for item in vendor_lock.check(lock, producer, root))


def test_producer_byte_tamper_and_consumer_edit_are_rejected(tmp_path: Path) -> None:
    producer, manifest = _producer(tmp_path)
    root, lock = _consumer(tmp_path, producer, manifest)
    (producer / "src/a.txt").write_text("tampered\n")
    errors = vendor_lock.check(lock, producer, root)
    assert any("producer artifact hash is stale: src/a.txt" in item for item in errors)
    (root / "copy/b.txt").write_text("edited\n")
    assert "copy/b.txt: edited locally" in vendor_lock.check(lock, root=root)


def test_local_check_rejects_malformed_source_commit(tmp_path: Path) -> None:
    producer, manifest = _producer(tmp_path)
    root, lock = _consumer(tmp_path, producer, manifest)
    state = json.loads(lock.read_text())
    state["source_commit"] = None
    lock.write_text(json.dumps(state))
    assert "source_commit must be a full 40-character lowercase SHA" in vendor_lock.check(lock, root=root)


def test_update_preflights_then_copies_at_selected_commit(tmp_path: Path) -> None:
    producer, manifest = _producer(tmp_path)
    root, lock = _consumer(tmp_path, producer, manifest)
    (producer / "src/a.txt").write_text("updated artifact\n")
    manifest["artifacts"]["src/a.txt"] = vendor_lock.digest(producer / "src/a.txt")
    manifest["version"] = "1.1.0"
    (producer / "contract/artifacts.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    _git(producer, "add", "contract/artifacts.json", "src/a.txt")
    subprocess.run(["git", "-C", str(producer), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "valid update"], check=True)
    selected_commit = _git(producer, "rev-parse", "HEAD")
    lock_data = json.loads(lock.read_text())
    lock_data["source_commit"] = selected_commit
    lock.write_text(json.dumps(lock_data))
    vendor_lock.update(lock, producer, root)
    updated = json.loads(lock.read_text())
    assert updated["source_commit"] == selected_commit
    assert updated["producer_version"] == "1.1.0"
    assert (root / "copy/a.txt").read_text() == "updated artifact\n"
    assert vendor_lock.check(lock, producer, root) == []


def test_update_rejects_stale_manifest_without_mutating_consumer(tmp_path: Path) -> None:
    producer, manifest = _producer(tmp_path)
    root, lock = _consumer(tmp_path, producer, manifest)
    before_file, before_lock = (root / "copy/a.txt").read_bytes(), lock.read_bytes()
    (producer / "src/a.txt").write_text("unmanifested change\n")
    with pytest.raises(SystemExit, match="manifest is stale"):
        vendor_lock.update(lock, producer, root)
    assert (root / "copy/a.txt").read_bytes() == before_file
    assert lock.read_bytes() == before_lock


def test_update_checks_every_artifact_before_copying_the_first(tmp_path: Path) -> None:
    producer, manifest = _producer(tmp_path)
    root, lock = _consumer(tmp_path, producer, manifest)
    (producer / "src/a.txt").write_text("valid refreshed A\n")
    (producer / "src/b.txt").write_text("stale B\n")
    manifest["artifacts"]["src/a.txt"] = vendor_lock.digest(producer / "src/a.txt")
    # The committed manifest correctly covers A but deliberately contains B's old hash.
    (producer / "contract/artifacts.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    _git(producer, "add", "contract/artifacts.json", "src")
    subprocess.run(["git", "-C", str(producer), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "stale B hash"], check=True)
    state = json.loads(lock.read_text())
    state["source_commit"] = _git(producer, "rev-parse", "HEAD")
    lock.write_text(json.dumps(state))
    before_files = {name: (root / name).read_bytes() for name in ("copy/a.txt", "copy/b.txt")}
    before_lock = lock.read_bytes()
    with pytest.raises(SystemExit, match="src/b.txt: producer manifest is stale"):
        vendor_lock.update(lock, producer, root)
    assert {name: (root / name).read_bytes() for name in before_files} == before_files
    assert lock.read_bytes() == before_lock


def test_update_rejects_wrong_commit_without_mutation(tmp_path: Path) -> None:
    producer, manifest = _producer(tmp_path)
    root, lock = _consumer(tmp_path, producer, manifest)
    (producer / "notes.txt").write_text("same bytes, another commit\n")
    _git(producer, "add", "notes.txt")
    subprocess.run(["git", "-C", str(producer), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-q", "-m", "advance pin"], check=True)
    before_files = {name: (root / name).read_bytes() for name in ("copy/a.txt", "copy/b.txt")}
    before_lock = lock.read_bytes()
    with pytest.raises(SystemExit, match="does not match pinned source_commit"):
        vendor_lock.update(lock, producer, root)
    assert {name: (root / name).read_bytes() for name in before_files} == before_files
    assert lock.read_bytes() == before_lock


def test_cli_usage(tmp_path: Path) -> None:
    result = subprocess.run([sys.executable, str(SCRIPT), "update", "runtime.lock.json"], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stderr.startswith("usage: vendor_lock.py check <lock>")
    missing = subprocess.run([sys.executable, str(SCRIPT), "check", "runtime.lock.json", "--producer"], cwd=ROOT, capture_output=True, text=True)
    assert missing.returncode == 1
    assert missing.stderr.startswith("usage: vendor_lock.py check <lock>")
