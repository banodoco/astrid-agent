"""LS-12: resolving one ready entry builds that workflow alone."""
from astrid.core.generation import entries


def test_explicit_ready_entry_builds_only_the_named_workflow(monkeypatch):
    from vibecomfy.registry import ready

    original = ready.workflow_from_ready
    loaded = []

    def observe(identifier, **kwargs):
        loaded.append(identifier)
        return original(identifier, **kwargs)

    monkeypatch.setattr(ready, "workflow_from_ready", observe)
    rows = entries._ready_entries_for("image/qwen_image_2_1_turbo")
    assert [row["id"] for row in rows] == ["image/qwen_image_2_1_turbo"]
    assert rows[0]["ambiguity"] is None
    assert loaded == ["image/qwen_image_2_1_turbo"]
