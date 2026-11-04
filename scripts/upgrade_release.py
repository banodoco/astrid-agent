#!/usr/bin/env python3
"""Resolve pinned and currently served release inputs without mutating source repos."""
from __future__ import annotations

import argparse
import ast
import base64
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASES = ROOT / "tests/upgrade/releases.json"
FIXTURE = ROOT / "tests/upgrade/fixtures/served-observation.json"


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, stdout=subprocess.PIPE).stdout


def config() -> dict:
    return json.loads(RELEASES.read_text(encoding="utf-8"))


def configured_repo(name: str, option: Path | None) -> Path:
    env_name = config()[name]
    repo = option or (Path(os.environ[env_name]) if os.environ.get(env_name) else None)
    if repo is None:
        raise ValueError(f"Pass --{name.removesuffix('_env').replace('_', '-')} or set {env_name} to a local checkout")
    return repo.resolve()


def manifest_observation(url: str, fixture: Path | None = None) -> dict:
    if fixture:
        observation = json.loads(fixture.read_text(encoding="utf-8"))
        if observation.get("source_url") != url or not isinstance(observation.get("manifest"), dict):
            raise ValueError("offline observation fixture has the wrong URL or manifest")
        return {**observation, "evidence_mode": "fixture"}
    request = urllib.request.Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": "TST-A2-resolver/1"})
    fetched_at = dt.datetime.now(dt.timezone.utc).isoformat()
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read()
        return {
            "source_url": response.geturl(), "fetched_at_utc": fetched_at,
            "http_status": response.status, "response_date": response.headers.get("Date"),
            "etag": response.headers.get("ETag"), "cache_control": response.headers.get("Cache-Control"),
            "manifest_sha256": hashlib.sha256(raw).hexdigest(), "manifest": json.loads(raw),
            "manifest_base64": base64.b64encode(raw).decode("ascii"),
            "evidence_mode": "live",
        }


def find_manifest_commit(repo: Path, manifest: dict) -> str | None:
    agent_sha = manifest.get("agent", {}).get("sha")
    if not isinstance(agent_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", agent_sha):
        return None
    candidates = git(repo, "log", "--all", "--format=%H", "-G", agent_sha, "--", "public/install-manifest.json").decode().splitlines()
    for commit in candidates:
        try:
            source = json.loads(git(repo, "show", f"{commit}:public/install-manifest.json"))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            continue
        if source == manifest:
            return commit
    return None


def resolve(release_id: str, output: Path, *, app_repo: Path | None = None, observation_fixture: Path | None = None) -> dict:
    cfg = config()
    release = next((r for r in cfg["releases"] if r["id"] == release_id), None)
    if release is None:
        raise ValueError(f"Unknown release id: {release_id}")
    repo = configured_repo("app_repo_env", app_repo)
    evidence = None
    if release["source"] == "live-url":
        try:
            evidence = manifest_observation(cfg["served_url"], observation_fixture)
            manifest = evidence["manifest"]
            commit = find_manifest_commit(repo, manifest)
        except Exception as exc:
            output.mkdir(parents=True, exist_ok=True)
            result = {"id": release_id, "status": "environmental-unverified", "source_url": cfg["served_url"], "error": f"{type(exc).__name__}: {exc}", "local_candidate_used": False}
            (output / "resolved.json").write_text(json.dumps(result, indent=2) + "\n")
            return result
        if commit is None:
            output.mkdir(parents=True, exist_ok=True)
            result = {"id": release_id, "status": "environmental-unverified", "evidence": evidence, "error": "served manifest has no exact matching manifest commit in the supplied local App clone", "local_candidate_used": False}
            (output / "served-observation.json").write_text(json.dumps(evidence, indent=2) + "\n")
            (output / "resolved.json").write_text(json.dumps(result, indent=2) + "\n")
            return result
        source_ref = "live-url"
    else:
        commit = git(repo, "rev-parse", f"{release['app_commit']}^{{commit}}").decode().strip()
        manifest = json.loads(git(repo, "show", f"{commit}:public/install-manifest.json"))
        source_ref = release["app_commit"]
    raw_manifest = git(repo, "show", f"{commit}:public/install-manifest.json")
    # For a live observation, preserve the endpoint's exact bytes and metadata; local git
    # provides provenance and the matching bootstrap blobs, not a substitute manifest.
    manifest_bytes = base64.b64decode(evidence["manifest_base64"]) if evidence else raw_manifest
    output.mkdir(parents=True, exist_ok=True)
    (output / "install-manifest.json").write_bytes(manifest_bytes)
    bootstrap = []
    for relpath in cfg["bootstrap_paths"]:
        body = git(repo, "show", f"{commit}:{relpath}")
        dest = output / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        bootstrap.append({"path": relpath, "sha256": hashlib.sha256(body).hexdigest()})
    agent, runtime = manifest["agent"]["sha"], manifest["runtime"]["sha"]
    schema_digest = manifest["runtime"].get("schema_digest")
    tuple_hash = hashlib.sha256("\0".join((agent, runtime, schema_digest or "")).encode()).hexdigest()[:16]
    result = {"id": release_id, "status": "verified", "source": source_ref, "app_manifest_commit": commit,
              "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
              "identity": {"agent": agent, "runtime": runtime, "schema_digest": schema_digest},
              "leg_id": tuple_hash, "bootstrap": bootstrap, "local_candidate_used": False}
    if evidence:
        result["served_evidence"] = {key: evidence.get(key) for key in
                                      ("source_url", "fetched_at_utc", "http_status", "response_date", "etag", "cache_control", "manifest_sha256", "evidence_mode")}
        (output / "served-observation.json").write_text(json.dumps(evidence, indent=2) + "\n")
    (output / "resolved.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def dedupe_legs(releases: list[dict]) -> list[dict]:
    by_tuple = {}
    for release in releases:
        identity = release["identity"]
        by_tuple.setdefault((identity["agent"], identity["runtime"], identity.get("schema_digest")), release)
    return list(by_tuple.values())


def _constant_assignment(path: Path, name: str) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == name for t in targets):
                value = node.value
                if isinstance(value, ast.Constant) and type(value.value) is int:
                    return value.value
    raise ValueError(f"Cannot find integer {name} declaration in {path}")


def _journal_version(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.comparators) != 1:
            continue
        pair = [(node.left, node.comparators[0])]
        for left, right in pair:
            for call, constant in ((left, right), (right, left)):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "get"
                        and isinstance(call.func.value, ast.Name) and call.func.value.id == "journal"
                        and call.args and isinstance(call.args[0], ast.Constant) and call.args[0].value == "version"
                        and isinstance(constant, ast.Constant) and type(constant.value) is int):
                    found.add(constant.value)
    if len(found) != 1:
        raise ValueError(f"Expected exactly one journal version declaration in {path}, found {sorted(found)}")
    return next(iter(found))


