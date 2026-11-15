"""Shared log and receipt redaction; standard-library dependencies only."""

from __future__ import annotations

import getpass
import json
import re
from pathlib import Path
from typing import Any, Mapping

_SENSITIVE_KEYS = re.compile(
    r"(?:credential|token|secret|password|prompt|environment(?:Value)?|env|media|signed.?url|authorization|api[_-]?key|bearer|path|root)$",
    re.IGNORECASE,
)
_PRIVATE_STRING = re.compile(
    r"(?:/(?:private|Users|home|opt|mnt|tmp|var)/|(?:^|[\s(])~[/\\]|(?:file|https?)://|"
    r"(?:api[_-]?key|access[_-]?token|authorization|client[_-]?secret|prompt|token|secret|password)\s*[:=]|"
    r"[?&](?:sig|signature|x-amz-signature)=|Bearer\s)",
    re.IGNORECASE,
)


def _redact_shared(value: Any, *, key: str = "") -> Any:
    """Replace private transport material with inert, non-executable markers."""
    if _SENSITIVE_KEYS.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(name): _redact_shared(item, key=str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact_shared(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [_redact_shared(item, key=key) for item in value]
    if isinstance(value, str) and _PRIVATE_STRING.search(value):
        return "<redacted>"
    return value


def redact_for_shared(value: Any) -> Any:
    """Return a JSON-safe shared projection without private local material."""
    return _redact_shared(value)


_SECRET_TEXT = re.compile(r"(?i)(['\"]?(?:[A-Z0-9]{1,32}_){0,8}(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|credential|password|secret|token|signature|sig|owner_id)['\"]?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}]+)")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_PRIVATE_KEY_BEGIN = re.compile(r"-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----")
_PRIVATE_KEY_END = re.compile(r"^-----END [A-Z0-9 ]*PRIVATE KEY-----", re.MULTILINE)
# Bounded and left-anchored: a scheme start is tried only at the first
# character of a run, and a failed attempt costs O(1) (review W177).
_URL_CREDENTIALS = re.compile(r"(?<![A-Za-z0-9+.-])([A-Za-z][A-Za-z0-9+.-]{0,31}://)[^/?#@\s<>\"']{1,4096}@")
_SECRET_KEY = re.compile(r"(?i)(?:credential|token|secret|password|api[_-]?key|authorization|owner_id)")
_USER_PATH = re.compile(r"(?i)(?:[a-z]:[\\/](?:users|documents and settings)[\\/]|/(?:users|home)/)[^\s\"'<>;,)]*")
_CHAT_CONTENT_KEY = re.compile(r"(?i)^(?:chat|chats|conversation|conversations|transcript|transcripts|messages|prompt|prompts|completion|completions|content)$")


def _redact_identity(text: str) -> str:
    home = str(Path.home())
    for spelling in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
        if spelling not in {"/", "\\", "."}:
            text = re.sub(re.escape(spelling), "<redacted:home>", text, flags=re.IGNORECASE)
    text = _USER_PATH.sub("<redacted:path>", text)
    try:
        users = {getpass.getuser(), Path.home().name}
    except OSError:
        users = {Path.home().name}
    for user in users - {""}:
        text = re.sub(r"(?<![\w-])" + re.escape(user) + r"(?![\w-])", "<redacted:user>", text, flags=re.IGNORECASE)
    return text


def _redact_identity_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {name: _redact_identity_value(item) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact_identity_value(item) for item in value]
    return _redact_identity(value) if isinstance(value, str) else value


def _redact_log_value(value: Any, key: str = "") -> Any:
    if _SECRET_KEY.search(key) or _CHAT_CONTENT_KEY.fullmatch(key):
        return "<redacted>"
    if isinstance(value, dict):
        return {str(name): _redact_log_value(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact_log_value(item, key) for item in value]
    if isinstance(value, str):
        value = _redact_private_key_blocks(value)
        value = _URL_CREDENTIALS.sub(r"\1<redacted>@", value)
        value = _BEARER.sub("Bearer <redacted>", value)
    value = redact_for_shared(value)
    return _redact_identity(value) if isinstance(value, str) else value


def _redact_private_key_blocks(text: str) -> str:
    """Replace only BEGIN through its matching END, or the remaining field.

    Searching literal delimiters also handles JSON's escaped newlines. An
    orphan END at a line start still protects log tails cut inside a key.
    """
    def redact_tail_prefix(fragment: str) -> str:
        last_end = None
        for end in _PRIVATE_KEY_END.finditer(fragment):
            last_end = end
        if last_end is not None:
            return "<redacted truncated private key>\n" + fragment[last_end.end():].lstrip("\r\n")
        return fragment

    output: list[str] = []
    cursor = 0
    while (begin := _PRIVATE_KEY_BEGIN.search(text, cursor)) is not None:
        output.append(redact_tail_prefix(text[cursor:begin.start()]))
        output.append("<redacted PEM>")
        delimiter = f"-----END {begin[1]}-----"
        end = text.find(delimiter, begin.end())
        cursor = len(text) if end < 0 else end + len(delimiter)
    output.append(redact_tail_prefix(text[cursor:]))
    return "".join(output)


def redact_log(text: str) -> str:
    """Redact structured and text credentials before logs enter a support archive."""
    text = _URL_CREDENTIALS.sub(r"\1<redacted>@", text)
    text = _BEARER.sub("Bearer <redacted>", text)
    # Decode first so an unterminated key cannot consume other JSON fields.
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        value = None
    if value is not None:
        return json.dumps(_redact_log_value(value), sort_keys=True) + "\n"
    text = _redact_private_key_blocks(text)
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            value = None
        if value is not None:
            lines.append(json.dumps(_redact_log_value(value), sort_keys=True) + ("\n" if line.endswith("\n") else ""))
        else:
            lines.append(_SECRET_TEXT.sub(r"\1<redacted>", line))
    return _redact_identity("".join(lines))


def redact_for_display(value: Any, key: str = "") -> Any:
    """Copy a receipt, redacting only free-text strings, never its metadata.

    In particular, sensitive-looking keys do not change values' types, and
    retained log paths and identities remain usable by receipt consumers.
    """
    if isinstance(value, dict):
        return {name: redact_for_display(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [redact_for_display(item, key) for item in value]
    if isinstance(value, str) and key in {
        "message", "issue_message", "cause", "tail", "excerpt",
        "log_tail", "log_excerpt", "stdout_tail", "stderr_tail",
    }:
        return redact_log(value)
    return value
