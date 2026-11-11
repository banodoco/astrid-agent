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
  JSON-escaped form and each of its lines of ``PROMPT_LINE_MIN`` (8) or more
  characters, ignoring case); a line holding ``LINE_DROP`` (32) or more
  consecutive characters of one of them is dropped whole;
* keys and tokens, URLs and IP addresses are always removed (``RULES``);
* ``TOTAL_CAP`` (256 KiB) for the whole folder, with the per-file ``FILE_CAPS``;
  the engine log tail gives way first;
* the submitted workflow comes back as structure only: no values.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time

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
#: How much of a log's end is read (and redacted whole) before the tail is cut to its cap.
SOURCE_WINDOW = 4 * 1024 * KiB
MAX_JSON_SOURCE = 4 * 1024 * KiB
PROMPT_MIN = 4
PROMPT_LINE_MIN = 8
LINE_DROP = 32
SECRET_MIN = 8
KEEP_POD_FOLDERS = 5
PHASES = frozenset({"claim", "preparation", "start", "poll", "fetch", "execution", "reset", "cancel"})
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}")
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH|CREDENTIAL|COOKIE", re.IGNORECASE)
DROPPED = "<line removed: prompt text>"
COUNTS = ("secret", "url", "email", "ip", "prompt", "lines_dropped")