def _annotation_shape(node: ast.AST) -> str:
    text = ast.unparse(node)
    if "dict" in text.lower():
        return "mapping"
    if "Path" in text or "str" in text:
        return "string-or-null" if "None" in text else "string"
    if "int" in text:
        return "integer-or-null" if "None" in text else "integer"
    return "expression"


def _shape(node: ast.AST, annotations: dict[str, str]):
    if isinstance(node, ast.Dict):
        return {key.value: _shape(value, annotations) for key, value in zip(node.keys, node.values) if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return {"dict": "mapping", "str": "string", "int": "integer"}.get(node.func.id, "expression")
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return annotations.get(node.attr, "expression")
    if isinstance(node, ast.Constant):
        return {str: "string", int: "integer", bool: "boolean", type(None): "null"}.get(type(node.value), "value")
    return "expression"


def _version_record_shape(path: Path) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Version":
            annotations = {n.target.id: _annotation_shape(n.annotation) for n in node.body
                           if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
            method = next((n for n in node.body if isinstance(n, ast.FunctionDef) and n.name == "document"), None)
            if method:
                ret = next((n.value for n in ast.walk(method) if isinstance(n, ast.Return)), None)
                if isinstance(ret, ast.Dict):
                    return _shape(ret, annotations)
    raise ValueError(f"Cannot find Version.document serialization in {path}")


def collect_format_versions(runtime_repo: Path, agent_repo: Path, runtime_ref: str = "HEAD", agent_ref: str = "HEAD") -> dict:
    store = git(runtime_repo, "show", f"{runtime_ref}:runtime_protocol/store.py").decode()
    service = git(runtime_repo, "show", f"{runtime_ref}:runtime_protocol/service.py").decode()
    core = git(agent_repo, "show", f"{agent_ref}:astrid/installer/core.py").decode()
    with __import__("tempfile").TemporaryDirectory() as temp:
        temp = Path(temp)
        (temp / "store.py").write_text(store)
        (temp / "service.py").write_text(service)
        (temp / "core.py").write_text(core)
        return {"workspace_schema_version": _constant_assignment(temp / "store.py", "SCHEMA_VERSION"),
                "journal_format_version": _journal_version(temp / "service.py"),
                "version_record_shape": _version_record_shape(temp / "core.py")}


def check_format_review(runtime_repo: Path, agent_repo: Path, runtime_ref: str = "HEAD", agent_ref: str = "HEAD") -> None:
    cfg = config()
    actual = collect_format_versions(runtime_repo, agent_repo, runtime_ref, agent_ref)
    review = cfg["format_review"]
    keys = ("workspace_schema_version", "journal_format_version", "version_record_shape")
    changed = {key: {"reviewed": review.get(key), "current": actual[key]} for key in keys if review.get(key) != actual[key]}
    if changed:
        raise ValueError("Production upgrade format changed without a release-list review entry: " + json.dumps(changed, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command", required=True)
    cmd = subs.add_parser("resolve")
    cmd.add_argument("id")
    cmd.add_argument("--app-repo", type=Path)
    cmd.add_argument("--offline-observation", type=Path, help="Use a captured served endpoint observation fixture")
    cmd.add_argument("--output", type=Path)
    guard = subs.add_parser("check-format-review")
    guard.add_argument("--runtime-repo", type=Path)
    guard.add_argument("--agent-repo", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        if args.command == "resolve":
            result = resolve(args.id, args.output or ROOT / ".upgrade-releases" / args.id,
                             app_repo=args.app_repo, observation_fixture=args.offline_observation)
            print(json.dumps(result, indent=2))
            return 0 if result.get("status") == "verified" else 2
        check_format_review(configured_repo("runtime_repo_env", args.runtime_repo), args.agent_repo)
        print("production upgrade format review is current")
    except (ValueError, RuntimeError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
