"""Export whitelisted facts from the matched audio diagnostic."""
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from matched_darwin_audio import CANDIDATE, REFERENCE, RUNTIME, TEST, VIBE


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(private, output):
    pair_file = private / "pair.json"
    pair = json.loads(pair_file.read_bytes()) if pair_file.is_file() else {}
    sources = {"reference": REFERENCE, "candidate": CANDIDATE}
    report = {
        "test": TEST,
        "runtime": RUNTIME,
        "vibecomfy": VIBE,
        "complete": pair.get("complete") is True,
        "matched_packages": pair.get("matched_packages") is True,
        "binary_sha256": {},
        "common_source_blobs": {},
        "cells": [],
    }
    for name in ("node", "ffmpeg", "ffprobe"):
        value = pair.get("binaries", {}).get(name, {}).get("sha256", "")
        if re.fullmatch(r"[0-9a-f]{64}", value):
            report["binary_sha256"][name] = value
    for path, blob in pair.get("equal_source_blobs", {}).items():
        if re.fullmatch(r"[A-Za-z0-9_./-]+", path) and re.fullmatch(r"[0-9a-f]{40}", blob):
            report["common_source_blobs"][path] = blob
    for side, sha in sources.items():
        folder = private / side
        native = folder / "result.xml"
        receipt = folder / "receipt.json"
        row = json.loads(receipt.read_bytes()) if receipt.is_file() else {}
        cell = {"side": side, "source": sha, "exit": None, "outcomes": [], "files": {}}
        if row.get("head") == sha and type(row.get("exit_code")) is int:
            cell["exit"] = row["exit_code"]
        for name in ("result.xml", "console.log", "receipt.json", "packages.json",
                     "import-origins.json", "editable-install.json", "checkout.json"):
            file = folder / name
            if file.is_file():
                cell["files"][name] = {"sha256": digest(file), "bytes": file.stat().st_size}
        if native.is_file():
            nodes = ET.parse(native).findall(".//testcase")
            for case in nodes:
                if (case.get("classname") != "tests.packs.test_audio_render.AudioRenderTest"
                        or case.get("name") != TEST.split("::")[-1]):
                    raise ValueError("Unexpected native test identity")
                status = next((tag for tag in ("failure", "error", "skipped") if case.find(tag) is not None), "passed")
                cell["outcomes"].append(status)
            if len(nodes) != 1:
                report["complete"] = False
        else:
            report["complete"] = False
        if cell["exit"] is None:
            report["complete"] = False
        log = folder / "console.log"
        if log.is_file():
            raw = log.read_bytes()
            cell["renderer_internal_error"] = b"RendererInternalError" in raw
            cell["reconciliation_budget_elapsed_seconds"] = [
                float(x) for x in re.findall(rb"candidate reconciliation budget ran out after (\d+(?:\.\d+)?)s", raw)
            ]
        report["cells"].append(cell)
    output.write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["complete"] and report["matched_packages"] else 1


if __name__ == "__main__":
    raise SystemExit(run(Path(sys.argv[1]), Path(sys.argv[2])))