_H = r"[0-9A-Fa-f]{1,4}"
#: (pattern, replacement, counter), applied in this order to every line after exact values.
RULES = tuple((re.compile(pattern), replacement, kind) for pattern, replacement, kind in (
    # URLs first, so credentials and query tokens inside them go with the whole URL.
    (r"\b[A-Za-z][A-Za-z0-9+.-]{1,30}://[^\s\"'<>`]*", "<url>", "url"),
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
    if depth > 64 or len(out) > 20000:
        return
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

    def __init__(self, prompts=(), secrets=()):
        self.counts = dict.fromkeys(COUNTS, 0)
        prompt_values, secret_values = set(), set()
        for value in prompts:
            prompt_values |= _variants(value, PROMPT_MIN, PROMPT_LINE_MIN)
        for value in secrets:
            secret_values |= _variants(value, SECRET_MIN, SECRET_MIN)
        self.prompt_strings = sum(1 for value in prompts if isinstance(value, str) and len(value) >= PROMPT_MIN)
        self.secret_values = len(secret_values)
        self._prompts = _alternations(prompt_values)
        self._secrets = _alternations(secret_values)
        windows = set()
        for value in prompt_values:
            low = value.lower()
            for start in range(0, len(low) - LINE_DROP + 1):
                windows.add(low[start:start + LINE_DROP])
                if len(windows) > 4_000_000:
                    break
        self._windows = windows

    def _exact(self, patterns, text, replacement, kind):
        for pattern in patterns:
            text, n = pattern.subn(replacement, text)
            self.counts[kind] += n
        return text

    def _generic(self, text):
        def replace(match):
            token = match.group(0)
            start = match.start()
            if (_DIGEST.fullmatch(token) and text[max(0, start - 7):start].lower() == "sha256:"):
                return token
            if not (re.search(r"[0-9]", token) and re.search(r"[A-Za-z]", token)):
                return token
            self.counts["secret"] += 1
            return "<secret>"
        return GENERIC.sub(replace, text)

    def holds_prompt(self, line):
        """True when ``line`` holds ``LINE_DROP`` or more consecutive characters of a prompt string."""
        if not self._windows or len(line) < LINE_DROP:
            return False
        low, windows = line.lower(), self._windows
        return any(low[i:i + LINE_DROP] in windows for i in range(len(low) - LINE_DROP + 1))

    def line(self, line):
        """One redacted line, or ``None`` when it is dropped whole."""
        if self.holds_prompt(line):
            self.counts["lines_dropped"] += 1
            return None
        line = self._exact(self._secrets, line, "<secret>", "secret")
        line = self._exact(self._prompts, line, "<prompt>", "prompt")
        for pattern, replacement, kind in RULES:
            line, n = pattern.subn(replacement, line)
            self.counts[kind] += n
        return self._generic(line)

    def text(self, text):
        out = []
        for line in text.split("\n"):
            redacted = self.line(line.rstrip("\r"))
            out.append(DROPPED if redacted is None else redacted)
        return "\n".join(out)

    def value(self, value, depth=0):
        """Redact every string (keys included) of a JSON value; numbers and booleans stay."""
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
                out[name] = self.value(item, depth + 1)
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
            return fit_json(redactor.value(value), cap)[0].encode("utf-8")
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
    """The workflow's node ids, class types, input names and link edges; every value becomes a marker."""
    counts = {"strings_replaced": 0, "values_replaced": 0}

    def name(value):
        if not isinstance(value, str):
            return _marker(value, counts)
        return redactor.text(value)[:128]

    def node_id(value):
        if isinstance(value, bool):
            return _marker(value, counts)
        if isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"[0-9]{1,12}", value)):
            return value
        return name(value)

    def generic(value, depth=0):
        if depth > 32:
            return {"redacted": "deep"}
        if isinstance(value, dict):
            return {name(key) if isinstance(key, str) else str(key): generic(item, depth + 1)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [generic(item, depth + 1) for item in value]
        return _marker(value, counts)

    def ints(value):
        if isinstance(value, list):
            return [item if isinstance(item, int) and not isinstance(item, bool) else _marker(item, counts)
                    for item in value]
        return None if value is None else _marker(value, counts)

    def slot_field(field, value):
        if field in ("name", "type"):
            return name(value)
        if field == "links":
            return ints(value)
        if value is None or (isinstance(value, int) and not isinstance(value, bool)):
            return value  # a link id, slot index, or no link: structure
        return _marker(value, counts)

    nodes = graph.get("nodes") if isinstance(graph, dict) else None
    if isinstance(nodes, list):
        out_nodes = []
        for node in nodes[:2000]:
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
                    row[key] = [{field: slot_field(field, slot[field])
                                 for field in ("name", "type", "link", "links", "slot_index") if field in slot}
                                for slot in slots if isinstance(slot, dict)]
            if "widgets_values" in node:
                row["widgets_values"] = generic(node["widgets_values"])
            out_nodes.append(row)
        links = graph.get("links")
        out_links = []
        if isinstance(links, list):
            for link in links[:10000]:
                if isinstance(link, list):
                    out_links.append([item if isinstance(item, int) and not isinstance(item, bool) else
                                      name(item) if isinstance(item, str) else _marker(item, counts)
                                      for item in link])
        return {"format": "litegraph", "nodes": out_nodes, "links": out_links}, counts
    api = nodes if isinstance(nodes, dict) else graph
    if isinstance(api, dict) and api and all(isinstance(v, dict) and "class_type" in v for v in api.values()):
        ids = set(api)
        out = {}
        for key, node in list(api.items())[:2000]:
            inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
            row = {"class_type": name(node.get("class_type")), "inputs": {}}
            for field, value in inputs.items():
                link = (isinstance(value, list) and len(value) == 2 and str(value[0]) in ids
                        and isinstance(value[1], int) and not isinstance(value[1], bool))
                row["inputs"][name(field)] = [str(value[0]), value[1]] if link else generic(value)
            out[str(node_id(key))] = row
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


def _open_regular(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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
        return json.loads(stream.read(limit + 1).decode("utf-8", "replace")), size


def pod_secrets(request, environ=None):
    """Exact secret values the pod holds: credential-named environment values and credential files."""
    environ = os.environ if environ is None else environ
    values = [value for name, value in environ.items() if SECRET_NAME.search(name) and len(value) >= SECRET_MIN]
    paths = [path for path in request.get("secret_files") or [] if isinstance(path, str)]
    for directory in request.get("secret_dirs") or []:
        try:
            paths += [os.path.join(directory, name) for name in sorted(os.listdir(directory))[:64]]
        except (OSError, TypeError):
            pass
    prepared = request.get("prepared")
    if isinstance(prepared, str):
        try:
            connection, _ = read_json(prepared, 64 * KiB)
            if isinstance(connection, dict) and isinstance(connection.get("credential_file"), str):
                paths.append(connection["credential_file"])
        except (OSError, ValueError):
            pass
    for path in paths:
        try:
            stream, size = _open_regular(path)
            with stream:
                raw = stream.read(4 * KiB).decode("utf-8", "replace") if size <= 4 * KiB else ""
        except OSError:
            continue
        found = []
        try:
            _strings(json.loads(raw), found)
        except ValueError:
            pass
        for value in [raw, *raw.splitlines(), *found]:
            if len(value.strip()) >= SECRET_MIN:
                values.append(value.strip())
    return values


# ----------------------------------------------------------------------------
# The collector
# ----------------------------------------------------------------------------


def _write(folder, name, body):
    data = body.encode("utf-8") if isinstance(body, str) else body
    with open(os.path.join(folder, name), "wb") as stream:
        stream.write(data)
    return len(data)


def write_manifest(folder):
    """The same ``wrapper-manifest.json`` the output fetch verifies; returns its sha256."""
    files = {}
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if name == MANIFEST_NAME or os.path.islink(path) or not os.path.isfile(path):
            continue
        with open(path, "rb") as stream:
            data = stream.read()
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    body = json.dumps({"schema_version": 1, "files": files}, sort_keys=True, indent=2) + "\n"
    with open(os.path.join(folder, MANIFEST_NAME), "w", encoding="utf-8") as stream:
        stream.write(body)
    return hashlib.sha256(body.encode()).hexdigest()


def _prune(root, keep_name, keep=KEEP_POD_FOLDERS):
    rows = []
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if NAME.fullmatch(name) and not os.path.islink(path) and os.path.isdir(path):
            rows.append((os.stat(path).st_mtime, name))
    rows.sort(reverse=True)
    kept = [name for _mtime, name in rows if name == keep_name] + [name for _mtime, name in rows if name != keep_name]
    for name in kept[keep:]:
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def collect(request, environ=None):
    """Write one redacted bundle folder; returns ``(folder, manifest sha256, collector facts)``."""
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
    redactor = Redactor(prompts=job_prompts(job), secrets=pod_secrets(request, environ))
    os.makedirs(root, mode=0o700, exist_ok=True)
    for stale in os.listdir(root):  # a collector killed mid-write leaves its partial folder
        path = os.path.join(root, stale)
        try:
            if ".partial-" in stale and stale.startswith(".") and time.time() - os.lstat(path).st_mtime > 3600:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass
    partial = os.path.join(root, f".{name}.partial-{os.getpid()}")
    shutil.rmtree(partial, ignore_errors=True)
    os.makedirs(partial, mode=0o700)
    facts = {}
    sizes = {}

    def text_file(file_name, source, budget):
        if source is None:
            facts[file_name] = {"present": False}
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
    if run_dir is not None:
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
    if request.get("include_install") and engine_root is not None:
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
        "schema": SCHEMA, "policy": POLICY,
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
    if os.path.lexists(folder):
        shutil.rmtree(folder) if os.path.isdir(folder) and not os.path.islink(folder) else os.unlink(folder)
    os.rename(partial, folder)
    _prune(root, name)
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
