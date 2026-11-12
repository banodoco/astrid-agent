"""Construct and run the cell's patched Qwen3 TTS models without weights.

Run in a CPU engine-deps venv, with ASTRID_QWENTTS_CHECKOUT pointing to a clone
of the recipe's comfyui-qwentts source. Only committed files at the declared pin
are copied; the engine helper applies and verifies the declared source patches.
The full upstream config snapshots are verified against the engine model lock.
Full-size constructors use meta; tiny random models run on CPU. Hub access is offline.
"""

import hashlib
import importlib.util
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
import types
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
    loader_source = subprocess.check_output(
        ["git", "-C", checkout, "show", f"{source.pin.revision}:AILab_QwenTTS.py"],
    )
    (target / "AILab_QwenTTS.py").write_bytes(loader_source)
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


def copy_locked_node_asset(destination, relative_path):
    recipe = load_recipe("comfyui", "linux-x86_64-cu130")
    models = load_model_lock(recipe.models_lock).models
    model = next(m for m in models
                 if m.repository == "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
                 and m.path == relative_path)
    source = Path(__file__).parent / "fixtures" / "qwen3-tts-node" / relative_path
    data = source.read_bytes()
    assert len(data) == model.bytes, relative_path
    assert hashlib.sha256(data).hexdigest() == model.sha256, relative_path
    target = destination / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


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


def tiny_talker_config(configs):
    payload = load_snapshot("speech-config.json", "config.json")
    talker = payload["talker_config"]
    predictor = talker["code_predictor_config"]
    # Keep the real nested architecture, including all three mRoPE axes and
    # multiple code groups. Six rotary channels give each axis one frequency.
    for config in (talker, predictor):
        config.update(hidden_size=6, intermediate_size=12, head_dim=6,
                      num_attention_heads=1, num_key_value_heads=1,
                      num_hidden_layers=1, vocab_size=8, num_code_groups=3)
        if "layer_types" in config:
            config["layer_types"] = config["layer_types"][:config["num_hidden_layers"]]
    talker.update(text_hidden_size=6, text_vocab_size=8)
    talker["rope_scaling"]["mrope_section"] = [1, 1, 1]
    return configs.Qwen3TTSConfig.from_dict(payload).talker_config


def tiny_tokenizer_config(configs):
    payload = load_snapshot("speech-tokenizer-config.json", "speech_tokenizer/config.json")
    encoder, decoder = payload["encoder_config"], payload["decoder_config"]
    for config in (encoder, decoder):
        config.update(hidden_size=6, intermediate_size=12, head_dim=6,
                      num_attention_heads=1, num_key_value_heads=1,
                      num_hidden_layers=1, codebook_size=4, num_quantizers=2,
                      vector_quantization_hidden_dimension=2)
    encoder.update(num_filters=2, codebook_dim=2, upsample_groups=6)
    # Preserve every convolution stage. The encoder has an additional stride-2
    # downsampler, while the decoder has two extra upsampling stages.
    encoder["upsampling_ratios"] = [2] * len(encoder["upsampling_ratios"])
    encoder["upsampling_ratios"][-1] *= 2
    decoder["upsample_rates"] = [2] * len(decoder["upsample_rates"])
    decoder["upsampling_ratios"] = [2] * len(decoder["upsampling_ratios"])
    decoder.update(latent_dim=6, codebook_dim=6,
                   decoder_dim=2 ** len(decoder["upsample_rates"]))
    stride = math.prod(decoder["upsample_rates"] + decoder["upsampling_ratios"])
    # The real config stores an explicit frame rate; scale it with the smaller
    # convolution strides so Mimi's extra downsampler still has stride two.
    encoder["_frame_rate"] = encoder["sampling_rate"] / stride
    payload.update(encoder_valid_num_quantizers=2, decode_upsample_rate=stride,
                   encode_downsample_rate=stride)
    return configs.Qwen3TTSTokenizerV2Config.from_dict(payload)


