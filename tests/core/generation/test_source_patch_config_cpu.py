"""The declared Qwen3 TTS config patch loads under the engine lock's transformers, on CPU, with no weights.

Found live (Release 13 Stage 1): transformers 5 no longer supplies ``pad_token_id`` on
``PretrainedConfig``, and the node's model constructors read it from both bundled config classes.
"""

import hashlib
import importlib.machinery
import importlib.util
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.engine_deps

import transformers  # noqa: E402  (the engine_deps lane provides it)
from transformers import GenerationConfig  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
LOCK = ROOT / "astrid/packs/runpod/requirements.linux-cu130.lock"


def locked_transformers_version():
    match = re.search(r"^transformers==([^\s\\]+)", LOCK.read_text(encoding="utf-8"), re.M)
    assert match, "the engine lock pins transformers"
    return match.group(1)


def load_configuration():
    path = ROOT / "astrid/packs/runpod/patches/configuration_qwen3_tts.txt"
    loader = importlib.machinery.SourceFileLoader("patched_configuration", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("token", [None, 0, 7])
@pytest.mark.parametrize("snapshot", [False, True])
def test_embedding_padding_config(nested, token, snapshot):
    assert transformers.__version__ == locked_transformers_version()
    module = load_configuration()
    payload = {"talker_config": {}}
    if snapshot:
        data = (Path(__file__).parent / "fixtures/speech-config.json").read_bytes()
        assert hashlib.sha256(data).hexdigest() == (
            "81aca2b6fac304944d8acf345272d8a9a727d5fc2e2e66b222ab4729340c7455"
        )
        payload = json.loads(data)
    root = module.Qwen3TTSConfig.from_dict(payload)
    GenerationConfig.from_model_config(root)
    config = root.talker_config
    if nested:
        config = config.code_predictor_config
    assert config.pad_token_id is None
    configured = type(config).from_dict({"pad_token_id": token})
    assert configured.pad_token_id == token
    restored = type(config).from_dict(configured.to_dict())
    assert restored.pad_token_id == token
    assert GenerationConfig.from_model_config(restored).pad_token_id == token
