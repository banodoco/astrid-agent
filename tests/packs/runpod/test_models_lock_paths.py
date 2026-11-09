"""The RunPod models lock names each file by its repo-relative path.

``path`` is the path inside the Hugging Face repository (the resolve URL
uses it); ``destination`` is the local install location. For the sources
below the local install adds a directory prefix that the repository does
not have, so ``path`` must not carry it (a prefixed path 404s).
"""

import json
from pathlib import Path, PurePosixPath

import pytest

LOCK = Path(__file__).resolve().parents[3] / "astrid/packs/runpod/models.lock.json"

# (repository, local install prefix the repository does not contain)
LOCAL_PREFIXES = (
    ("tencent/HunyuanVideo-Foley", "hunyuan_foley/"),
    ("google/siglip2-base-patch16-512", "hunyuan_foley/siglip2/"),
    ("laion/larger_clap_general", "hunyuan_foley/clap/"),
    ("Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice", "TTS/Qwen3-TTS/Qwen3-TTS-12Hz-0.6B-CustomVoice/"),
)


def _models():
    return json.loads(LOCK.read_text(encoding="utf-8"))["models"]


def test_every_lock_path_is_repo_relative_and_safe():
    for m in _models():
        path = m["path"]
        assert path and not path.startswith("/"), m["name"]
        assert ".." not in PurePosixPath(path).parts, m["name"]


@pytest.mark.parametrize("repository,prefix", LOCAL_PREFIXES)
def test_local_prefix_is_not_in_the_repository_path(repository, prefix):
    rows = [m for m in _models() if m["repository"] == repository]
    assert rows, f"no lock rows for {repository}"
    for m in rows:
        assert m["destination"].startswith(prefix), m["name"]
        assert not m["path"].startswith(prefix), (
            f"{m['name']}: path {m['path']!r} carries the local prefix {prefix!r}; "
            f"the repository has the file at {m['path'][len(prefix):]!r}")
        assert m["destination"] == prefix + m["path"], m["name"]