@pytest.mark.parametrize("component", ["talker", "code_predictor", "speech_tokenizer"])
def test_tiny_random_model_forward_on_cpu(node, component):
    configs, models, tokenizer_configs, tokenizer_models = node
    torch.manual_seed(0)
    with torch.no_grad():
        if component == "speech_tokenizer":
            config = tiny_tokenizer_config(tokenizer_configs)
            model = tokenizer_models.Qwen3TTSTokenizerV2Model(config).eval()
            frames = 2
            samples = frames * config.encode_downsample_rate
            waveform = torch.randn(1, samples)
            encoded = model.encode(waveform, padding_mask=torch.ones_like(waveform, dtype=torch.long))
            assert len(encoded.audio_codes) == 1
            codes = encoded.audio_codes[0]
            assert codes.shape == (frames, config.encoder_valid_num_quantizers)
            assert codes.dtype == torch.long
            assert ((codes >= 0) & (codes < config.encoder_config.codebook_size)).all()
            # Zero in the first codebook denotes padding to this decoder API.
            codes = torch.randint(1, config.decoder_config.codebook_size,
                                  (1, frames, config.decoder_config.num_quantizers))
            decoded = model.decode(codes).audio_values
            assert len(decoded) == 1
            # The vocoder's causal transposed convolutions crop both ends:
            # each decoder block maps L input samples to (L - 1) * rate.
            decoded_samples = frames * math.prod(config.decoder_config.upsampling_ratios)
            for rate in config.decoder_config.upsample_rates:
                decoded_samples = (decoded_samples - 1) * rate
            assert decoded[0].shape == (decoded_samples,)
            assert torch.isfinite(decoded[0]).all()
        else:
            config = tiny_talker_config(configs)
            if component == "talker":
                model = models.Qwen3TTSTalkerForConditionalGeneration(config).eval()
                prefill_kwargs = dict(trailing_text_hidden=torch.randn(1, 1, config.hidden_size),
                                      tts_pad_embed=torch.randn(1, 1, config.hidden_size))
            else:
                model = models.Qwen3TTSTalkerCodePredictorModelForConditionalGeneration(
                    config.code_predictor_config, config,
                ).eval()
                prefill_kwargs = {}
            embeddings = torch.randn(1, 2, config.hidden_size)
            prefill = model(inputs_embeds=embeddings, use_cache=True, **prefill_kwargs)
            assert prefill.logits.shape == (1, 2, model.config.vocab_size)
            assert torch.isfinite(prefill.logits).all()
            assert prefill.past_key_values.get_seq_length() == 2
            if component == "talker":
                step_kwargs = dict(past_hidden=prefill.past_hidden,
                                   generation_step=prefill.generation_step,
                                   trailing_text_hidden=prefill.trailing_text_hidden,
                                   tts_pad_embed=prefill.tts_pad_embed,
                                   subtalker_dosample=False)
            else:
                step_kwargs = dict(generation_steps=prefill.generation_steps)
            step = model(input_ids=torch.ones(1, 1, dtype=torch.long),
                         past_key_values=prefill.past_key_values, use_cache=True, **step_kwargs)
            assert step.logits.shape == (1, 1, model.config.vocab_size)
            assert torch.isfinite(step.logits).all()
            assert step.past_key_values.get_seq_length() == 3
        assert all(parameter.device.type == "cpu" for parameter in model.parameters())


