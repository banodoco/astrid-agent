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


# A consent choice as the MCP/JSON contract states it: user_env, or code=<12 lowercase hex>.
CONSENT_SCOPE_PATTERN = r"^(user_env|code=[0-9a-f]{12})$"


def consent_scope(value: str) -> str:
    """Keep bare/stale code choices for the content-bound warning boundary."""
    if isinstance(value, str) and (value in {"user_env", "code"} or value.startswith("code=")):
        return value
    if value == "paid":
        # Paid machines are claimed outside generation consent; keep the guidance.
        raise ValueError("consent must contain user_env or code=<token>; rent with `astrid machines claim`")
    raise ValueError("consent must contain user_env or code=<token>")


# Argument parsers that take a consent choice publish this schema (see astrid.mcp_server).
setattr(consent_scope, "json_schema", {"type": "string", "pattern": CONSENT_SCOPE_PATTERN})
