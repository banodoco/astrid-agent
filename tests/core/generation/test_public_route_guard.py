"""Guard the public generation facade against routes that bypass its hub."""

from __future__ import annotations

import ast
import copy
from pathlib import Path


_PUBLIC_GENERATION_METHODS = {
    "audio": {"generation.generate_audio", "generation.generate_speech"},
    "image": {"generation.generate_image"},
    "video": {"generation.generate_video"},
    # Workflow is the typed workflow facade route, which uses the supplied
    # invocation client's invoke_result seam rather than a generation.* verb.
    "workflow": {"vibecomfy.run"},
}


def _facade_node(source: str) -> ast.ClassDef:
    tree = ast.parse(source)
    return next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GenerationFacade"
    )


def _public_methods(facade: ast.ClassDef) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {
        node.name: node
        for node in facade.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }


def _calls(method: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> list[ast.Call]:
    return [
        node for node in ast.walk(method)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == name)
            or (isinstance(node.func, ast.Name) and node.func.id == name)
        )
    ]


def _route_guard_errors(facade: ast.ClassDef) -> list[str]:
    methods = _public_methods(facade)
    errors: list[str] = []
    actual = set(methods)
    expected = set(_PUBLIC_GENERATION_METHODS)
    if actual != expected:
        errors.append(
            f"public GenerationFacade methods changed: "
            f"missing={sorted(expected - actual)}, added={sorted(actual - expected)}"
        )

    for method_name, route_ids in _PUBLIC_GENERATION_METHODS.items():
        method = methods.get(method_name)
        if method is None:
            continue
        if method_name == "workflow":
            route_calls = _calls(method, "invoke_result")
        else:
            route_calls = _calls(method, "_invoke")
        routed_ids = {
            arg.value
            for call in route_calls
            for arg in call.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        }
        if not route_ids.issubset(routed_ids):
            errors.append(
                f"{method_name} does not use its registered central route: "
                f"expected={sorted(route_ids)}, found={sorted(routed_ids)}"
            )

    getattr_method = next(
        (node for node in facade.body if isinstance(node, ast.FunctionDef) and node.name == "__getattr__"),
        None,
    )
    if getattr_method is None:
        errors.append("GenerationFacade.__getattr__ no longer delegates plugin verbs")
    else:
        registry_calls = {call.func.id for call in ast.walk(getattr_method)
                          if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}
        if not {"get_verb", "load_generation_verb_plugins"}.issubset(registry_calls):
            errors.append("GenerationFacade.__getattr__ bypasses the generation verb registry")
    return errors


def test_public_generation_routes_use_the_registered_central_paths() -> None:
    source_path = Path(__file__).resolve().parents[3] / "astrid" / "sdk" / "generation.py"
    facade = _facade_node(source_path.read_text(encoding="utf-8"))
    assert _route_guard_errors(facade) == []


def test_route_guard_rejects_a_planted_public_bypass() -> None:
    source_path = Path(__file__).resolve().parents[3] / "astrid" / "sdk" / "generation.py"
    facade = copy.deepcopy(_facade_node(source_path.read_text(encoding="utf-8")))
    facade.body.append(ast.parse(
        "def image_cloud(self, **kwargs):\n"
        "    import astrid.sdk as sdk\n"
        "    return sdk.invoke('generation.generate_image', **kwargs)\n"
    ).body[0])

    errors = _route_guard_errors(facade)
    assert len(errors) == 1
    assert "added=['image_cloud']" in errors[0]
