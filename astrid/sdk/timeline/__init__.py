"""Modular API over the timeline working-copy implementation."""
__all__ = ["Checkout", "Clip", "Cut", "Word", "Voice", "TimelineEditError", "EDITABLE_NOTE",
           "draft_path", "drafts_root", "find_draft", "resolve_ids"]


def __getattr__(name):
    from astrid.sdk import timeline_checkout as legacy
    if name in __all__:
        return getattr(legacy, name)
    raise AttributeError(name)
