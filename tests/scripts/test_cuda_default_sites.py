from __future__ import annotations

import sys
import time
import types

import pytest

if "paramiko" not in sys.modules:
    sys.modules["paramiko"] = types.ModuleType("paramiko")

from astrid.packs.runpod import h3_release as build  # noqa: E402
from astrid.packs.runpod.cuda_versions import DEFAULT_ALLOWED_CUDA_VERSIONS  # noqa: E402
from astrid.packs.runpod import claim_capacity as claim  # noqa: E402
from scripts import pod_session as pod  # noqa: E402

EXPECTED = ("13.0", "13.1", "13.2", "13.3")


class _LaunchArgvCaptured(Exception):
    pass


def test_claim_script_reuses_the_module_default_list() -> None:
    assert DEFAULT_ALLOWED_CUDA_VERSIONS == EXPECTED
    assert claim.DEFAULT_ALLOWED_CUDA_VERSIONS is DEFAULT_ALLOWED_CUDA_VERSIONS




def test_pod_session_cuda_string_is_derived_from_the_shared_list() -> None:
    assert pod.CUDA_VERSIONS == ",".join(DEFAULT_ALLOWED_CUDA_VERSIONS)
    assert pod.CUDA_VERSIONS == ",".join(EXPECTED)
