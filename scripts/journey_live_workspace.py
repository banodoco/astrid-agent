"""Live canonical workspace seed and observations for the direct R0 journey."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


class LiveWorkspace:
    def __init__(self, endpoint, token):
        parsed = urlsplit(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or parsed.path not in ("", "/"):
            raise ValueError("Runtime endpoint must be loopback HTTP")
        self.endpoint, self.token = endpoint.rstrip("/"), token

    def request(self, method, path, body=None, *, key=None, binary=False):
        headers = {"Authorization": "Bearer " + self.token, "Origin": "https://astrid.haus"}
        if key:
            headers["Idempotency-Key"] = key
        if body is not None:
            if binary:
                headers.update({"Content-Type": "text/plain", "X-Filename": "release0.txt"})
            else:
                headers["Content-Type"] = "application/json"
                body = json.dumps(body).encode()
        with urlopen(Request(self.endpoint + path, data=body, headers=headers, method=method), timeout=120) as response:
            value = response.read()
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
    client.request("POST", path("projects", project_id, "timelines"), {
        "timeline_id": timeline, "name": "User edit " + generation}, key=timeline)
    revision = "tst-c1-parent-" + generation
    client.request("POST", base + "/composition-revisions", {
        "project_id": project_id, "timeline_id": timeline, "expected_head": None,
        "parent_revision_id": revision, "internal_timeline_revisions": [], "shot_revisions": [],
        "parent_composition": {"config": {"tracks": [], "clips": [], "metadata": {"user_note": generation}},
                               "registry": {"assets": {}}, "clips": [], "occurrences": []},
    }, key="composition-" + generation)
    preferences = client.request("GET", path("preferences", "user"))
    client.request("PUT", path("preferences", "user"), {
        "content": "Keep user changes " + generation, "expected_version": preferences["version"]},
        key="preferences-" + generation)
    payload = ("C1 object " + generation + "\n").encode()
    client.request("POST", path("objects"), payload, key="object-" + generation, binary=True)
    return {"project": project_id, "timeline": timeline, "revision": revision,
            "object_digest": hashlib.sha256(payload).hexdigest(), "generation": generation}


def snapshot(client, keys):
    project = client.request("GET", path("projects", keys["project"]))
    base = path("projects", keys["project"], "timelines", keys["timeline"])
    timeline = client.request("GET", base)
    if timeline["head_revision_id"] != keys["revision"]:
        raise ValueError("Seeded canonical composition is no longer the head")
    revision = client.request("GET", base + "/composition-revisions/" + quote(keys["revision"], safe=""))
    content = revision["payload"]
    generation = keys["generation"]
    if (timeline["name"] != "User edit " + generation
            or content["config"] != {"tracks": [], "clips": [], "metadata": {"user_note": generation}}
            or content["registry"] != {"assets": {}}):
        raise ValueError("Seeded canonical timeline content differs from the requested data")
    preferences = client.request("GET", path("preferences", "user"))
    if preferences["content"] != "Keep user changes " + generation:
        raise ValueError("Seeded preferences differ from the requested data")
    payload = client.request("GET", path("objects", keys["object_digest"]), binary=True)
    if hashlib.sha256(payload).hexdigest() != keys["object_digest"]:
        raise ValueError("Seeded object bytes do not match their digest")
    task = client.request("GET", path("tasks", keys["task_id"]))
    if task["state"] != "succeeded" or "result" not in task:
        raise ValueError("Seeded task is no longer settled successfully with a result")
    doctor = client.request("GET", path("doctor"))
    schema = doctor["checks"]["schema"]
    if schema["ok"] is not True or type(schema["actual_version"]) is not int or type(schema["expected_version"]) is not int:
        raise ValueError("Runtime doctor did not verify the selected schema")
    return {"project": {k: project[k] for k in ("project_id", "slug", "name", "metadata")},
            "timeline": {"name": timeline["name"], "head": timeline["head_revision_id"],
                         "revision": {k: revision[k] for k in ("revision_id", "content_digest", "payload")}},
            "preferences": {k: preferences[k] for k in ("content", "version")},
            "task": {k: task[k] for k in ("task_id", "state", "spec", "result")},
            "cas_sha256": hashlib.sha256(payload).hexdigest(),
            "schema_version": schema["actual_version"], "target_schema_version": schema["expected_version"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("seed", "snapshot"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    data = args.data.resolve(strict=True)
    if not (data.parent / "tst-b1-owner.json").is_file():
        raise ValueError("Live workspace requires the B1 owned run")
    discovery = json.loads((data / "runtime" / "discovery.json").read_text(encoding="utf-8"))
    credential = Path(discovery["credential_file"])
    if credential.is_symlink() or not credential.resolve(strict=True).is_relative_to(data):
        raise ValueError("Runtime credential is outside the owned data root")
    client = LiveWorkspace(discovery["endpoint"], credential.read_text(encoding="utf-8").strip())
    request = json.loads(args.request.read_text(encoding="utf-8"))
    result = seed(client, **request) if args.action == "seed" else snapshot(client, request)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
