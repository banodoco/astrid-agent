"""Live canonical workspace seed and observations for the direct R0 journey."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import traceback
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

if __package__:
    from . import journey_data_roots
else:
    import journey_data_roots

ERROR_CODES = frozenset({"runtime_error", "unauthorized", "conflict", "validation_error", "forbidden",
                         "unavailable", "not_found", "retired_route", "owner_busy", "realm_admission_failed",
                         "lease_fenced", "protocol_error", "invalid_request"})
ERROR_TYPES = frozenset({"HTTPError", "URLError", "TimeoutError", "JSONDecodeError", "KeyError", "ValueError",
                         "TypeError", "RuntimeError", "OSError", "FileNotFoundError", "PermissionError"})
FAILURE_FUNCTIONS = frozenset({"main", "execute", "selection_for", "check_selection", "request", "seed", "snapshot"})
SNAPSHOT_FAILURE_FIELDS = frozenset({"project", "timeline", "object_digest", "task_id", "id", "slug", "name",
                                    "metadata", "content", "version", "status", "task", "schema_version",
                                    "target_schema_version", "project_id", "timeline_id", "cas_sha256",
                                    "head_revision_id"})


def failure_diagnostic(exc):
    """Only fixed vocabulary and source locations cross the artifact boundary."""
    frames = [{"function": frame.name, "line": frame.lineno}
              for frame in traceback.extract_tb(exc.__traceback__)
              if Path(frame.filename).name == "journey_live_workspace.py" and frame.name in FAILURE_FUNCTIONS]
    kind = type(exc).__name__
    missing_field = None
    if isinstance(exc, KeyError) and len(exc.args) == 1 and isinstance(exc.args[0], str):
        if exc.args[0] in SNAPSHOT_FAILURE_FIELDS:
            missing_field = exc.args[0]
    snapshot_lookup = getattr(exc, "safe_snapshot_lookup", None)
    schema = "astrid.live-workspace-failure.v3" if snapshot_lookup is not None else "astrid.live-workspace-failure.v2"
    diagnostic = {"schema": schema, "error_type": kind if kind in ERROR_TYPES else "other",
            "frames": frames[-10:], "http_status": exc.code if isinstance(exc, HTTPError) and type(exc.code) is int and 100 <= exc.code <= 599 else None,
            "runtime_code": getattr(exc, "safe_runtime_code", None) if isinstance(exc, HTTPError) else None,
            "missing_field": missing_field}
    if snapshot_lookup is not None:
        diagnostic["snapshot_lookup"] = snapshot_lookup
    return diagnostic


def _json_type(value):
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) in (int, float):
        return "number"
    if type(value) is str:
        return "string"
    if type(value) is dict:
        return "object"
    if type(value) is list:
        return "array"
    return "other"


def _lookup_field(value, field):
    present = type(value) is dict and field in value
    return {"present": present, "type": _json_type(value[field]) if present else None}


def snapshot_lookup_diagnostic(timeline, keys):
    """Describe only the two lookup containers and field presence/types."""
    return {"timeline_type": _json_type(timeline),
            "timeline_head_revision_id": _lookup_field(timeline, "head_revision_id"),
            "keys_type": _json_type(keys),
            "keys_head": _lookup_field(keys, "head")}


def _valid_snapshot_lookup(value):
    def valid_field(field):
        return (isinstance(field, dict) and set(field) == {"present", "type"}
                and type(field["present"]) is bool
                and (field["type"] is None or type(field["type"]) is str and field["type"] in
                     {"null", "boolean", "number", "string", "object", "array", "other"})
                and (field["present"] or field["type"] is None)
                and (not field["present"] or field["type"] is not None))
    return (isinstance(value, dict) and set(value) == {"timeline_type", "timeline_head_revision_id", "keys_type", "keys_head"}
            and type(value["timeline_type"]) is str
            and value["timeline_type"] in {"null", "boolean", "number", "string", "object", "array", "other"}
            and type(value["keys_type"]) is str
            and value["keys_type"] in {"null", "boolean", "number", "string", "object", "array", "other"}
            and valid_field(value["timeline_head_revision_id"])
            and valid_field(value["keys_head"]))


def valid_failure_diagnostic(value):
    if not isinstance(value, dict):
        return False
    schema = value.get("schema")
    expected = {"schema", "error_type", "frames", "http_status", "runtime_code"}
    if schema in {"astrid.live-workspace-failure.v2", "astrid.live-workspace-failure.v3"}:
        expected.add("missing_field")
    if schema == "astrid.live-workspace-failure.v3":
        expected.add("snapshot_lookup")
    return (set(value) == expected
            and schema in {"astrid.live-workspace-failure.v1", "astrid.live-workspace-failure.v2", "astrid.live-workspace-failure.v3"}
            and isinstance(value["error_type"], str) and value["error_type"] in ERROR_TYPES | {"other"}
            and isinstance(value["frames"], list) and len(value["frames"]) <= 10
            and all(isinstance(frame, dict) and set(frame) == {"function", "line"}
                    and isinstance(frame["function"], str) and frame["function"] in FAILURE_FUNCTIONS and type(frame["line"]) is int
                    and 0 < frame["line"] < 10000 for frame in value["frames"])
            and (value["http_status"] is None or type(value["http_status"]) is int and 100 <= value["http_status"] <= 599)
            and (value["runtime_code"] is None or isinstance(value["runtime_code"], str) and value["runtime_code"] in ERROR_CODES)
            and (schema == "astrid.live-workspace-failure.v1" or
                 value["missing_field"] is None or
                 isinstance(value["missing_field"], str) and value["missing_field"] in SNAPSHOT_FAILURE_FIELDS)
            and (schema != "astrid.live-workspace-failure.v3" or _valid_snapshot_lookup(value["snapshot_lookup"])))


class LiveWorkspace:
    def __init__(self, endpoint, token, guard=None):
        parsed = urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.path not in ("", "/"):
            raise ValueError("Runtime endpoint must be loopback HTTP")
        self.endpoint, self.token, self.guard = endpoint.rstrip("/"), token, guard

    def request(self, method, path, body=None, *, key=None, binary=False):
        if self.guard and method not in ("GET", "HEAD"):
            self.guard()
        headers = {"Authorization": "Bearer " + self.token, "Origin": "https://astrid.haus"}
        if key:
            headers["Idempotency-Key"] = key
        if body is not None:
            if binary:
                headers.update({"Content-Type": "text/plain", "X-Filename": "release0.txt"})
            else:
                headers["Content-Type"] = "application/json"
                body = json.dumps(body).encode()
        try:
            with urlopen(Request(self.endpoint + path, data=body, headers=headers, method=method), timeout=120) as response:
                value = response.read()
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read(16384))
                code = detail.get("code") if isinstance(detail, dict) else None
                if isinstance(code, str) and code in ERROR_CODES:
                    exc.safe_runtime_code = code
            except (ValueError, OSError):
                pass
            raise
        return value if binary else json.loads(value)


def path(*parts):
    return "/v1/" + "/".join(quote(str(part), safe="") for part in parts)


def seed(client, project, generation, task_id=None, create_project=False):
    if not create_project:
        raise ValueError("Live R0 seed requires its own project")
    project_id = client.request("POST", path("projects"), {
        "slug": project, "name": "Release-0 user project " + generation,
        "metadata": {"seed": "release0-to-release1"}}, key="project-" + generation)["data"]["project_id"]
    timeline = "tst-c1-" + generation
    base = path("projects", project_id, "timelines", timeline)
    # The smallest authentic route: create the timeline the way a Release 9 user does, then
    # record what the public HTTP timeline resource serves. The public create and GET omit
    # the revision fields, so only the public identity fields are recorded and checked.
    client.request("POST", path("projects", project_id, "timelines"), {
        "timeline_id": timeline, "name": "User edit " + generation}, key=timeline)
    public = client.request("GET", base)
    if not isinstance(public, dict) or public.get("name") != "User edit " + generation:
        raise ValueError("Created timeline is not served with its requested name")
    if "head_revision_id" not in public:
        raise ValueError("Created timeline serves no head field")
    head = public["head_revision_id"]
    if head is not None and (not isinstance(head, str) or not head):
        raise ValueError("Created timeline serves an invalid head field")
    preference_path = path("preferences", "project") + "?project_id=" + quote(project_id, safe="")
    preferences = client.request("GET", preference_path)
    if preferences["version"] != 0 or preferences["content"]:
        raise ValueError("Seed project already has preferences")
    client.request("PUT", preference_path, {
        "content": "Keep user changes " + generation, "expected_version": 0},
        key="preferences-" + generation)
    payload = ("C1 object " + generation + "\n").encode()
    client.request("POST", path("objects"), payload, key="object-" + generation, binary=True)
    return {"project": project_id, "timeline": timeline, "head": head,
            "object_digest": hashlib.sha256(payload).hexdigest(), "generation": generation}


R11_PACK_REFUSAL = "stable selected pack capture requires Linux file lease support"


def snapshot(client, keys):
    project = client.request("GET", path("projects", keys["project"]))
    base = path("projects", keys["project"], "timelines", keys["timeline"])
    timeline = client.request("GET", base)
    lookup_diagnostic = snapshot_lookup_diagnostic(timeline, keys)
    generation = keys["generation"]
    # The public timeline resource is the only timeline identity a user's HTTP client sees.
    # Its name and head must be the ones the seed recorded from that same resource.
    try:
        if timeline["name"] != "User edit " + generation:
            raise ValueError("Seeded timeline identity differs from the requested data")
        if timeline["head_revision_id"] != keys["head"]:
            raise ValueError("Seeded timeline head differs from the seeded head")
    except (KeyError, TypeError) as exc:
        exc.safe_snapshot_lookup = lookup_diagnostic
        raise
    preference_path = path("preferences", "project") + "?project_id=" + quote(keys["project"], safe="")
    preferences = client.request("GET", preference_path)
    if preferences["content"] != "Keep user changes " + generation:
        raise ValueError("Seeded preferences differ from the requested data")
    payload = client.request("GET", path("objects", keys["object_digest"]), binary=True)
    if hashlib.sha256(payload).hexdigest() != keys["object_digest"]:
        raise ValueError("Seeded object bytes do not match their digest")
    task = client.request("GET", path("tasks", keys["task_id"]))
    if keys.get("r11_selected_pack_refusal") is True and keys.get("generation") == "release11":
        error = (task.get("result") or {}).get("error") or {}
        if task.get("state") != "failed" or error.get("code") != "executor_failed" or error.get("message") != R11_PACK_REFUSAL:
            raise ValueError("Seeded R11 task no longer has the expected selected-pack refusal")
    elif task["state"] != "succeeded" or "result" not in task:
        raise ValueError("Seeded task is no longer settled successfully with a result")
    health = client.request("GET", path("health"))
    expected_schema_digest = keys.get("expected_schema_digest")
    if (not isinstance(expected_schema_digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_schema_digest)):
        raise ValueError("Selected installation lacks its Runtime schema digest")
    if (health.get("protocol") != "workspace.v1"
            or health.get("schema_digest") != expected_schema_digest):
        raise ValueError("Runtime health does not match the selected schema")
    return {"project": {k: project[k] for k in ("project_id", "slug", "name", "metadata")},
            "timeline": {"name": timeline["name"], "head": timeline["head_revision_id"]},
            "preferences": {k: preferences[k] for k in ("content", "version")},
            "task": {k: task[k] for k in ("task_id", "state", "spec", "result")},
            "cas_sha256": hashlib.sha256(payload).hexdigest(),
            "schema_digest": health["schema_digest"], "target_schema_digest": expected_schema_digest}


# These fields bind discovery to the owner selected by the parent. Do not pass
# the credential itself through the command line or the evidence files.
SELECTION_FIELDS = ("endpoint", "realm_root", "active_realm", "runtime_instance_id",
                    "pid", "process_birth_id", "credential_file")


def selection_for(data, root, discovery):
    data, root = Path(data), Path(root)
    recorded_data, active_data = journey_data_roots.selection(root, data)
    if (data != active_data or root != recorded_data.parent / "program"
            or recorded_data != recorded_data.parent / "data"
            or not data.is_dir() or not recorded_data.is_dir() or not root.is_dir()):
        raise ValueError("Live workspace requires the owned program and data roots")
    owner_path = recorded_data.parent / "tst-b1-owner.json"
    if owner_path.is_symlink():
        raise ValueError("Live workspace ownership record cannot be a symlink")
    owner = json.loads(owner_path.read_text(encoding="utf-8"))
    if (owner.get("schema_version") != 1
            or not re.fullmatch(r"[0-9a-f]{32}", str(owner.get("run_id", "")))
            or owner.get("root") != str(root) or owner.get("data_root") != str(recorded_data)):
        raise ValueError("Live workspace ownership record does not match the run")
    selection = {field: discovery.get(field) for field in SELECTION_FIELDS}
    if any(not selection[field] for field in SELECTION_FIELDS):
        raise ValueError("Runtime discovery lacks selected owner identity")
    realm = Path(selection["realm_root"])
    if not realm.is_absolute() or not realm.is_dir() or realm != realm.resolve() or not realm.is_relative_to(data):
        raise ValueError("Selected workspace is missing or outside the owned run")
    credential = Path(selection["credential_file"])
    if (not credential.is_absolute() or credential.is_symlink()
            or not credential.resolve(strict=True).is_relative_to(data)):
        raise ValueError("Runtime credential is outside the owned data root")
    selection["credential_sha256"] = hashlib.sha256(credential.read_bytes()).hexdigest()
    selection["run_id"] = owner["run_id"]
    return selection


def check_selection(data, root, expected):
    try:
        discovery = json.loads((data / "runtime" / "discovery.json").read_text(encoding="utf-8"))
        current = selection_for(data, root, discovery)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Runtime owner changed during live workspace access") from exc
    if current != expected:
        raise RuntimeError("Runtime owner changed during live workspace access")
    # Health is queried at the bound endpoint, never at an independently
    # selected discovery endpoint.
    client = LiveWorkspace(expected["endpoint"], "")
    health = client.request("GET", "/v1/health")
    if health.get("status") != "ok" or health.get("runtime_instance_id") != expected["runtime_instance_id"]:
        raise RuntimeError("Runtime owner changed during live workspace access")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "snapshot"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path)
    args = parser.parse_args()
    try:
        execute(args)
    except Exception as exc:
        if args.diagnostic:
            try:
                args.diagnostic.write_text(json.dumps(failure_diagnostic(exc), sort_keys=True) + "\n", encoding="utf-8")
            except OSError:
                pass
        raise


def credential_token(credential):
    # Discovery advertises the Astrid profile JSON, not the daemon owner token.
    try:
        value = json.loads(credential.read_text(encoding="utf-8"))
        token = value.get("token") if isinstance(value, dict) else None
        if not isinstance(token, str) or not re.fullmatch(r"[!-~]+", token):
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("Invalid Astrid credential") from None
    return token


def execute(args):
    data, root = args.data, args.root
    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    check_selection(data, root, selection)
    credential = Path(selection["credential_file"])
    client = LiveWorkspace(selection["endpoint"], credential_token(credential),
                           guard=lambda: check_selection(data, root, selection))
    request = json.loads(args.request.read_text(encoding="utf-8"))
    result = seed(client, **request) if args.action == "seed" else snapshot(client, request)
    check_selection(data, root, selection)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
