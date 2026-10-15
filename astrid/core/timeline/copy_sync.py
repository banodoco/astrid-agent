"""Pure exact-token copy matching and conservative source correspondence.

No reads, component execution, authoring, or fuzzy/semantic matching live here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from types import SimpleNamespace

from astrid.core.timeline import moments

NORMALIZATION = "copy-token-v1"
_EDGE = '.,;:!?…"\'“”‘’()[]{}'


def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def tokens(text):
    """Normalized tokens with exact original Unicode-code-point spans and raw text."""
    result = []
    for match in re.finditer(r"\S+", text):
        raw = match.group()
        # Trim on the original string, before casefold can expand a code point.
        start, end = 0, len(raw)
        while start < end and raw[start] in _EDGE:
            start += 1
        while end > start and raw[end - 1] in _EDGE:
            end -= 1
        value = unicodedata.normalize("NFC", unicodedata.normalize("NFC", raw[start:end]).casefold())
        value = value.replace("‘", "'").replace("’", "'")
        if value:
            result.append({"token": value, "text": raw[start:end],
                           "range": [match.start() + start, match.start() + end]})
    return result


def normalized(text):
    return tuple(t["token"] for t in tokens(text))


def runs(text, line):
    """All whole-word candidates, using the existing moment consecutive matcher."""
    words = [SimpleNamespace(**w, segment=line["segment"]) for w in line["words"]]
    return [[{"id": w.id, "text": w.text} for w in hit]
            for hit in moments.find_words(moments.Moment("word", text=text), words, tokenizer=normalized)]


def overlap(a, b):
    return bool(a and b and a[0] < a[1] and b[0] < b[1] and a[0] < b[1] and b[0] < a[1])


def selected_display(display):
    text = display["text"]
    return text[slice(*display["range"])] if "range" in display else text


def validate_declarations(value):
    """Validate the owned schema and range/whole-word/duplicate invariants."""
    from pathlib import Path

    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[3] /
                         "schemas/astrid-intent/astrid-intent.schema.json").read_text())
    Draft202012Validator({"$ref": "#/$defs/copyQuotes", "$defs": schema["$defs"]}).validate(value)
    seen = set()
    for quote in value:
        display, source = quote["display"], quote["source"]
        text = display["text"]
        if "range" in display:
            start, end = display["range"]
            if not 0 <= start < end <= len(text):
                raise ValueError("display.range must be a nonempty half-open code-point interval")
            if any(t["range"][0] < start < t["range"][1] or t["range"][0] < end < t["range"][1]
                   for t in tokens(text)):
                raise ValueError("display.range must select complete words")
        if not normalized(selected_display(display)):
            raise ValueError("a quote must contain words")
        if normalized(selected_display(display)) != normalized(" ".join(w["text"] for w in source["words"])):
            raise ValueError("display and captured source words must match exactly under copy-token-v1")
        if len({w["id"] for w in source["words"]}) != len(source["words"]):
            raise ValueError("duplicate source word identities")
        key = (display["path"], tuple(display.get("range", [0, len(text)])))
        if key in seen:
            raise ValueError("duplicate quote association")
        seen.add(key)
    return copy.deepcopy(value)


def line_key(source):
    return tuple(source.get(k) for k in ("occurrence_id", "binding_id", "clip", "segment"))


def correspondence(before, after, selected):
    """Map a run only if every optimal exact-word alignment preserves it uniquely.

    Prefix/suffix LCS tables enumerate possible equal edges and deletions, instead
    of trusting one arbitrary alignment. Changed lines are compared independently.
    """
    a = [normalized(w["text"]) for w in before]
    b = [normalized(w["text"]) for w in after]
    ids = [w["id"] for w in before]
    try:
        indexes = [ids.index(w["id"]) for w in selected]
    except ValueError:
        return None, "captured words unavailable in before source"
    if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
        return None, "broken source contiguity"
    if any(before[i]["text"] != w["text"] for i, w in zip(indexes, selected)):
        return None, "captured source changed without correspondence evidence"
    if a == b:  # same normalized full line: positions are demonstrably unchanged
        return [copy.deepcopy(after[i]) for i in indexes], None
    n, m = len(a), len(b)
    prefix = [[0] * (m + 1) for _ in range(n + 1)]
    suffix = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n):
        for j in range(m):
            prefix[i + 1][j + 1] = prefix[i][j] + 1 if a[i] == b[j] else max(prefix[i][j + 1], prefix[i + 1][j])
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            suffix[i][j] = suffix[i + 1][j + 1] + 1 if a[i] == b[j] else max(suffix[i + 1][j], suffix[i][j + 1])
    length = prefix[n][m]
    mapped = []
    for i in indexes:
        choices = [j for j in range(m) if a[i] == b[j] and prefix[i][j] + 1 + suffix[i + 1][j + 1] == length]
        deleted = any(prefix[i][j] + suffix[i + 1][j] == length for j in range(m + 1))
        if not choices:
            return None, "source words replaced or deleted"
        if len(choices) != 1 or deleted:
            return None, "ambiguous source correspondence; explicit rebinding required"
        mapped.append(choices[0])
    if mapped != list(range(mapped[0], mapped[0] + len(mapped))):
        return None, "broken source contiguity"
    return [copy.deepcopy(after[i]) for i in mapped], None
