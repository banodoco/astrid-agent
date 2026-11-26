"""CPU-only proof of consent, resumability and Portable install custody."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock

from astrid.core.generation import comfy_portable as portable
from tests.core.generation.environment_states import (
    clean_machine,
    fake_disk_usage,
    fake_gpu,
    foreign_destination,
    snapshot,
)


class FakeSteps:
    """Tiny local files, with failures at the same boundaries as real steps."""

    archive_bytes = b"verified fake archive"

    def __init__(self):
        self.download_calls = 0
        self.extract_calls = 0
        self.fail_download_once = False
        self.fail_extract_once = False
        self.omit_files = set()
        self.parts_seen = []
        self.staging_seen = []
        self.steps = portable.Steps(self.download, self.extract, self.missing)

    @property
    def work_calls(self):
        return self.download_calls, self.extract_calls

    def download(self, pin, archive_path, part_path, progress):
        self.download_calls += 1
        archive_path, part_path = Path(archive_path), Path(part_path)
        self.parts_seen.append(part_path.read_bytes() if part_path.exists() else b"")
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        if self.fail_download_once:
            self.fail_download_once = False
            part_path.write_bytes(self.archive_bytes[:6])
            raise InterruptedError("download interrupted")
        part_path.write_bytes(self.archive_bytes)
        archive_path.write_bytes(self.archive_bytes)
        if progress is not None:
            progress("download", len(self.archive_bytes), len(self.archive_bytes))

    def extract(self, archive_path, staging_dir, pin):
        self.extract_calls += 1
        assert Path(archive_path).read_bytes() == self.archive_bytes
        staging_dir = Path(staging_dir)
        self.staging_seen.append(snapshot(staging_dir))
        root = staging_dir / pin["release"]["inner_directory"]
        for relative in pin["release"]["required_files"]:
            if relative in self.omit_files:
                continue
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"installed {relative}".encode())
        if self.fail_extract_once:
            self.fail_extract_once = False
            raise InterruptedError("extract interrupted")

    def missing(self, root, pin):
        return [
            relative for relative in pin["release"]["required_files"]
            if not (Path(root) / relative).is_file()
        ]


@pytest.fixture
def case(tmp_path):
    machine = clean_machine(tmp_path)
    pin = {
        "schema": "astrid.comfy-portable-pin.v1",
        "platform": "win32",
        "supported_gpu_patterns": [r"RTX (20|30|40|50)\d\d"],
        "note": "Synthetic unit test metadata, not an actual release.",
        "release": {
            "tag": "test-release",
            "url": "https://example.invalid/portable.zip",
            "archive_name": "portable.zip",
            "archive_format": "zip",
            "archive_bytes": 100,
            "archive_sha256": "a" * 64,
            "extracted_bytes": 300,
            "estimated_minutes": 1,
            "inner_directory": "ComfyUI_windows_portable",
            "required_files": ["python_embeded/python.exe", "ComfyUI/main.py"],
            "start_script": "run_nvidia_gpu.bat",
            "launch_args": ["--listen", "127.0.0.1"],
        },
    }
    kwargs = {
        "pin": pin,
        "platform": "win32",
        "gpu": fake_gpu("rtx4090"),
        "disk_usage": fake_disk_usage(30_000_000_000),
        "environ": machine.environ,
    }
    offer = portable.build_offer(machine.destination, **kwargs)
    fake = FakeSteps()
    metadata = machine.destination / ".astrid-install"
    return SimpleNamespace(
        machine=machine,
        pin=pin,
        kwargs=kwargs,
        fake=fake,
        consent=offer["offer_sha256"],
        destination=machine.destination,
        metadata=metadata,
        journal=metadata / "journal.json",
        root=machine.destination / pin["release"]["inner_directory"],
        archive=metadata / "archive" / pin["release"]["archive_name"],
        part=metadata / "archive" / (pin["release"]["archive_name"] + ".part"),
        staging=metadata / "staging",
    )


def apply(case, **overrides):
    kwargs = {**case.kwargs, "steps": case.fake.steps}
    kwargs.update(overrides)
    return portable.install_portable(case.destination, case.consent, **kwargs)


def read_journal(case):
    return json.loads(case.journal.read_text(encoding="utf-8"))


def interrupt_extract(case):
    case.fake.fail_extract_once = True
    with pytest.raises(InterruptedError, match="extract interrupted"):
        apply(case)
    assert read_journal(case)["phase"] == "downloaded"
    assert case.archive.is_file()


def test_steps_are_frozen(case):
    with pytest.raises(FrozenInstanceError):
        case.fake.steps.download = lambda *_: None


@pytest.mark.parametrize(
    ("consent", "code"),
    [(None, "consent_required"), ("wrong", "consent_mismatch"),
     (1, "consent_mismatch"), (True, "consent_mismatch")],
)
def test_consent_refusals_write_nothing(tmp_path, case, consent, code):
    before = snapshot(tmp_path)
    with pytest.raises(portable.PortableError) as caught:
        portable.install_portable(
            case.destination, consent, steps=case.fake.steps, **case.kwargs
        )
    assert caught.value.code == code
    assert snapshot(tmp_path) == before
    assert not case.destination.exists()
    assert case.fake.work_calls == (0, 0)


@pytest.mark.parametrize("reason", ["space", "gpu", "foreign"])
def test_refused_offer_writes_nothing(tmp_path, case, reason):
    kwargs = dict(case.kwargs)
    destination = case.destination
    if reason == "space":
        kwargs["disk_usage"] = fake_disk_usage(1)
        code = "insufficient_space"
    elif reason == "gpu":
        kwargs["gpu"] = fake_gpu("gtx1060")
        code = "unsupported_gpu"
    else:
        destination = foreign_destination(tmp_path)
        code = "destination_occupied"
    offer = portable.build_offer(destination, **kwargs)
    before = snapshot(tmp_path)
    with pytest.raises(portable.PortableError) as caught:
        portable.install_portable(
            destination, offer["offer_sha256"], steps=case.fake.steps, **kwargs
        )
    assert caught.value.code == code
    assert snapshot(tmp_path) == before
    assert not case.destination.exists()
    assert case.fake.work_calls == (0, 0)
    if reason == "foreign":
        assert (destination / "notes.txt").read_text(encoding="utf-8") == "My personal notes.\n"


def test_full_install_has_receipt_correct_paths_and_no_archive(case):
    progress = []
    result = apply(case, progress=lambda *event: progress.append(event))
    assert result == {
        "status": "installed",
        "resumed_from": None,
        "root": str(case.root),
        "comfy_dir": str(case.root / "ComfyUI"),
        "python": str(case.root / "python_embeded" / "python.exe"),
        "release": case.pin["release"]["tag"],
        "kept_previous": None,
    }
    journal = read_journal(case)
    assert journal["schema"] == "astrid.comfy-install-journal.v1"
    assert journal["offer_sha256"] == case.consent
    assert journal["release"] == case.pin["release"]["tag"]
    assert journal["phase"] == "complete"
    assert journal["renaming"] is False
    receipt = json.loads((case.metadata / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["release"] == result["release"]
    assert receipt["root"] == result["root"]
    assert receipt["python"] == result["python"]
    assert datetime.fromisoformat(receipt["installed_at"]).utcoffset() == timedelta(0)
    assert case.fake.missing(case.root, case.pin) == []
    assert not case.archive.exists()
    assert not case.part.exists()
    assert case.fake.work_calls == (1, 1)
    assert ("download", len(case.fake.archive_bytes), len(case.fake.archive_bytes)) in progress
    assert all(len(event) == 3 for event in progress)


@pytest.mark.parametrize("free_bytes", [30_000_000_000, 0])
def test_complete_install_is_a_no_write_noop(tmp_path, case, free_bytes):
    apply(case)
    kwargs = {**case.kwargs, "disk_usage": fake_disk_usage(free_bytes)}
    before = snapshot(tmp_path)
    offer = portable.build_offer(case.destination, **kwargs)
    assert offer["status"] == "offered"
    assert offer["state"] == "complete"
    assert offer["offer_sha256"] == case.consent
    assert offer["refusal"] is None
    fresh_steps = FakeSteps()
    result = portable.install_portable(
        case.destination, case.consent, steps=fresh_steps.steps, **kwargs
    )
    assert result["status"] == "already_installed"
    assert result["resumed_from"] == "complete"
    assert result["kept_previous"] is None
    assert fresh_steps.work_calls == (0, 0)
    assert snapshot(tmp_path) == before


def test_interrupted_extract_resumes_without_downloading(case):
    interrupt_extract(case)
    assert case.fake.work_calls == (1, 1)
    assert (case.staging / case.pin["release"]["inner_directory"]).is_dir()
    result = apply(case)
    assert result["status"] == "installed"
    assert result["resumed_from"] == "downloaded"
    assert case.fake.work_calls == (1, 2)
    assert read_journal(case)["phase"] == "complete"


def test_interrupted_download_resumes_the_existing_part(case):
    case.fake.fail_download_once = True
    with pytest.raises(InterruptedError, match="download interrupted"):
        apply(case)
    assert read_journal(case)["phase"] == "started"
    assert case.part.read_bytes() == case.fake.archive_bytes[:6]
    assert case.fake.work_calls == (1, 0)
    result = apply(case)
    assert result["resumed_from"] == "started"
    assert case.fake.work_calls == (2, 1)
    assert case.fake.parts_seen == [b"", case.fake.archive_bytes[:6]]
    assert read_journal(case)["phase"] == "complete"


def test_stale_staging_is_replaced_but_other_destination_files_are_kept(case):
    interrupt_extract(case)
    stale = case.staging / "stale" / "unrelated.txt"
    stale.parent.mkdir()
    stale.write_text("partial extraction", encoding="utf-8")
    personal = case.destination / "my-notes.txt"
    personal.write_text("keep this", encoding="utf-8")
    personal_stat = personal.stat()
    apply(case)
    assert case.fake.staging_seen == [{}, {}]
    assert not stale.exists()
    assert personal.read_text(encoding="utf-8") == "keep this"
    assert personal.stat().st_mtime_ns == personal_stat.st_mtime_ns


def test_incomplete_archive_is_not_promoted_and_archive_is_kept(case):
    case.fake.omit_files = {"python_embeded/python.exe"}
    with pytest.raises(portable.PortableError) as caught:
        apply(case)
    assert caught.value.code == "archive_incomplete"
    assert read_journal(case)["phase"] == "downloaded"
    assert case.archive.read_bytes() == case.fake.archive_bytes
    assert not case.root.exists()
    case.fake.omit_files.clear()
    assert apply(case)["resumed_from"] == "downloaded"
    assert case.fake.work_calls == (1, 2)


def test_crash_after_rename_recovers_without_extracting_again(case, monkeypatch):
    original = portable._write_journal

    def crash_before_extracted(path, record):
        if record["phase"] == "extracted":
            raise InterruptedError("crashed after rename")
        return original(path, record)

    with monkeypatch.context() as patch:
        patch.setattr(portable, "_write_journal", crash_before_extracted)
        with pytest.raises(InterruptedError, match="crashed after rename"):
            apply(case)
    assert read_journal(case)["phase"] == "downloaded"
    assert read_journal(case)["renaming"] is True
    assert case.fake.missing(case.root, case.pin) == []
    assert not (case.staging / case.pin["release"]["inner_directory"]).exists()
    result = apply(case)
    assert result["status"] == "installed"
    assert result["resumed_from"] == "downloaded"
    assert case.fake.work_calls == (1, 1)
    assert read_journal(case)["phase"] == "complete"


def test_held_filelock_refuses_another_installer(case):
    case.fake.fail_download_once = True
    with pytest.raises(InterruptedError):
        apply(case)
    with FileLock(str(case.metadata / "lock"), timeout=0):
        with pytest.raises(portable.PortableError) as caught:
            apply(case)
    assert caught.value.code == "install_in_progress"
    assert case.fake.work_calls == (1, 0)
    assert read_journal(case)["phase"] == "started"


def test_journal_with_another_offer_digest_changes_nothing(tmp_path, case):
    interrupt_extract(case)
    journal = read_journal(case)
    journal["offer_sha256"] = "b" * 64
    case.journal.write_text(json.dumps(journal), encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(portable.PortableError) as caught:
        apply(case)
    assert caught.value.code == "consent_mismatch"
    assert snapshot(tmp_path) == before
    assert case.fake.work_calls == (1, 1)


@pytest.mark.parametrize("situation", ["not_renaming", "still_staged", "incomplete_final"])
def test_unowned_final_root_is_not_replaced(tmp_path, case, situation):
    interrupt_extract(case)
    journal = read_journal(case)
    journal["renaming"] = situation != "not_renaming"
    case.journal.write_text(json.dumps(journal), encoding="utf-8")
    case.root.mkdir()
    (case.root / "personal.txt").write_text("do not move", encoding="utf-8")
    if situation == "incomplete_final":
        shutil.rmtree(case.staging)
    else:
        for relative in case.pin["release"]["required_files"]:
            path = case.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("a different installation", encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(portable.PortableError) as caught:
        apply(case)
    assert caught.value.code == "destination_changed"
    assert snapshot(tmp_path) == before
    assert case.fake.work_calls == (1, 1)


def test_broken_complete_install_is_kept_with_all_remaining_contents(case):
    apply(case)
    (case.root / "python_embeded" / "python.exe").unlink()
    (case.root / "personal.txt").write_text("saved by the person", encoding="utf-8")
    before = snapshot(case.root)
    result = apply(case)
    assert result["status"] == "installed"
    assert result["resumed_from"] == "complete"
    kept = Path(result["kept_previous"])
    assert kept.parent == case.destination
    assert re.fullmatch(re.escape(case.root.name) + r"\.broken-\d{8}T\d{6}", kept.name)
    assert snapshot(kept) == before
    assert (kept / "personal.txt").read_text(encoding="utf-8") == "saved by the person"
    assert not (kept / "python_embeded" / "python.exe").exists()
    assert case.fake.missing(case.root, case.pin) == []
    assert case.fake.work_calls == (2, 2)
    assert read_journal(case)["phase"] == "complete"
    assert read_journal(case)["offer_sha256"] == case.consent


def test_resume_offer_counts_existing_bytes_without_changing_consent(tmp_path, case):
    original = portable.build_offer(case.destination, **case.kwargs)
    interrupt_extract(case)
    done = sum(
        child.stat().st_size
        for directory in (case.metadata / "archive", case.staging)
        for child in directory.rglob("*")
        if child.is_file()
    )
    assert done > 0
    needed = portable.peak_bytes(case.pin["release"]) - done
    before = snapshot(tmp_path)
    offer = portable.build_offer(
        case.destination, **{**case.kwargs, "disk_usage": fake_disk_usage(needed)}
    )
    assert offer["status"] == "offered"
    assert offer["state"] == "ours"
    assert offer["resume"] == {"phase": "downloaded", "done_bytes": done}
    assert offer["peak_bytes"] == needed
    assert offer["offer_sha256"] == original["offer_sha256"]
    refused = portable.build_offer(
        case.destination, **{**case.kwargs, "disk_usage": fake_disk_usage(needed - 1)}
    )
    assert refused["refusal"]["code"] == "insufficient_space"
    assert refused["offer_sha256"] == original["offer_sha256"]
    assert snapshot(tmp_path) == before
