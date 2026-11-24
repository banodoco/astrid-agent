"""Generation consent choices, independent of optional engine packages."""
from contextlib import contextmanager
from contextvars import ContextVar

_COMMAND = ContextVar("astrid_code_consent_command", default=())


@contextmanager
def consent_command(argv):
    token = _COMMAND.set(tuple(argv))
    try:
        yield
    finally:
        _COMMAND.reset(token)


def current_consent_command():
    return _COMMAND.get()


def consent_scope(value: str) -> str:
    """Keep bare/stale code choices for the content-bound warning boundary."""
    if isinstance(value, str) and (value in {"user_env", "code"} or value.startswith("code=")):
        return value
    raise ValueError("consent must contain user_env or code=<token>; rent with `astrid machines claim`")
