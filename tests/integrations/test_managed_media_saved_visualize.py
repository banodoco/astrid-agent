"""A saved revision whose media came from the managed media-add path must visualize.

The edit entry for such media carries only a content-addressed ``media_id``; the
managed media record supplies the digest. The materializer and the scene
observer must resolve the identity from that one source, and the host-side file
locator must stay out of the retained-snapshot comparison.
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.conftest import owned_process_groups, real_runtime  # noqa: F401
from tests.sdk.test_timeline_editing_acceptance_runtime import editing_case  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]


def _png(path: Path, color: str) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=1",
         "-frames:v", "1", "-threads", "1", "-c:v", "png", str(path)],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    return path


def _start_host(daemon):
    from astrid.sdk.host_bootstrap import PACK_HOST_ACTOR, PACK_HOST_SCOPES, ensure_pack_host

    worker = Path(daemon.worker_credential_path)
    os.environ.update({
        "ASTRID_RUNTIME_ENDPOINT": daemon.endpoint,
        "ASTRID_RUNTIME_CREDENTIAL": str(worker),
        "ASTRID_RUNTIME_DATA_ROOT": str(worker.parent.parent.parent),
    })
    return ensure_pack_host(
        {
            "endpoint": daemon.endpoint, "worker_credential_file": str(worker),
            "worker_actor": PACK_HOST_ACTOR, "worker_scopes": PACK_HOST_SCOPES,
            "source_checkout": str(ROOT),
        },
        reconfigure_action="managed media visualize test",
    )


def _visualize(client, project, *args):
    from astrid.packs.timeline.cli import build_parser

    parsed = build_parser(client).parse_args(["visualize", "film", "--project", project, *args])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = parsed.handler(parsed)
        except SystemExit as exit_:
            code = exit_.code
    return code, out.getvalue() + err.getvalue()


@pytest.mark.remotion
@pytest.mark.ffmpeg
def test_saved_revision_with_managed_media_visualizes_a_frame(editing_case, tmp_path):
    daemon, client, project, timeline = editing_case
    _start_host(daemon)
    first, second = _png(tmp_path / "one.png", "red"), _png(tmp_path / "two.png", "blue")

    def place(t):
        t.add(first, at=0, hold=1, track="V1", id="shot-1")
        t.add(second, at=1, hold=1, track="V1", id="shot-2")

    saved = timeline.commit(place, label="Place two files", key="managed-media-visualize")
    revision = saved["data"]["accepted_revision_id"]

    code, text = _visualize(client, project, "--at", "shot-1", "--revision-id", revision)
    assert code == 0, text
    assert "materialized registry differs" not in text
    assert "requires a runtime-managed digest" not in text
    assert "removeprefix" not in text
    assert re.search(r"\.png", text), text