def test_comfyui_node_loads_complete_tiny_checkpoint_on_cpu(node, tmp_path, monkeypatch):
    configs, models, tokenizer_configs, tokenizer_models = node
    model_id = "Qwen3-TTS-12Hz-0.6B-CustomVoice"
    model_dir = tmp_path / "models" / "TTS" / "Qwen3-TTS" / model_id
    model_dir.mkdir(parents=True)

    # Create only tiny random weights from the real model structure. The source
    # configs and tokenizer assets below remain lock-verified upstream files;
    # the model and speech-tokenizer configs are shrunk to fit these weights.
    torch.manual_seed(2468)
    model_payload = load_snapshot("speech-config.json", "config.json")
    model_payload["talker_config"] = tiny_talker_config(configs).to_dict()
    model_config = configs.Qwen3TTSConfig.from_dict(model_payload)
    random_model = models.Qwen3TTSForConditionalGeneration(model_config).eval()
    from safetensors.torch import save_file

    save_file({key: value.detach().cpu().contiguous()
               for key, value in random_model.state_dict().items()}, model_dir / "model.safetensors")
    model_config.save_pretrained(model_dir)
    saved_config_path = model_dir / "config.json"
    saved_config = json.loads(saved_config_path.read_text(encoding="utf-8"))
    # The upstream model config omits this optional section for CustomVoice;
    # the config class's default object adds it when serializing.
    saved_config.pop("speaker_encoder_config", None)
    saved_config_path.write_text(json.dumps(saved_config, indent=2) + "\n", encoding="utf-8")
    del random_model

    for path in ("generation_config.json", "merges.txt", "preprocessor_config.json",
                 "tokenizer_config.json", "vocab.json", "speech_tokenizer/configuration.json",
                 "speech_tokenizer/preprocessor_config.json"):
        copy_locked_node_asset(model_dir, path)
    # These are tiny versions of the hash-verified upstream configs. The
    # tokenizer's original-layout weights exercise Mimi's native key mapping.
    speech_config = tiny_tokenizer_config(tokenizer_configs)
    speech_dir = model_dir / "speech_tokenizer"
    speech_dir.mkdir(exist_ok=True)
    (speech_dir / "config.json").write_text(speech_config.to_json_string(), encoding="utf-8")
    shutil.copyfile(
        Path(__file__).parent / "fixtures" / "qwen3-tts-tiny" / "tokenizer" / "model.safetensors",
        speech_dir / "model.safetensors",
    )

    # The node's imports are the only ComfyUI integration boundary required by
    # _load_model; model discovery itself uses the ordinary models_dir layout.
    folder_paths = types.ModuleType("folder_paths")
    folder_paths.models_dir = str(tmp_path / "models")
    folder_paths.folder_names_and_paths = {}
    folder_paths.get_folder_paths = lambda _name: []
    comfy = types.ModuleType("comfy")
    model_management = types.ModuleType("comfy.model_management")
    comfy.model_management = model_management
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)
    monkeypatch.setitem(sys.modules, "comfy", comfy)
    monkeypatch.setitem(sys.modules, "comfy.model_management", model_management)

    node_root = Path(models.__file__).parents[3]
    spec = importlib.util.spec_from_file_location("qwentts_node_loader_test", node_root / "AILab_QwenTTS.py")
    assert spec and spec.loader
    node_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(node_module)
    monkeypatch.setattr(node_module, "_MODEL_CACHE", {})

    loaded = node_module._load_model("CustomVoice", "0.6B", "cpu", "fp32", "eager")
    assert loaded.device == torch.device("cpu")
    assert loaded.processor.tokenizer is not None
    assert type(loaded.processor).__name__ == "Qwen3TTSProcessor"
    assert loaded.model.speech_tokenizer is not None
    assert loaded.model.speech_tokenizer.feature_extractor is not None
    assert loaded.model.speech_tokenizer.model is not None
    assert loaded.model.generate_config
    assert all(parameter.device.type == "cpu" for parameter in loaded.model.parameters())
    assert all(parameter.device.type == "cpu"
               for parameter in loaded.model.speech_tokenizer.model.parameters())


@pytest.mark.parametrize("component", ["talker", "code_predictor"])
def test_flash_attention_dispatch_support_on_cpu(node, component, monkeypatch):
    configs, models, _, _ = node
    config = tiny_talker_config(configs)
    cls = (models.Qwen3TTSTalkerForConditionalGeneration if component == "talker"
           else models.Qwen3TTSTalkerCodePredictorModelForConditionalGeneration)
    # Exercise Transformers' real dispatch support check without requiring the
    # optional GPU kernel or executing attention.
    monkeypatch.setattr(cls, "_flash_attn_import_error", lambda *args, **kwargs: None)
    selected = config if component == "talker" else config.code_predictor_config
    with torch.device("meta"):
        model = cls(selected) if component == "talker" else cls(selected, config)
    selected._attn_implementation = "flash_attention_2"
    assert model._flash_attn_can_dispatch(2, is_init_check=True)
