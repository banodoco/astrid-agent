"""P56 LE-2a: content-bound consent data; target enforcement is LE-2b."""
from __future__ import annotations

import copy
import json
import re

import pytest

NODE = {"repo": "https://example.test/pack.git", "commit": "a" * 40,
        "requirements_txt": "requirements.txt", "pip_no_deps": ["last==2", "first==1"]}


def test_record_shape_hash_and_token_are_stable():
    from vibecomfy.node_recipes import recipe_sha256

    from astrid.core.generation.node_approvals import (
        approval_record,
        consent_matches,
        consent_token,
    )

    record = approval_record(NODE, placement_class="local")
    assert set(record) == {"identity", "recipe_sha256", "approval_sha256", "placement_class"}
    assert record["identity"] == {key: NODE[key] for key in ("repo", "commit")}
    assert record["recipe_sha256"] == recipe_sha256({key: NODE[key] for key in ("requirements_txt", "pip_no_deps")})
    assert re.fullmatch(r"[0-9a-f]{64}", record["approval_sha256"])
    assert consent_token(record) == record["approval_sha256"][:12]
    assert consent_matches(consent_token(record), NODE, placement_class="local")
    assert approval_record(dict(reversed(list(NODE.items()))), placement_class="local") == record


@pytest.mark.parametrize("change,placement", [
    ({"repo": "https://example.test/other.git"}, "local"),
    ({"commit": "b" * 40}, "local"), ({"requirements_txt": "other.txt"}, "local"),
    ({"pip_no_deps": ["last==3", "first==1"]}, "local"),
    ({"pip_no_deps": ["first==1", "last==2"]}, "local"),
    ({}, "attached"), ({}, "runpod"),
])
def test_changed_identity_recipe_or_placement_detects_stale_approval_and_token(change, placement):
    from astrid.core.generation.node_approvals import (
        approval_matches,
        approval_record,
        consent_matches,
        consent_token,
    )

    before = approval_record(NODE, placement_class="local")
    changed = {**NODE, **change}
    after = approval_record(changed, placement_class=placement)
    assert consent_token(before) != consent_token(after)
    assert not consent_matches(consent_token(before), changed, placement_class=placement)
    assert not approval_matches(before, changed, placement_class=placement)


@pytest.mark.parametrize("token", [None, True, "code", "", "a" * 11, "a" * 13, "A" * 12, "g" * 12])
def test_bare_or_malformed_consent_is_not_an_approval(token):
    from astrid.core.generation.node_approvals import consent_matches

    assert not consent_matches(token, NODE, placement_class="local")


@pytest.mark.parametrize("change,placement", [
    ({"commit": "main"}, "local"), ({"commit": "a" * 39}, "local"),
    ({"repo": ""}, "local"), ({"repo": "bad\nrepo"}, "local"),
    ({"install_py": True}, "local"), ({}, "auto"), ({}, "managed"),
])
def test_record_requires_the_closed_node_shape_and_resolved_placement(change, placement):
    from astrid.core.generation.node_approvals import approval_record

    with pytest.raises(ValueError):
        approval_record({**NODE, **change}, placement_class=placement)


def test_entry_sidecar_round_trip_never_changes_or_approves_source_refs(tmp_path):
    from astrid.core.generation.node_approvals import (
        approval_matches,
        approval_record,
        read_entry_approvals,
        write_entry_approvals,
    )

    entry = tmp_path / "entry.json"
    source = tmp_path / "workflow.vibe.json"
    entry.write_text(json.dumps({"id": "test", "bundle": "bundle"}))
    source.write_text(json.dumps({"requirements": {"custom_node_refs": [NODE]}}))
    original = {path: path.read_bytes() for path in (entry, source)}
    assert read_entry_approvals(tmp_path) == []  # a source declaration is not consent
    record = approval_record(NODE, placement_class="runpod")
    write_entry_approvals(tmp_path, [record])
    loaded = read_entry_approvals(tmp_path)
    assert loaded == [record]
    assert approval_matches(loaded[0], NODE, placement_class="runpod")
    assert original == {path: path.read_bytes() for path in (entry, source)}
    record["identity"]["commit"] = "b" * 40
    assert read_entry_approvals(tmp_path) == loaded


def test_invalid_record_cannot_replace_an_existing_approval(tmp_path):
    from astrid.core.generation.node_approvals import (
        APPROVALS_FILENAME,
        approval_record,
        read_entry_approvals,
        write_entry_approvals,
    )

    (tmp_path / "entry.json").write_text("{}")
    valid = approval_record(NODE, placement_class="attached")
    write_entry_approvals(tmp_path, [valid])
    before = (tmp_path / APPROVALS_FILENAME).read_bytes()
    for field, value in (("approval_sha256", "0" * 64), ("recipe_sha256", "0" * 64), ("placement_class", "local")):
        with pytest.raises(ValueError):
            write_entry_approvals(tmp_path, [{**copy.deepcopy(valid), field: value}])
        assert (tmp_path / APPROVALS_FILENAME).read_bytes() == before
    assert read_entry_approvals(tmp_path) == [valid]
    with pytest.raises(ValueError):
        write_entry_approvals(tmp_path, [valid, valid])


def test_sidecar_rejects_malformed_or_symlinked_records(tmp_path):
    from astrid.core.generation.node_approvals import APPROVALS_FILENAME, read_entry_approvals

    sidecar = tmp_path / APPROVALS_FILENAME
    sidecar.write_text('{"not": "records"}')
    with pytest.raises(ValueError):
        read_entry_approvals(tmp_path)
    sidecar.unlink()
    other = tmp_path / "outside.json"
    other.write_text("[]")
    sidecar.symlink_to(other)
    with pytest.raises(ValueError, match="symlink"):
        read_entry_approvals(tmp_path)
