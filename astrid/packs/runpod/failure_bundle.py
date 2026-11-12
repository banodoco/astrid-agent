"""The pod-side failure bundle and its redactor (``astrid.failure-redaction.v1``).

Shipped to the pod as standard-library-only source and run with one ``exec``,
as ``failure_diagnostics.py`` is. It writes one redacted, size-capped folder,
``/root/astrid/failures/<name>/``, with a digest manifest the controller
fetches through the same verified fetch as a job's outputs. The newest
``KEEP_POD_FOLDERS`` folders are kept on the pod. The controller runs the same
``Redactor`` again on every fetched file before it lands beside the local
failure receipt; nothing here is uploaded anywhere.

The rule (owner decision H10):

* redact before truncating: every source window is redacted whole, then cut
  at line boundaries (a partial first line of a log window is dropped unread);
* prompt text: every string of ``PROMPT_MIN`` (4) characters or more in the
  job's ``bindings`` and ``inputs`` is replaced wherever it appears (also its
  JSON-escaped form and each of its lines of ``PROMPT_LINE_MIN`` (4) or more
  characters, ignoring case); a line holding ``LINE_DROP`` (32) or more
  matching characters is dropped whole;
* keys and tokens, URLs and IP addresses are always removed (``RULES``);
* ``TOTAL_CAP`` (256 KiB) for the whole folder, with the per-file ``FILE_CAPS``;
  the engine log tail gives way first;
* the submitted workflow comes back as structure only: no values.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import sys
import time
import unicodedata

POLICY = "astrid.failure-redaction.v1"
SCHEMA = "astrid.failure-bundle.v1"
COMMAND_MARK = "astrid-failure-bundle"
MANIFEST_NAME = "wrapper-manifest.json"
MANIFEST_MARK = "WRAPPER_MANIFEST_SHA256="
KiB = 1024
TOTAL_CAP = 256 * KiB
#: Per-file caps (bytes); the folder total, manifest included, never exceeds TOTAL_CAP.
FILE_CAPS = {
    "error-tail.txt": 32 * KiB,
    "engine-log-tail.txt": 128 * KiB,
    "node-error.json": 32 * KiB,
    "workflow.redacted.json": 48 * KiB,
    "install-receipt.json": 8 * KiB,
    "collector.json": 8 * KiB,
}
BUNDLE_FILES = tuple(FILE_CAPS)
MANIFEST_CAP = 8 * KiB
LINE_CAP = 2 * KiB
MAX_REDACT_LINE = 8 * KiB
#: How much of a log's end is read (and redacted whole) before the tail is cut to its cap.
SOURCE_WINDOW = 4 * 1024 * KiB
MAX_JSON_SOURCE = 4 * 1024 * KiB
PROMPT_MIN = 4
PROMPT_LINE_MIN = 4
LINE_DROP = 32
SECRET_MIN = 1
KEEP_POD_FOLDERS = 5
PHASES = frozenset({"claim", "preparation", "start", "poll", "fetch", "execution", "reset", "cancel"})
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}")
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL|COOKIE", re.IGNORECASE)
DROPPED = "<line removed: prompt text>"
COUNTS = ("secret", "url", "email", "ip", "prompt", "lines_dropped")

_H = r"[0-9A-Fa-f]{1,4}"
#: (pattern, replacement, counter), applied in this order to every line after exact values.
RULES = tuple((re.compile(pattern), replacement, kind) for pattern, replacement, kind in (
    # URLs first, so credentials and query tokens inside them go with the whole URL.
    (r"\b[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>`]*", "<url>", "url"),
    (r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+:[\w./~-]+", "<url>", "url"),  # scp-style git@host:path
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b", "<email>", "email"),
    (r"\bwww\.[^\s\"'<>`]+", "<url>", "url"),
    # A bare host name; not an attribute or call in code (``self.net(``, ``torchvision.io.read``).
    (r"\b(?:[A-Za-z0-9-]+\.)+(?:com|net|org|io|ai|dev|app|cloud|co|xyz|gov|edu)\b(?!\.\w|[(\w-])"
     r"(?::\d{1,5})?(?:/[^\s\"'<>`]*)?", "<url>", "url"),
    # Keys and tokens.
    (r"(?i)\b((?:proxy-)?authorization|set-cookie|cookie|x-api-key|api-key|x-auth-token|x-amz-security-token)"
     r"(\s*[\"']?\s*[:=]\s*[\"']?)(?!\s*<secret>)[^\"'\r\n]+", r"\1\2<secret>", "secret"),
    (r"(?i)\bbearer\s+(?!<)[A-Za-z0-9._~+/=-]+", "Bearer <secret>", "secret"),
    (r"(?i)\bbasic\s+[A-Za-z0-9+/]{8,}={0,2}", "Basic <secret>", "secret"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*|.*-----END [A-Z ]*PRIVATE KEY-----", "<secret>", "secret"),
    (r"\bhf_[A-Za-z0-9]{8,}", "<secret>", "secret"),
    (r"\bsk-[A-Za-z0-9_-]{8,}", "<secret>", "secret"),
    (r"\brpa_[A-Za-z0-9]{8,}", "<secret>", "secret"),
    (r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b", "<secret>", "secret"),
    (r"\bgh[pousr]_[A-Za-z0-9]{16,}|\bgithub_pat_[A-Za-z0-9_]{16,}", "<secret>", "secret"),
    (r"\bxox[abposr]-[A-Za-z0-9-]{8,}", "<secret>", "secret"),
    (r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*", "<secret>", "secret"),
    (r"(?i)\b([A-Za-z0-9_.-]*(?:api[_-]?key|access[_-]?key|secret|token|password|passwd|pwd|auth|credentials?"
     r"|cookie|session[_-]?id|private[_-]?key))(\s*[\"']?\s*[:=]\s*[\"']?)(?!<)[^\s\"',;&]+", r"\1\2<secret>", "secret"),
    (r"^\s*[A-Za-z0-9+/]{40,}={0,2}\s*$", "<secret>", "secret"),  # a bare base64 line (key material)
    # IP addresses (v4, then full and compressed v6).
    (r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)", "<ip>", "ip"),
    (rf"(?<![\w:])(?:{_H}:){{7}}{_H}(?![\w:])", "<ip>", "ip"),
    (rf"(?<![\w:])(?:{_H}(?::{_H})*::(?:{_H}(?::{_H})*)?|::{_H}(?::{_H})*)(?:%[A-Za-z0-9]+)?(?![\w:])", "<ip>", "ip"),
))
#: Any letter-and-digit run of 32 or more characters (``_``, ``+``, ``-`` included), except ``sha256:<hex>``.
GENERIC = re.compile(r"(?<![A-Za-z0-9_+-])[A-Za-z0-9_+-]{32,}={0,2}(?![A-Za-z0-9_+=-])")
_DIGEST = re.compile(r"[0-9a-f]{64}")


# ----------------------------------------------------------------------------
# The redactor (same code on the pod and on the controller)
# ----------------------------------------------------------------------------


def _strings(value, out, depth=0):
    if depth > 64 or len(out) >= 20000:
        raise ValueError("redaction context over cap")
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _strings(item, out, depth + 1)


def job_prompts(job):
    """Every string in the job's ``bindings`` and ``inputs`` (the prompt text and the user's values)."""
    found = []
    if isinstance(job, dict):
        _strings(job.get("bindings"), found)
        _strings(job.get("inputs"), found)
    return found


def _variants(value, minimum, line_minimum):
    out = set()
    if not isinstance(value, str):
        return out
    for candidate in (value, value.strip(), json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]):
        if len(candidate) >= minimum:
            out.add(candidate)
    for line in re.split(r"\r\n|\r|\n|\\n|\\r", value):
        line = line.strip()
        if len(line) >= line_minimum:
            out.add(line)
    return out


def _alternations(values):
    ordered = sorted(values, key=lambda v: (-len(v), v))
    return [re.compile("|".join(re.escape(v) for v in ordered[i:i + 400]), re.IGNORECASE)
            for i in range(0, len(ordered), 400)]


class Redactor:
    """Line redaction with counts. ``prompts`` and ``secrets`` are exact strings to remove."""

    def __init__(self, prompts=(), secrets=(), check=None):
        self._check = check or (lambda: None)
        self.counts = dict.fromkeys(COUNTS, 0)
        # Bound iterators too: context construction is part of the deadline.
        def bounded(values):
            result = []
            for value in values:
                self._check()
                if len(result) >= 20000:
                    raise ValueError("redaction context over cap")
                result.append(value)
            return result
        prompts, secrets = bounded(prompts), bounded(secrets)
        # Refuse incomplete or excessive context; callers must withhold text.
        if len(prompts) + len(secrets) > 20000:
            raise ValueError("redaction context over cap")
        prompt_values, secret_values = set(), set()
        size = 0
        for values, target, minimum in ((prompts, prompt_values, PROMPT_MIN),
                                        (secrets, secret_values, SECRET_MIN)):
            for value in values:
                self._check()
                if not isinstance(value, str):
                    raise ValueError("invalid redaction context")
                size += len(value.encode("utf-8"))
                if size > 256 * KiB:
                    raise ValueError("redaction context over cap")
                target.update(_variants(unicodedata.normalize("NFC", value), minimum, minimum))
        self.prompt_strings = sum(len(value) >= PROMPT_MIN for value in prompts)
        self.secret_values = len(secret_values)
        self._secrets = tuple(sorted({value.lower() for value in secret_values}, key=lambda value: (-len(value), value)))
        self._fragments, self._windows = set(), set()
        for value in prompt_values:
            self._check()
            low = value.lower()
            self._fragments.update(low[i:i + PROMPT_MIN] for i in range(len(low) - PROMPT_MIN + 1))
            self._windows.update(low[i:i + LINE_DROP] for i in range(len(low) - LINE_DROP + 1))

    def _lower_source(self, text):
        low = text.lower()
        origins = None
        if len(low) != len(text):
            # Unicode lowercasing may expand a code point (for example İ).
            # Keep source offsets so a preceding expansion cannot shift a
            # replacement past the actual private text.
            origins = []
            for source_index, character in enumerate(text):
                if source_index % 1024 == 0:
                    self._check()
                origins.extend([source_index] * len(character.lower()))
        return low, origins

    def _prompt_spans(self, text):
        # Match against the original text, then merge overlapping spans before
        # replacing; sequential substitutions can expose overlapping fragments.
        low, origins = self._lower_source(text)
        spans = []
        for i in range(len(low) - PROMPT_MIN + 1):
            if i % 1024 == 0:
                self._check()
            if low[i:i + PROMPT_MIN] in self._fragments:
                start = origins[i] if origins is not None else i
                end = origins[i + PROMPT_MIN - 1] + 1 if origins is not None else i + PROMPT_MIN
                if spans and start <= spans[-1][1]:
                    spans[-1][1] = max(spans[-1][1], end)
                else:
                    spans.append([start, end])
        return spans

    def _plain_line(self, line, encoded_edits=()):
        if self.holds_prompt(line):
            self.counts["lines_dropped"] += 1
            return None
        # All policies inspect the original line. Inserting a marker before
        # another rule runs can split a URL, credential assignment, or secret.
        edits = list(encoded_edits)

        def add(start, end, replacement, kind):
            edits.append([start, end, replacement, kind])
            self.counts[kind] += 1
            if len(edits) > 65536:
                raise ValueError("redaction match count over cap")

        low, origins = self._lower_source(line)
        for secret in self._secrets:
            self._check()
            offset = low.find(secret)
            while offset >= 0:
                self._check()
                end = offset + len(secret)
                start_source = origins[offset] if origins is not None else offset
                end_source = origins[end - 1] + 1 if origins is not None else end
                add(start_source, end_source, "<secret>", "secret")
                offset = low.find(secret, offset + 1)
        for start, end in self._prompt_spans(line):
            add(start, end, "<prompt>", "prompt")
        for pattern, replacement, kind in RULES:
            self._check()
            for match in pattern.finditer(line):
                add(match.start(), match.end(), match.expand(replacement), kind)
        # ipaddress validates unspecified and IPv4-mapped IPv6 too. Match the
        # complete original address, before the IPv4 rule can split its tail.
        addresses = r"(?<![\w:])[0-9A-Fa-f:.]{1,128}(?:%[A-Za-z0-9_.-]{1,64})?(?![\w:])"
        for match in re.finditer(addresses, line):
            if ":" not in match.group(0):
                continue
            try:
                ipaddress.ip_address(match.group(0).split("%", 1)[0])
            except ValueError:
                continue
            add(match.start(), match.end(), "<ip>", "ip")
        for match in GENERIC.finditer(line):
            token = match.group(0)
            if _DIGEST.fullmatch(token) and line[max(0, match.start() - 7):match.start()].lower() == "sha256:":
                continue
            if re.search(r"[0-9]", token) and re.search(r"[A-Za-z]", token):
                add(match.start(), match.end(), "<secret>", "secret")
        merged = []
        priority = {"url": 0, "secret": 1, "ip": 2, "email": 3, "prompt": 4}
        for start, end, replacement, kind in sorted(edits):
            if merged and start < merged[-1][1]:
                previous = merged[-1]
                previous[1] = max(previous[1], end)
                selected = min((previous[3], kind), key=priority.__getitem__)
                previous[2], previous[3] = "<" + selected + ">", selected
            else:
                merged.append([start, end, replacement, kind])
        for start, end, replacement, _kind in reversed(merged):
            line = line[:start] + replacement + line[end:]
        return line

    def holds_prompt(self, line):
        """True when ``line`` holds ``LINE_DROP`` or more prompt-matching characters."""
        if sum(end - start for start, end in self._prompt_spans(line)) >= LINE_DROP:
            return True
        if not self._windows or len(line) < LINE_DROP:
            return False
        low, windows = line.lower(), self._windows
        for i in range(len(low) - LINE_DROP + 1):
            if i % 1024 == 0:
                self._check()
            if low[i:i + LINE_DROP] in windows:
                return True
        return False

    def line(self, line, _depth=0):
        """Canonicalize bounded encodings before removing their original spans."""
        self._check()
        # Withhold excessive lines whole, never shorten unredacted text. This
        # bounds regex near misses while retaining the ordinary capped tail.
        if len(line) > MAX_REDACT_LINE:
            return "<line withheld: processing cap>"
        # JSON escaped slashes must be recognized as a whole URL before email
        # or token substitutions split credentials out of the address.
        line = line.replace("\\/", "/")
        line = re.sub(r"(?:\\u[0-9a-fA-F]{4})+",
                      lambda match: json.loads('"' + match.group(0) + '"'), line)
        line = unicodedata.normalize("NFC", line)
        encoded_edits = []
        for match in re.finditer(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{4,}={0,2}(?![A-Za-z0-9+/=])", line):
            self._check()
            token = match.group(0)
            replacement = None
            kind = "secret"
            if len(token) > 16384:
                replacement = "<encoded withheld>"
            else:
                try:
                    decoded = base64.b64decode(token, validate=True).decode("utf-8")
                except (ValueError, UnicodeError):
                    continue
                decoded = unicodedata.normalize("NFC", decoded)
                if _depth >= 3:
                    replacement = "<encoded withheld>"
                else:
                    before = self.counts.copy()
                    cleaned = self.line(decoded, _depth + 1)
                    if cleaned is None:
                        return None
                    if cleaned != decoded:
                        replacement = "<encoded redacted>"
                        kind = "secret" if self.counts["secret"] > before["secret"] else "prompt"
            if replacement is not None:
                encoded_edits.append([match.start(), match.end(), replacement, kind])
        return self._plain_line(line, encoded_edits)

    def text(self, text):
        out = []
        for line in text.split("\n"):
            redacted = self.line(line.rstrip("\r"))
            out.append(DROPPED if redacted is None else redacted)
        return "\n".join(out)

    def value(self, value, depth=0):
        """Redact every string (keys included) of a JSON value; numbers and booleans stay."""
        self._check()
        if depth > 64:
            return "<redacted: too deep>"
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            out = {}
            for key, item in value.items():
                name = self.text(str(key))
                while name in out:
                    name += "#"
                out[name] = "<secret>" if SECRET_NAME.search(str(key)) else self.value(item, depth + 1)
                if SECRET_NAME.search(str(key)):
                    self.counts["secret"] += 1
            return out
        if isinstance(value, (list, tuple)):
            return [self.value(item, depth + 1) for item in value]
        return value if value is None or isinstance(value, (bool, int, float)) else self.text(str(value))


# ----------------------------------------------------------------------------
# Caps (applied after redaction)
# ----------------------------------------------------------------------------


def fit_text(text, cap, *, line_cap=LINE_CAP):
    """The tail of ``text`` within ``cap`` bytes, cut at line boundaries; long lines shortened.

    Returns ``(text, truncated, shortened_lines)``.
    """
    out, size, truncated, shortened = [], 0, False, 0
    for line in reversed(text.split("\n")):
        raw = line.encode("utf-8")
        if len(raw) > line_cap:
            line = raw[:line_cap].decode("utf-8", "ignore") + " [shortened]"
            shortened += 1
        n = len(line.encode("utf-8")) + 1
        if size + n > cap:
            truncated = True
            break
        out.append(line)
        size += n
    return "\n".join(reversed(out)), truncated, shortened


def _shorten(value, limit, depth=0):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + " [shortened]"
    if isinstance(value, dict):
        return {key: _shorten(item, limit, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_shorten(item, limit, depth + 1) for item in value]
    return value


def dump(value):
    return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


def fit_json(value, cap):
    """``(body, truncated)``: the JSON within ``cap`` bytes, shortening strings, then lists, then giving up."""
    body = dump(value)
    if len(body) <= cap:
        return body, False
    for limit in (1024, 256, 64):
        body = dump(_shorten(value, limit))
        if len(body) <= cap:
            return body, True
    if isinstance(value, dict):
        trimmed = {key: (item[:8] if isinstance(item, list) else item) for key, item in _shorten(value, 64).items()}
        body = dump(trimmed)
        if len(body) <= cap:
            return body, True
    return dump({"truncated": True, "reason": "over_cap"}), True


def redact_file(name, data, redactor):
    """One bundle file redacted whole, then held to its cap (the controller's second pass)."""
    cap = FILE_CAPS[name]
    text = data.decode("utf-8", "replace")
    if name.endswith(".json"):
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            pass
        else:
            # Rebuild opaque references together: generic key redaction could
            # collapse generated node IDs while leaving edges inconsistent.
            if name == "workflow.redacted.json":
                value, _counts = structure(value, redactor)
            else:
                value = redactor.value(value)
            return fit_json(value, cap)[0].encode("utf-8")
    return fit_text(redactor.text(text), cap)[0].encode("utf-8")


# ----------------------------------------------------------------------------
# The submitted workflow, structure only
# ----------------------------------------------------------------------------


def _marker(value, counts):
    counts["values_replaced"] += 1
    if isinstance(value, str):
        counts["strings_replaced"] += 1
        return {"redacted": "string", "len": len(value)}
    if isinstance(value, bool):
        return {"redacted": "boolean"}
    if isinstance(value, (int, float)):
        return {"redacted": "number"}
    if value is None:
        return {"redacted": "null"}
    return {"redacted": type(value).__name__}


def structure(graph, redactor):
    """Only validated names and opaque node/edge references; no submitted values."""
    counts = {"strings_replaced": 0, "values_replaced": 0}

    def name(value):
        if isinstance(value, str) and NAME_RE.fullmatch(value):
            cleaned = redactor.text(value)
            if cleaned == value:
                return value
        return _marker(value, counts)

    def generic(value, depth=0):
        redactor._check()
        if depth > 32:
            return {"redacted": "deep"}
        if isinstance(value, list):
            return [generic(item, depth + 1) for item in value]
        # Arbitrary dictionary keys are also submitted values.
        if isinstance(value, dict):
            return {"redacted": "dict"}
        return _marker(value, counts)

    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    identifiers = {}

    def node_id(value):
        key = str(value)
        if key not in identifiers:
            identifiers[key] = "node_" + str(len(identifiers) + 1)
        return identifiers[key]

    if isinstance(nodes, list):
        for node in nodes[:2000]:
            if isinstance(node, dict) and "id" in node:
                node_id(node["id"])
        out_nodes = []
        for node in nodes[:2000]:
            redactor._check()
            if not isinstance(node, dict):
                continue
            row = {}
            if "id" in node:
                row["id"] = node_id(node["id"])
            for key in ("type", "class_type"):
                if key in node:
                    row[key] = name(node[key])
            for key in ("inputs", "outputs"):
                slots = node.get(key)
                if isinstance(slots, list):
                    rows = []
                    for slot in slots:
                        if not isinstance(slot, dict):
                            continue
                        shaped = {}
                        for field in ("name", "type", "link", "links", "slot_index"):
                            if field not in slot:
                                continue
                            value = slot[field]
                            if field in ("name", "type"):
                                shaped[field] = name(value)
                            elif field == "links" and isinstance(value, list):
                                shaped[field] = [v if type(v) is int else _marker(v, counts) for v in value]
                            else:
                                shaped[field] = value if value is None or type(value) is int else _marker(value, counts)
                        rows.append(shaped)
                    row[key] = rows
            if "widgets_values" in node:
                row["widgets_values"] = generic(node["widgets_values"])
            out_nodes.append(row)
        out_links = []
        links = graph.get("links")
        if isinstance(links, list):
            for link in links[:10000]:
                if isinstance(link, list) and len(link) == 6:
                    out_links.append([link[0] if type(link[0]) is int else _marker(link[0], counts),
                                      node_id(link[1]), link[2] if type(link[2]) is int else _marker(link[2], counts),
                                      node_id(link[3]), link[4] if type(link[4]) is int else _marker(link[4], counts),
                                      name(link[5])])
        return {"format": "litegraph", "nodes": out_nodes, "links": out_links}, counts
    api = nodes if isinstance(nodes, dict) else graph
    if isinstance(api, dict) and api and all(isinstance(v, dict) and "class_type" in v for v in api.values()):
        for key in list(api)[:2000]:
            node_id(key)
        out = {}
        for key, node in list(api.items())[:2000]:
            redactor._check()
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            row = {"class_type": name(node.get("class_type")), "inputs": {}}
            for field, value in inputs.items():
                safe_field = name(field)
                if not isinstance(safe_field, str):
                    continue
                link = (isinstance(value, list) and len(value) == 2 and str(value[0]) in identifiers
                        and type(value[1]) is int)
                row["inputs"][safe_field] = [node_id(value[0]), value[1]] if link else generic(value)
            out[node_id(key)] = row
        return {"format": "api", "nodes": out}, counts
    return {"format": "unknown", "structure": generic(graph)}, counts


# ----------------------------------------------------------------------------
# Reading the pod's sources (confined, bounded, never following a link)
# ----------------------------------------------------------------------------


def _confined(path, roots):
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or ".." in path.split("/"):
        return None
    real = os.path.realpath(path)
    for root in roots:
        if isinstance(root, str) and root.startswith("/"):
            base = os.path.realpath(root).rstrip("/") + "/"
            if real.startswith(base):
                return path
    return None


def _open_directory(path, *, create=False):
    """Open a directory through descriptors, refusing every symlink component."""
    if isinstance(path, int):
        return os.dup(path)
    path = os.fspath(path)
    if not os.path.isabs(path) or ".." in path.split("/") or "\x00" in path:
        raise ValueError("unsafe directory path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.split("/"):
            if not part or part == ".":
                continue
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_regular(path):
    directory, name = os.path.split(os.fspath(path))
    parent = _open_directory(directory)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    stream = os.fdopen(fd, "rb")
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode):
        stream.close()
        raise OSError("not a regular file")
    return stream, info.st_size


def read_tail(path, window=SOURCE_WINDOW):
    """``(text, source_bytes)`` of the last ``window`` bytes; a partial first line is dropped unread."""
    stream, size = _open_regular(path)
    with stream:
        start = max(0, size - window)
        stream.seek(start)
        data = stream.read(window)
    if start > 0:
        cut = data.find(b"\n")
        data = b"" if cut < 0 else data[cut + 1:]
    return data.decode("utf-8", "replace"), size


def read_json(path, limit=MAX_JSON_SOURCE):
    stream, size = _open_regular(path)
    with stream:
        if size > limit:
            raise ValueError("too large")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError("too large")
        return json.loads(data.decode("utf-8", "replace")), size


def pod_secrets(request, environ=None):
    """Exact secret values the pod holds: credential-named environment values and credential files."""
    environ = os.environ if environ is None else environ
    values = [value for name, value in environ.items() if SECRET_NAME.search(name) and len(value) >= SECRET_MIN]
    paths = [path for path in request.get("secret_files") or [] if isinstance(path, str)]
    for directory in request.get("secret_dirs") or []:
        try:
            directory_fd = _open_directory(directory)
            try:
                names = sorted(os.listdir(directory_fd))
                if len(names) > 64:
                    raise ValueError("too many secret sources")
                paths += [os.path.join(directory, name) for name in names]
            finally:
                os.close(directory_fd)
        except FileNotFoundError:
            pass
        except (OSError, TypeError) as exc:
            raise ValueError("secret directory unavailable") from exc
    prepared = request.get("prepared")
    if isinstance(prepared, str):
        try:
            connection, _ = read_json(prepared, 64 * KiB)
            if isinstance(connection, dict):
                for key, value in connection.items():
                    if SECRET_NAME.search(str(key)) and isinstance(value, str) and value:
                        values.append(value)
            if isinstance(connection, dict) and isinstance(connection.get("credential_file"), str):
                paths.append(connection["credential_file"])
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            raise ValueError("prepared context unavailable") from exc
    for path in paths:
        try:
            stream, size = _open_regular(path)
            with stream:
                if size > 4 * KiB:
                    raise ValueError("secret source too large")
                data = stream.read(4 * KiB + 1)
                if len(data) > 4 * KiB:
                    raise ValueError("secret source too large")
                raw = data.decode("utf-8", "strict")
        except OSError as exc:
            raise ValueError("secret source unavailable") from exc
        found = []
        try:
            parsed = json.loads(raw)
        except ValueError:
            pass
        else:
            _strings(parsed, found)
        for value in [raw, *raw.splitlines(), *found]:
            if len(value.strip()) >= SECRET_MIN:
                values.append(value.strip())
    return values


# ----------------------------------------------------------------------------
# The collector
# ----------------------------------------------------------------------------


def _write(folder, name, body):
    if not isinstance(name, str) or name in {"", ".", ".."} or "/" in name:
        raise ValueError("unsafe bundle filename")
    data = body.encode("utf-8") if isinstance(body, str) else body
    directory = _open_directory(folder)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("not a regular file")
            stream.write(data)
    finally:
        os.close(directory)
    return len(data)


def write_manifest(folder):
    """Write the verified manifest using the same anchored bundle directory."""
    directory = _open_directory(folder)
    try:
        files = {}
        for name in sorted(os.listdir(directory)):
            if name == MANIFEST_NAME:
                continue
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                continue
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise OSError("not a regular file")
                data = stream.read(TOTAL_CAP + 1)
            if len(data) > TOTAL_CAP:
                raise ValueError("bundle exceeds cap")
            files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        body = json.dumps({"schema_version": 1, "files": files}, sort_keys=True, indent=2) + "\n"
        _write(directory, MANIFEST_NAME, body)
        return hashlib.sha256(body.encode()).hexdigest()
    finally:
        os.close(directory)


def _prune(root, keep_name, keep=KEEP_POD_FOLDERS):
    directory = _open_directory(root)
    try:
        rows = []
        for name in os.listdir(directory):
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if NAME.fullmatch(name) and stat.S_ISDIR(info.st_mode):
                rows.append((info.st_mtime, name))
        rows.sort(reverse=True)
        kept = [name for _mtime, name in rows if name == keep_name] + [name for _mtime, name in rows if name != keep_name]
        for name in kept[keep:]:
            shutil.rmtree(name, dir_fd=directory, ignore_errors=True)
    finally:
        os.close(directory)


def collect(request, environ=None):
    """Write one redacted bundle folder; returns ``(folder, manifest sha256, collector facts)``."""
    root = request.get("failures_root")
    name = request.get("name")
    if (not isinstance(root, str) or not root.startswith("/")
            or not isinstance(name, str) or not NAME.fullmatch(name)):
        raise ValueError("invalid bundle name or root")
    directory = _open_directory(root, create=True)
    try:
        return _collect(request, environ, directory)
    finally:
        os.close(directory)


def _collect(request, environ, directory):
    began = time.monotonic()
    name, root = request.get("name"), request.get("failures_root")
    if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(root, str) or not root.startswith("/"):
        raise ValueError("invalid bundle name or root")
    roots = [path for path in request.get("roots") or [] if isinstance(path, str) and path.startswith("/")]
    run_dir = _confined(request.get("run_dir"), roots)
    job_dir = _confined(request.get("job_dir"), roots)
    engine_root = _confined(request.get("engine_root"), roots)
    job = None
    if run_dir is not None:
        try:
            job, _ = read_json(os.path.join(run_dir, "job.json"), 4 * 1024 * KiB)
        except (OSError, ValueError):
            job = None
    context = request.get("redaction_context")
    supplied = isinstance(context, dict)
    context_complete = (context.get("complete") is True if supplied else
                        isinstance(job, dict) and all(key in job for key in ("bindings", "inputs")))
    prompts = context.get("prompts") if supplied else job_prompts(job)
    secrets = context.get("secrets") if supplied else []
    if (not isinstance(prompts, list) or not all(isinstance(value, str) for value in prompts)
            or not isinstance(secrets, list) or not all(isinstance(value, str) for value in secrets)):
        context_complete = False
    try:
        pod_values = pod_secrets(request, environ)
        all_prompts = (prompts or []) + (job_prompts(job) if supplied else [])
        redactor = Redactor(prompts=all_prompts if context_complete else [],
                            secrets=(secrets or []) + pod_values)
    except (OSError, ValueError):
        context_complete = False
        redactor = Redactor()

    for stale in os.listdir(directory):
        try:
            info = os.stat(stale, dir_fd=directory, follow_symlinks=False)
            if (".partial-" in stale and stale.startswith(".") and stat.S_ISDIR(info.st_mode)
                    and time.time() - info.st_mtime > 3600):
                shutil.rmtree(stale, dir_fd=directory, ignore_errors=True)
        except OSError:
            pass
    partial_name = f".{name}.partial-{os.getpid()}"
    shutil.rmtree(partial_name, dir_fd=directory, ignore_errors=True)
    os.mkdir(partial_name, mode=0o700, dir_fd=directory)
    partial = os.open(partial_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        return _collect_files(request, environ, directory, partial, partial_name, began,
                              name, root, roots, run_dir, job_dir, engine_root, job, redactor, context_complete)
    finally:
        os.close(partial)


def _collect_files(request, environ, directory, partial, partial_name, began,
                   name, root, roots, run_dir, job_dir, engine_root, job, redactor, context_complete):
    facts = {}
    sizes = {}

    def text_file(file_name, source, budget):
        if source is None or not context_complete:
            facts[file_name] = {"present": False, "withheld": not context_complete}
            return
        try:
            text, source_bytes = read_tail(source)
        except (OSError, ValueError):
            facts[file_name] = {"present": False}
            return
        body, truncated, shortened = fit_text(redactor.text(text), budget)
        sizes[file_name] = _write(partial, file_name, body)
        facts[file_name] = {"present": True, "source_bytes": source_bytes, "bytes": sizes[file_name],
                            "truncated": truncated or len(text.encode("utf-8")) < source_bytes,
                            "shortened_lines": shortened}

    def json_file(file_name, value, source_bytes):
        body, truncated = fit_json(value, FILE_CAPS[file_name])
        sizes[file_name] = _write(partial, file_name, body)
        facts[file_name] = {"present": True, "source_bytes": source_bytes, "bytes": sizes[file_name],
                            "truncated": truncated}

    text_file("error-tail.txt", os.path.join(job_dir, "stderr") if job_dir else None, FILE_CAPS["error-tail.txt"])
    if run_dir is not None and context_complete:
        try:
            result, size = read_json(os.path.join(run_dir, "result.json"))
            json_file("node-error.json", redactor.value(result), size)
        except (OSError, ValueError, RecursionError):
            facts["node-error.json"] = {"present": False}
    workflow_counts = {"strings_replaced": 0, "values_replaced": 0}
    source = ((job or {}).get("execution") or {}).get("source") if isinstance(job, dict) else None
    if run_dir is not None and isinstance(source, str) and source.startswith(run_dir.rstrip("/") + "/in/"):
        try:
            graph_path = _confined(os.path.join(run_dir, source[len(run_dir.rstrip("/")) + 1:]), [run_dir])
            if graph_path is None:
                raise ValueError("workflow source outside the run folder")
            graph, size = read_json(graph_path)
            shaped, workflow_counts = structure(graph, redactor)
            json_file("workflow.redacted.json", shaped, size)
        except (OSError, ValueError, RecursionError):
            facts["workflow.redacted.json"] = {"present": False}
    if request.get("include_install") and engine_root is not None and context_complete:
        try:
            receipt, size = read_json(os.path.join(engine_root, ".astrid-engine", "install.json"), 1024 * KiB)
            json_file("install-receipt.json", redactor.value(receipt), size)
        except (OSError, ValueError, RecursionError):
            facts["install-receipt.json"] = {"present": False}
    # The engine tail gives way first: it gets what the other files leave of the total.
    budget = min(FILE_CAPS["engine-log-tail.txt"],
                 TOTAL_CAP - sum(sizes.values()) - FILE_CAPS["collector.json"] - MANIFEST_CAP)
    engine_log = None
    if engine_root is not None:
        engine_log = os.path.join(engine_root, "runtime", "out", "sessions", "comfyui", "comfy.log")
    text_file("engine-log-tail.txt", engine_log, max(0, budget))
    exit_code = request.get("exit_code")
    phase = request.get("phase")
    collector = {
        "schema": SCHEMA, "policy": POLICY, "context_complete": context_complete,
        "exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
        "phase": phase if phase in PHASES else None,
        "files": facts, "redaction": dict(redactor.counts), "workflow": workflow_counts,
        "prompt_strings": redactor.prompt_strings, "secret_values": redactor.secret_values,
        "limits": {"prompt_min_chars": PROMPT_MIN, "prompt_line_min_chars": PROMPT_LINE_MIN,
                   "line_drop_chars": LINE_DROP, "secret_min_chars": SECRET_MIN, "total_cap_bytes": TOTAL_CAP,
                   "file_caps": FILE_CAPS, "line_cap_bytes": LINE_CAP, "source_window_bytes": SOURCE_WINDOW},
        "note": f"Prompt strings under {PROMPT_MIN} characters are not matched.",
        "elapsed_s": round(time.monotonic() - began, 3),
    }
    body, _truncated = fit_json(collector, FILE_CAPS["collector.json"])
    _write(partial, "collector.json", body)
    digest = write_manifest(partial)
    folder = os.path.join(root, name)
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise OSError("symlink in bundle path")
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(name, dir_fd=directory)
        else:
            os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        pass
    os.rename(partial_name, name, src_dir_fd=directory, dst_dir_fd=directory)
    _prune(directory, name)
    return folder, digest, collector


def command(request, python="/opt/astrid/bin/python"):
    """The one pod command: this file as source, the request as one base64 argument."""
    import shlex

    source = base64.b64encode(open(__file__, "rb").read()).decode()
    blob = base64.b64encode(json.dumps(request, sort_keys=True).encode()).decode()
    return (f": {COMMAND_MARK}; P={shlex.quote(python)}; [ -x \"$P\" ] || P=python3; "
            f"printf %s {source} | base64 -d | \"$P\" - {blob}")


def main(argv):
    try:
        request = json.loads(base64.b64decode(argv[1]).decode("utf-8"))
        _folder, digest, _facts = collect(request)
    except Exception as exc:  # noqa: BLE001 (only the class name ever leaves the pod)
        print(f"{COMMAND_MARK}: failed {type(exc).__name__}")
        return 1
    print(MANIFEST_MARK + digest)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
