"""PK-8: the pack build hook contract.

The hook runs once per pack in an isolated stage (never in a live generation),
receives the frozen version-1 request, and the transaction writes
``.build/<source>.artifact-manifest.json`` and ``.build/admission.json`` with
exactly the keys ``version,packId,sourceDigest,digest,artifactManifestDigest,
committedRevision,transactionId``. Two builds of the same source are
byte-identical. Uses the real pinned Node/esbuild builder (no builder fake) and the
Git fixtures of ``test_transaction.py``.
"""
from __future__ import annotations

from pathlib import Path

import astrid.core.pack.transaction as transaction
from astrid.core.pack.identity import pack_digest
from astrid.core.pack.transaction import read_group
from tests.core.pack.test_transaction import (  # noqa: F401  (pytest fixtures)
    GROUP, Repository, accepted_refresh, browser, browser_records, install, packs_root, repo, tree_bytes,
)

REQUEST_KEYS = {"version", "transaction_id", "pack_id", "stage_root", "source_digest", "entry",
                "extension_manifest", "toolchain"}
RECORD_KEYS = {"version", "packId", "sourceDigest", "digest", "artifactManifestDigest",
               "committedRevision", "transactionId"}


def _spy(monkeypatch):
    calls = []
    real = transaction.build_pack_assets

    def spy(request):
        stage = Path(request["stage_root"])
        calls.append({"request": dict(request), "stage": stage,
                      "observed_digest": pack_digest(stage)})
        return real(request)

    monkeypatch.setattr(transaction, "build_pack_assets", spy)
    return calls


def test_hook_runs_once_per_pack_in_an_isolated_stage_and_writes_admission_records(
        repo: Repository, packs_root: Path, browser, monkeypatch) -> None:
    calls = _spy(monkeypatch)
    result = install(repo, packs_root, **browser, refresh=accepted_refresh)
    snapshot = read_group(packs_root)
    assert sorted(call["request"]["pack_id"] for call in calls) == sorted(GROUP)  # once per pack
    for call in calls:
        assert set(call["request"]) == REQUEST_KEYS and call["request"]["version"] == 1
        assert call["request"]["transaction_id"] == snapshot.transaction_id == result.transaction_id
        live = snapshot.paths[call["request"]["pack_id"]].resolve()
        # Isolated: the hook never ran inside the published generation.
        assert call["stage"].resolve() != live and not call["stage"].resolve().is_relative_to(live)
    records = browser_records(snapshot)
    for pack_id, path in snapshot.paths.items():
        assert set(records[pack_id]) == RECORD_KEYS
        assert (path / ".build" / f"{records[pack_id]['sourceDigest']}.artifact-manifest.json").is_file()
        assert (path / ".build" / "admission.json").is_file()


def test_checker_inputs_are_the_staged_source_of_each_selected_root(
        repo: Repository, packs_root: Path, browser, monkeypatch) -> None:
    calls = _spy(monkeypatch)
    install(repo, packs_root, **browser, refresh=accepted_refresh)
    snapshot = read_group(packs_root)
    stages = {call["request"]["pack_id"]: call for call in calls}
    assert len({call["stage"].resolve() for call in calls}) == len(GROUP)  # one root per pack
    records = browser_records(snapshot)
    for pack_id, call in stages.items():
        # The digest the hook is told to build is the staged source it was given,
        # and it is the source digest the published admission records.
        assert call["request"]["source_digest"] == call["observed_digest"]
        assert records[pack_id]["sourceDigest"] == call["request"]["source_digest"]
        assert call["request"]["entry"] == browser["builds"][pack_id]["entry"]
        assert call["request"]["toolchain"] == browser["toolchain"]


def test_two_builds_of_the_same_source_are_byte_identical(
        repo: Repository, tmp_path: Path, browser) -> None:
    first_root, second_root = tmp_path / "first" / "packs", tmp_path / "second" / "packs"
    install(repo, first_root, **browser, refresh=accepted_refresh)
    install(repo, second_root, **browser, refresh=accepted_refresh)
    first, second = read_group(first_root), read_group(second_root)
    for pack_id in GROUP:
        built_first = {name: data for name, data in tree_bytes(first.paths[pack_id] / ".build").items()
                       if name != "admission.json"}
        built_second = {name: data for name, data in tree_bytes(second.paths[pack_id] / ".build").items()
                        if name != "admission.json"}
        assert built_first and built_first == built_second
