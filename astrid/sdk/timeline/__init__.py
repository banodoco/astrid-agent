"""Modular API over the timeline working-copy implementation."""
__all__ = ["Checkout", "Clip", "Cut", "Word", "Voice", "TimelineEditError", "EDITABLE_NOTE",
           "draft_path", "drafts_root", "find_draft", "resolve_ids", "build_producer_batch",
           "capture_assembled_media", "capture_clip_add_media", "capture_clip_replace_media",
           "capture_parent_completion", "open", "Timeline", "CommitConflict", "CommitRefused"]


def __getattr__(name):
    if name in ("open", "Timeline"):
        from . import commit_session
        return getattr(commit_session, name)
    if name in ("CommitConflict", "CommitRefused"):
        from . import sync
        return getattr(sync, name)
    if name == 'build_producer_batch':
        from .producer import build_producer_batch
        return build_producer_batch
    if name in ("capture_assembled_media", "capture_clip_add_media", "capture_clip_replace_media",
                "capture_parent_completion"):
        from . import native_parent_media_commands
        return getattr(native_parent_media_commands, name)
    if name not in __all__:
        raise AttributeError(name)
    from astrid.sdk import timeline_checkout as legacy
    if name in __all__:
        return getattr(legacy, name)
    raise AttributeError(name)
