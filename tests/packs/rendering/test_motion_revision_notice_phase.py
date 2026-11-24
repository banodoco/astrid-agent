"""RN-5b compact notices compare content identity, not upstream provenance."""

import pytest

from astrid.core.rendering.revision_notice import (
    MAX_PROGRESS_PHASE,
    pending_notice_phase,
    revision_notice_phase,
    revision_notice_text,
)


@pytest.mark.parametrize("upstream", ["e" * 40, None], ids=["witnessed", "unwitnessed"])
def test_pack_tree_mismatch_phase_shows_both_content_digests(upstream):
    editor = "1234567890abcdef" * 4
    resolved = "fedcba0987654321" * 4
    row = {
        "pack_id": "astrid_motion",
        "comparison": "mismatch",
        "digest_rule": "pack-tree-v1",
        "editor_revision": editor,
        "resolved_revision": resolved,
        "upstream_revision": upstream,
        "derived_from": None,
    }

    phase = revision_notice_phase(row)
    assert phase == (
        f"revision mismatch astrid_motion editor {editor[:29]}...{editor[-8:]}"
        f"; render {resolved[:29]}...{resolved[-8:]}"
    )
    assert len(phase) == MAX_PROGRESS_PHASE == 128
    assert pending_notice_phase({"phase": phase, "percent": 10}) == phase
    assert str(upstream) not in phase
    # Full text retains the unabridged identities and the distinct upstream
    # provenance, including when the selected bytes have no trusted upstream.
    assert revision_notice_text(row) == (
        f"Pack astrid_motion revision mismatch: editor {editor}; "
        f"render upstream {upstream} (resolved {resolved})"
    )


@pytest.mark.parametrize("digest_rule", [None, "other-rule"], ids=["legacy", "other"])
@pytest.mark.parametrize("has_upstream", [True, False], ids=["upstream", "derived"])
def test_non_pack_tree_phase_retains_legacy_revision_namespace(digest_rule, has_upstream):
    editor, upstream, derived, resolved = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    row = {
        "pack_id": "rendering",
        "comparison": "mismatch",
        "editor_revision": editor,
        "derived_from": derived,
        "resolved_revision": resolved,
    }
    if digest_rule is not None:
        row["digest_rule"] = digest_rule
    if has_upstream:
        row["upstream_revision"] = upstream
    legacy_render = upstream if has_upstream else derived
    assert revision_notice_phase(row) == (
        f"revision mismatch rendering editor {editor}; render {legacy_render}"
    )
    assert revision_notice_text(row) == (
        f"Pack rendering revision mismatch: editor {editor}; "
        f"render upstream {legacy_render} (resolved {resolved})"
    )
