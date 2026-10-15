"""Modular API over the timeline working-copy implementation."""
__all__ = ["Checkout", "Clip", "Cut", "Word", "Voice", "TimelineEditError", "EDITABLE_NOTE",
           "EditCommand", "CommandError", "draft_path", "drafts_root", "find_draft", "resolve_ids"]


def __getattr__(name):
    if name in {"EditCommand", "CommandError"}:
        from astrid.sdk.timeline import commands
        return getattr(commands, name)
    if name in __all__:
        from astrid.sdk import timeline_checkout as legacy
        return getattr(legacy, name)
    raise AttributeError(name)
