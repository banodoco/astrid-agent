from __future__ import annotations

import sys
import time
import types

import pytest

if "paramiko" not in sys.modules:
    sys.modules["paramiko"] = types.ModuleType("paramiko")

from astrid.packs.runpod import h3_release as build  # noqa: E402
from astrid.packs.runpod.cuda_versions import DEFAULT_ALLOWED_CUDA_VERSIONS  # noqa: E402
from scripts import claim_runpod_5090_backup as claim  # noqa: E402
from scripts import pod_session as pod  # noqa: E402
from scripts import watch_h3_5090_storage as watcher  # noqa: E402

EXPECTED = ("13.0", "13.1", "13.2", "13.3")


class _LaunchArgvCaptured(Exception):
    pass


def test_claim_script_reuses_the_module_default_list() -> None:
    assert DEFAULT_ALLOWED_CUDA_VERSIONS == EXPECTED
    assert claim.DEFAULT_ALLOWED_CUDA_VERSIONS is DEFAULT_ALLOWED_CUDA_VERSIONS


def test_claim_h3_and_watch_default_sites_produce_the_same_list(monkeypatch) -> None:
    claim_list = tuple(claim._parser().parse_args([]).allowed_cuda_versions.split(","))

    from runpod_lifecycle import api

    captured: dict = {}

    def fake_create_pod(*_args, **kwargs):
        captured.update(kwargs)
        return {"id": "pod"}

    monkeypatch.setattr(api, "create_pod", fake_create_pod)
    provider = build.RunPodProvider("TEST-KEY")
    try:
        provider.create(
            {"id": "vol", "mount": "/workspace", "datacenter": "TEST-1"},
            "image",
            "NVIDIA GeForce RTX 5090",
            "name",
        )
    finally:
        provider.http.close()
    h3_list = tuple(captured["allowed_cuda_versions"])

    seen: list[list[str]] = []

    def fake_cli(args, timeout):
        seen.append(list(args))
        raise _LaunchArgvCaptured

    monkeypatch.setattr(watcher, "cli", fake_cli)
    with pytest.raises(_LaunchArgvCaptured):
        watcher.launch(time.time() + 60)
    argv = seen[0]
    watch_list = tuple(argv[argv.index("--allowed-cuda-versions") + 1].split(","))

    assert claim_list == EXPECTED
    assert h3_list == EXPECTED
    assert watch_list == EXPECTED


def test_pod_session_cuda_string_is_derived_from_the_shared_list() -> None:
    assert pod.CUDA_VERSIONS == ",".join(DEFAULT_ALLOWED_CUDA_VERSIONS)
    assert pod.CUDA_VERSIONS == ",".join(EXPECTED)
