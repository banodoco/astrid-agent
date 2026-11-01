#!/usr/bin/env python3
"""Authenticated GitHub Actions artifact collection for sealed hosted sources.

The caller supplies the source binding from the immutable producer plan.  API
responses and archive contents are treated as hostile until every run, job,
artifact, checksum, and member identity has been checked.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import urllib.request
import zipfile


API = "https://api.github.com"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 1000


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _positive_int(value: object, label: str) -> int:
    require(type(value) is int and value > 0, f"invalid {label}")
    return int(value)


class NoAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Do not leak the read-only API token to artifact storage redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and not newurl.startswith(API + "/"):
            redirected.remove_header("Authorization")
        return redirected


def get(path: str, binary: bool = False) -> object:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token := os.environ.get("C06_DOWNLOAD_TOKEN"):
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(API + path, headers=headers)
    with urllib.request.build_opener(NoAuthRedirect()).open(request, timeout=60) as response:
        data = response.read(MAX_ARCHIVE_BYTES + 1)
    require(len(data) <= MAX_ARCHIVE_BYTES, "API response too large")
    return data if binary else json.loads(data)


def source_key(source: dict[str, object]) -> str:
    """A canonical, cross-batch unique hosted source identity."""
    return hashlib.sha256(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def validate_source(source: object, tuple_repos: object) -> dict[str, object]:
    require(isinstance(source, dict), "hosted source is not an object")
    required = {"kind", "repo", "workflow_path", "workflow_revision", "run_id", "run_attempt", "job_id", "artifact_id", "artifact_name", "head_sha"}
    require(set(source) == required and source.get("kind") == "hosted", "invalid hosted source binding")
    repo = source["repo"]
    require(isinstance(repo, str) and re.fullmatch(r"[a-z0-9-]+", repo), "invalid hosted repository")
    require(isinstance(tuple_repos, dict) and isinstance(tuple_repos.get(repo), dict), "hosted source repo absent from tuple")
    tuple_repo = tuple_repos[repo]
    require(source["head_sha"] == tuple_repo.get("sha"), "hosted source head SHA is not tuple-bound")
    require(tuple_repo.get("url") == "https://github.com/banodoco/astrid-" + repo, "invalid hosted tuple URL")
    require(isinstance(source["workflow_path"], str) and source["workflow_path"].startswith(".github/workflows/") and
            ".." not in PurePosixPath(source["workflow_path"]).parts, "invalid hosted workflow path")
    require(isinstance(source["workflow_revision"], str) and re.fullmatch(r"[0-9a-f]{40}", source["workflow_revision"]),
            "invalid hosted workflow revision")
    for name in ("run_id", "run_attempt", "job_id", "artifact_id"):
        _positive_int(source[name], name)
    require(isinstance(source["artifact_name"], str) and re.fullmatch(r"[A-Za-z0-9_.-]+", source["artifact_name"]),
            "invalid hosted artifact name")
    return dict(source)


def _safe_member(name: str) -> str:
    path = PurePosixPath(name)
    require(bool(name) and not path.is_absolute() and ".." not in path.parts and "\\" not in name and str(path) == name,
            "unsafe archive member")
    return name


def download(source: object, tuple_repos: object, destination: str | Path, fetch=get) -> dict[str, object]:
    """Download one pre-bound artifact into a new private directory.

    ``fetch`` is intentionally injectable for deterministic synthetic tests;
    the production default queries GitHub's API with a read-only token.
    """
    binding = validate_source(source, tuple_repos)
    repo = str(binding["repo"])
    run_id, attempt, job_id, artifact_id = (int(binding[name]) for name in ("run_id", "run_attempt", "job_id", "artifact_id"))
    base = "/repos/banodoco/astrid-" + repo + "/actions"
    run = fetch(f"{base}/runs/{run_id}")
    jobs = fetch(f"{base}/runs/{run_id}/attempts/{attempt}/jobs?per_page=100")
    artifacts = fetch(f"{base}/runs/{run_id}/artifacts?per_page=100")
    require(isinstance(run, dict) and isinstance(jobs, dict) and isinstance(artifacts, dict), "malformed hosted API response")
    require(run.get("id") == run_id and run.get("run_attempt") == attempt and run.get("head_sha") == binding["head_sha"] and
            run.get("status") == "completed" and run.get("conclusion") == "success" and
            run.get("path") == binding["workflow_path"] and run.get("repository", {}).get("html_url") == tuple_repos[repo]["url"],
            "wrong or unsuccessful hosted run")
    all_jobs = jobs.get("jobs")
    require(jobs.get("total_count") == 1 and isinstance(all_jobs, list) and len(all_jobs) == 1, "hosted run is not one reviewed job")
    job = all_jobs[0]
    require(isinstance(job, dict) and job.get("id") == job_id and job.get("run_id") == run_id and
            job.get("run_attempt") == attempt and job.get("status") == "completed" and job.get("conclusion") == "success",
            "wrong or unsuccessful hosted job")
    listed = artifacts.get("artifacts")
    require(type(artifacts.get("total_count")) is int and 0 < artifacts["total_count"] <= 100 and
            isinstance(listed, list) and len(listed) == artifacts["total_count"], "incomplete hosted artifact list")
    matching = [item for item in listed if isinstance(item, dict) and item.get("id") == artifact_id]
    require(len(matching) == 1, "missing or duplicate hosted artifact")
    artifact = matching[0]
    require(artifact.get("name") == binding["artifact_name"] and artifact.get("expired") is False and
            artifact.get("workflow_run", {}).get("id") == run_id, "wrong hosted artifact association")
    digest = artifact.get("digest")
    require(isinstance(digest, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", digest), "missing hosted artifact digest")
    raw = fetch(f"{base}/artifacts/{artifact_id}/zip", True)
    require(isinstance(raw, bytes) and len(raw) <= MAX_ARCHIVE_BYTES and hashlib.sha256(raw).hexdigest() == digest[7:],
            "hosted archive digest mismatch")
    target = Path(destination)
    require(not target.exists(), "hosted artifact destination already exists")
    target.mkdir(mode=0o700, parents=True)
    files: list[dict[str, object]] = []
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            require(0 < len(members) <= MAX_MEMBERS, "invalid hosted archive member count")
            names: set[str] = set()
            for member in members:
                name = _safe_member(member.filename)
                require(not member.is_dir() and not ((member.external_attr >> 16) & 0o170000 == 0o120000) and name not in names,
                        "duplicate, directory, or symlink hosted archive member")
                names.add(name)
                total += member.file_size
                require(total <= MAX_ARCHIVE_BYTES, "expanded hosted archive too large")
                data = archive.read(member)
                path = target / name
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_bytes(data)
                files.append({"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid hosted artifact archive") from exc
    names = {str(item["path"]) for item in files}
    require({"manifest.json", "junit.xml"} <= names, "hosted artifact lacks manifest or junit")
    return {
        "source": binding,
        "run": {"id": run_id, "attempt": attempt, "job_id": job_id, "artifact_id": artifact_id,
                "artifact_name": binding["artifact_name"], "head_sha": binding["head_sha"]},
        "archive_sha256": digest[7:],
        "files": files,
    }
