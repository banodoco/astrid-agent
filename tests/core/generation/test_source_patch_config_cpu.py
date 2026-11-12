"""Construct the cell's patched Qwen3 TTS models without downloading weights.

Run in a CPU engine-deps venv, with ASTRID_QWENTTS_CHECKOUT pointing to a clone
of the recipe's comfyui-qwentts source. Only committed files at the declared pin
are copied; the engine helper applies and verifies the declared source patches.
The full upstream config snapshots are verified against the engine model lock.
All model parameters stay on meta, and Hugging Face access is offline.
"""

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.engine_deps

import transformers  # noqa: E402  (the engine_deps lane provides it)
import torch  # noqa: E402
from huggingface_hub import constants as hub_constants  # noqa: E402
from transformers import GenerationConfig  # noqa: E402

from astrid.core.generation.engine_recipe import load_model_lock, load_recipe, recipe_digest
from astrid.core.generation.engine_runner import _HELPER_SCHEMA, _sha, helper_manifest
from astrid.core.generation.engine_runner_target import dispatch

ROOT = Path(__file__).resolve().parents[3]
LOCK = ROOT / "astrid/packs/runpod/requirements.linux-cu130.lock"


def locked_version(package):
    text = LOCK.read_text(encoding="utf-8")
    match = re.search(rf"^{package}==([^\s\\]+)", text, re.M)
    if match:
        return match.group(1)
    # torch is pinned as a wheel URL in this lock. A CPU wheel with the same
    # public version exercises the same constructors without CUDA libraries.
    match = re.search(rf"^{package} @ \S+/{package}-([^%+\s]+)(?:%2B|\+)", text, re.M)
    assert match, f"the engine lock pins {package}"
    return match.group(1)


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    assert transformers.__version__ == locked_version("transformers")
    assert torch.__version__.split("+", 1)[0] == locked_version("torch")
    recipe = load_recipe("comfyui", "linux-x86_64-cu130")
    source = next(s for s in recipe.sources if s.name == "comfyui-qwentts")
    checkout = os.environ.get("ASTRID_QWENTTS_CHECKOUT")
    assert checkout, "set ASTRID_QWENTTS_CHECKOUT to a clone of " + source.pin.repository
    revision = subprocess.check_output(
        ["git", "-C", checkout, "rev-parse", "HEAD"], text=True,
    ).strip()
    assert revision == source.pin.revision
    archive = subprocess.check_output(
        ["git", "-C", checkout, "archive", source.pin.revision, "qwen_tts"],
    )
    root = tmp_path_factory.mktemp("qwentts-engine")
    target = root / source.relative_path
    target.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as files:
        files.extractall(target, filter="data")
    payload = dict(schema=_HELPER_SCHEMA, engine=recipe.engine, platform=recipe.platform,
                   root=str(root), recipe_sha256=recipe_digest(recipe),
                   helper_sha256=_sha(helper_manifest(recipe)), source=source.name)
    dispatch("apply-source-patches", payload)
    dispatch("verify-source-patches", payload)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("HF_HUB_OFFLINE", "1")
        patch.setenv("TRANSFORMERS_OFFLINE", "1")
        patch.setattr(hub_constants, "HF_HUB_OFFLINE", True)
        patch.syspath_prepend(str(target))
        from qwen_tts.core.models import configuration_qwen3_tts, modeling_qwen3_tts
        from qwen_tts.core.tokenizer_12hz import (
            configuration_qwen3_tts_tokenizer_v2, modeling_qwen3_tts_tokenizer_v2,
        )

        assert Path(modeling_qwen3_tts.__file__).is_relative_to(target)
        try:
            yield (configuration_qwen3_tts, modeling_qwen3_tts,
                   configuration_qwen3_tts_tokenizer_v2, modeling_qwen3_tts_tokenizer_v2)
        finally:
            for name in list(sys.modules):
                if name == "qwen_tts" or name.startswith("qwen_tts."):
                    del sys.modules[name]


def load_snapshot(filename, model_path):
    recipe = load_recipe("comfyui", "linux-x86_64-cu130")
    models = load_model_lock(recipe.models_lock).models
    model = next(m for m in models if m.repository == "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
                 and m.path == model_path)
    data = (Path(__file__).parent / "fixtures" / filename).read_bytes()
    assert len(data) == model.bytes
    assert hashlib.sha256(data).hexdigest() == model.sha256
    return json.loads(data)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("token", [None, 0, 7])
@pytest.mark.parametrize("snapshot", [False, True])
def test_embedding_padding_config(node, nested, token, snapshot):
    module = node[0]
    payload = {"talker_config": {}}
    if snapshot:
        payload = load_snapshot("speech-config.json", "config.json")
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


@pytest.mark.parametrize("component", ["talker", "code_predictor", "tts", "speech_tokenizer"])
def test_real_model_constructors_on_meta(node, component):
    configs, models, tokenizer_configs, tokenizer_models = node
    config = configs.Qwen3TTSConfig.from_dict(load_snapshot("speech-config.json", "config.json"))
    with torch.device("meta"):
        if component == "talker":
            model = models.Qwen3TTSTalkerForConditionalGeneration(config.talker_config)
        elif component == "code_predictor":
            model = models.Qwen3TTSTalkerCodePredictorModelForConditionalGeneration(
                config.talker_config.code_predictor_config, config.talker_config,
            )
        elif component == "tts":
            model = models.Qwen3TTSForConditionalGeneration(config)
        else:
            tokenizer_config = tokenizer_configs.Qwen3TTSTokenizerV2Config.from_dict(
                load_snapshot("speech-tokenizer-config.json", "speech_tokenizer/config.json"),
            )
            model = tokenizer_models.Qwen3TTSTokenizerV2Model(tokenizer_config)
    parameters = list(model.parameters())
    assert parameters
    assert all(parameter.device.type == "meta" for parameter in parameters)
    if model.can_generate():
        assert model.generation_config is not None
