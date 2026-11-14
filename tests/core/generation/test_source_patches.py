import ast
import hashlib
from types import SimpleNamespace

import pytest

from astrid.core.generation import engine_recipe
from astrid.core.generation import engine_runner_target as target
from astrid.core.generation.engine_contracts import IntegrityError, SourcePatch


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def patch_context(tmp_path, monkeypatch):
    source = SimpleNamespace(name="fixture", relative_path="source")
    before, after = b"original\n", b"patched\n"
    patch = SourcePatch("fixture", "nested/file.py", digest(before), digest(after), "packs/test/file.py")
    recipe = SimpleNamespace(platform="linux-x86_64-cu130", sources=(source,), source_patches=(patch,))
    path = tmp_path / "source/nested/file.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(before)
    monkeypatch.setattr(target.recipes, "load_recipe", lambda *args: recipe)
    monkeypatch.setattr(target.recipes, "recipe_digest", lambda recipe: "recipe")
    monkeypatch.setattr(target, "helper_manifest", lambda recipe: {})
    monkeypatch.setattr(target.recipes, "_read_ref", lambda ref: (None, after))
    payload = {"schema": target._HELPER_SCHEMA, "engine": "fixture", "platform": recipe.platform,
               "root": str(tmp_path), "recipe_sha256": "recipe", "helper_sha256": target._sha({}),
               "source": "fixture"}
    return path, payload, before, after


def test_apply_and_check_declared_patch(patch_context):
    path, payload, before, after = patch_context
    assert target.dispatch("apply-source-patches", payload)["facts"] == {"changed": True}
    assert path.read_bytes() == after
    assert target.dispatch("apply-source-patches", payload)["facts"] == {"changed": False}
    check = {**payload, "entries": [" M nested/file.py"]}
    assert target.dispatch("declared-source-modifications", check)["facts"] == {"valid": True}
    assert target.dispatch("declared-source-modifications", {**check, "entries": [" M other.py"]})["facts"] == {"valid": False}
    path.write_bytes(b"hand edit")
    assert target.dispatch("declared-source-modifications", check)["facts"] == {"valid": False}
    with pytest.raises(IntegrityError, match="source patch digest mismatch") as caught:
        target.dispatch("apply-source-patches", payload)
    assert caught.value.reason == "source patch digest mismatch"
    assert path.read_bytes() == b"hand edit"
    path.write_bytes(before)


def test_real_replacement_uses_the_transformers_5_decorator():
    # 687a0ed90 shipped a shim that accepted check_model_inputs as a decorator or as a decorator
    # factory. bc43fe466 removed it on purpose: the engine is pinned to transformers 5
    # (def117751), whose check_model_inputs is a plain decorator, and the patch uses it directly.
    recipe = engine_recipe.load_recipe("comfyui", "linux-x86_64-cu130")
    patch = next(p for p in recipe.source_patches
                 if p.path == "qwen_tts/core/tokenizer_12hz/modeling_qwen3_tts_tokenizer_v2.py")
    source = (engine_recipe._ROOT / patch.replacement).read_text()
    assert digest(source.encode()) == patch.sha256_after
    tree = ast.parse(source)
    assert any(isinstance(node, ast.ImportFrom) and node.module == "transformers.utils.generic"
               and [alias.name for alias in node.names] == ["check_model_inputs"] for node in tree.body)
    assert not any(isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "_check_model_inputs" for t in node.targets)
                   for node in tree.body)
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "Qwen3TTSTokenizerV2DecoderTransformerModel")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                  and any(isinstance(d, ast.Name) and d.id == "check_model_inputs" for d in node.decorator_list))
    method.decorator_list = [d for d in method.decorator_list
                             if isinstance(d, ast.Name) and d.id == "check_model_inputs"]
    method.body = [ast.Pass()]
    method.args = ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[])
    method.returns = None
    cls = ast.ClassDef(name="Fixture", bases=[], keywords=[], body=[method], decorator_list=[])
    decorated = []

    def check_model_inputs(function):
        decorated.append(function.__name__)
        return function
    namespace = {"check_model_inputs": check_model_inputs}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[])), "<patch>", "exec"), namespace)
    assert hasattr(namespace["Fixture"], method.name) and decorated == [method.name]
    lock = (engine_recipe._ROOT / "packs/runpod/requirements.linux-cu130.lock").read_text()
    assert "\ntransformers==5." in lock
