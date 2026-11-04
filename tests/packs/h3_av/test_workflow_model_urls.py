"""Offline guard for model URLs embedded in portable H3 workflow bundles."""

import ast
import json
from pathlib import Path
import re

import pytest

from astrid.core.generation.engine_recipe import load_recipe_model_lock
from astrid.packs.h3_av.shared.generation import _required_locked_url


ROOT = Path(__file__).resolve().parents[3]
PACK_WORKFLOWS = ROOT / "astrid/packs/h3_av/workflows"
CHARACTER_SWAP_URL = (
    "https://huggingface.co/akatz-ai/MiniMax-H3-Character-Swap-LoRA/resolve/"
    "62407e0cc8089c363abd9ce4b0b27662abb237af/"
    "h3_character_swap_pro4500_1000.safetensors"
)
URL = re.compile(r"https?://[^\s\"'\\<>]+")
MODEL_FILE = re.compile(r"\.(?:safetensors|ckpt|pt|pth|bin|gguf)(?:\?|$)", re.I)


def _workflow_files():
    # Include sources and companion graphs as well as Python workflow payloads.
    roots = [PACK_WORKFLOWS, *(p for p in (ROOT / "workflows").glob("*h3*") if p.is_dir())]
    return sorted(p for root in roots for p in root.rglob("*")
                  if p.is_file() and p.suffix in {".py", ".json"})


def _declared_model_urls(value, inside_models=False):
    if isinstance(value, dict):
        for key, child in value.items():
            if inside_models and key == "url" and child is not None:
                assert isinstance(child, str)
                yield child
            else:
                yield from _declared_model_urls(child, inside_models or key in {"models", "model_assets"})
    elif isinstance(value, list):
        for child in value:
            yield from _declared_model_urls(child, inside_models)


def _python_model_urls(text, path):
    tree = ast.parse(text, filename=str(path))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and
                ((isinstance(node.func, ast.Name) and node.func.id == "ModelAsset") or
                 (isinstance(node.func, ast.Attribute) and node.func.attr == "ModelAsset"))):
            continue
        # ModelAsset(filename, url, subdir) also accepts its URL positionally.
        # Unpacking makes the URL impossible to inspect statically, so reject it.
        assert not any(isinstance(arg, ast.Starred) for arg in node.args), str(path)
        assert not any(keyword.arg is None for keyword in node.keywords), str(path)
        url_nodes = ([node.args[1]] if len(node.args) > 1 else []) + [
            keyword.value for keyword in node.keywords if keyword.arg == "url"
        ]
        for url_node in url_nodes:
            assert isinstance(url_node, ast.Constant) and isinstance(url_node.value, str), str(path)
            yield url_node.value


@pytest.mark.parametrize("source", [
    'ModelAsset("model", dynamic_url, "models")',
    'ModelAsset(url=dynamic_url, subdir="models")',
    'ModelAsset(*args)',
    'ModelAsset(**kwargs)',
])
def test_python_model_url_discovery_rejects_uninspectable_arguments(source):
    with pytest.raises(AssertionError):
        list(_python_model_urls(source, "workflow.py"))


def test_python_model_url_discovery_includes_positional_url():
    source = 'ModelAsset("model", "https://example.com/download", "models")'
    assert list(_python_model_urls(source, "workflow.py")) == ["https://example.com/download"]


def test_character_swap_revision_remains_pinned():
    from astrid.packs.h3_av.shared import generation

    assert (f"https://huggingface.co/{generation.CHARACTER_SWAP_REPOSITORY}/resolve/"
            f"{generation.CHARACTER_SWAP_REVISION}/{generation.CHARACTER_SWAP_FILENAME}") == CHARACTER_SWAP_URL


def test_h3_workflow_model_urls_match_lock():
    lock = load_recipe_model_lock("vibecomfy-h3", "linux-x86_64-cu130")
    models = {model.destination: model for model in lock.models}
    locked_urls = {_required_locked_url(models, destination) for destination in models}
    assert len(locked_urls) == 5
    allowed = locked_urls | {CHARACTER_SWAP_URL}
    found = set()
    files_with_models = {}

    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        assert "/resolve/main/" not in text, str(path)
        urls = set(URL.findall(text))
        model_urls = {url for url in urls if "huggingface.co/" in url
                      or "/resolve/" in url or MODEL_FILE.search(url)}
        if path.suffix == ".py":
            # Also catch a new ModelAsset URL on an unrelated host or without
            # a model-file extension, and reject dynamic URLs in the payload.
            model_urls.update(_python_model_urls(text, path))
        else:
            model_urls.update(_declared_model_urls(json.loads(text)))
        assert model_urls <= allowed, f"{path}: unexpected model URLs {model_urls - allowed}"
        if model_urls:
            files_with_models[path] = model_urls
            found.update(model_urls)

    # Avoid a vacuous pass if workflow discovery or asset declarations disappear.
    assert locked_urls <= found
    expected_workflows = [
        PACK_WORKFLOWS / name / "workflow.py"
        for name in ("native_h3_continuation", "native_h3_continuation_refs")
    ] + [
        ROOT / "workflows" / name / "workflow.py"
        for name in ("seitanism_h3_av_extension", "seitanism_h3_av_extension_fixed",
                     "seitanism_h3_av_extension_repaired", "seitanism_h3_av_extension_verified")
    ]
    for path in expected_workflows:
        assert locked_urls <= files_with_models.get(path, set()), str(path)
