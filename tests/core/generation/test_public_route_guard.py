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

# Temporary direct provider call sites, each tied to the Generate task that
# moves it behind the shared generation backend. Keep entries at individual
# function/call sites so adding another fal call to one of these files still
# fails this guard.
_PROVIDER_CALL_MIGRATIONS = {
    (
        "astrid/packs/fal/actions/h3_video/run.py",
        "execute",
        "fal_submit_and_poll",
    ): "G08",
    (
        "astrid/packs/fal/actions/fal_foley/run.py",
        "main",
        "fal_submit_and_poll",
    ): "G08",
    (
        "astrid/packs/seedance_local/actions/reference_video/run.py",
        "execute",
        "fal_submit_and_poll",
    ): "G08",
    (
        "astrid/packs/media/actions/speech_repair_lavasr/run.py",
        "_run_lavasr",
        "fal_client.upload_file",
    ): "G09",
    (
        "astrid/packs/media/actions/speech_repair_lavasr/run.py",
        "_run_lavasr",
        "fal_client.subscribe",
    ): "G09",
    (
        "astrid/packs/media/actions/speech_repair_lavasr/run.py",
        "_run_deepfilternet3",
        "fal_client.upload_file",
    ): "G09",
    (
        "astrid/packs/media/actions/speech_repair_lavasr/run.py",
        "_run_deepfilternet3",
        "fal_client.subscribe",
    ): "G09",
    (
        "astrid/packs/rendering/actions/sprite_sheet/upscale.py",
        "ai_upscale_frames_with_fal",
        "fal_client.SyncClient",
    ): "G09",
    (
        "astrid/packs/rendering/actions/sprite_sheet/upscale.py",
        "ai_upscale_frames_with_fal",
        "client.subscribe",
    ): "G09",
    (
        "astrid/packs/video_editing/actions/animate_image/run.py",
        "call_gpt_image_edit",
        "fal_submit_and_poll",
    ): "G09",
    (
        "astrid/packs/video_editing/actions/animate_image/run.py",
        "call_wan_animate_move",
        "fal_submit_and_poll",
    ): "G09",
    (
        "astrid/packs/video_editing/actions/logo_ideas/run.py",
        "render_concepts",
        "fal_submit_and_poll",
    ): "G09",
    (
        "astrid/packs/video_editing/actions/logo_ideas/run.py",
        "render_grid_image",
        "fal_submit_and_poll",
    ): "G09",
    (
        "astrid/packs/video_editing/actions/vary_grid/run.py",
        "call_fal_edit",
        "fal_submit_and_poll",
    ): "G09",
}

_APPROVED_PROVIDER_MODULES = {
    "astrid/core/generation/backends/fal.py",
    "astrid/core/util/http.py",
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


class _ProviderCallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.function_stack: list[str] = []
        self.sites: set[tuple[str, str]] = set()
        self.fal_client_names: set[str] = {"fal_client"}
        self.client_names: set[str] = set()

    @staticmethod
    def _callee(node: ast.expr) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = _ProviderCallVisitor._callee(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return None

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "fal_client":
                self.fal_client_names.add(alias.asname or "fal_client")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "fal_client":
            self.fal_client_names.update(alias.asname or alias.name for alias in node.names)

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._callee(node.value) in {f"{name}.SyncClient" for name in self.fal_client_names}:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.client_names.add(target.id)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        callee = self._callee(node.func)
        if callee:
            direct_fal = any(callee.startswith(f"{name}.") for name in self.fal_client_names)
            imported_fal = callee in self.fal_client_names and callee != "fal_client"
            fal_client_method = any(
                callee == f"{name}.subscribe" for name in self.client_names
            )
            if direct_fal or imported_fal or fal_client_method or callee == "fal_submit_and_poll":
                self.sites.add((self.function_stack[-1] if self.function_stack else "<module>", callee))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        # H3 and Seedance retain a submitter injection seam; the default is
        # still a direct dependency on fal_submit_and_poll and must be tracked.
        if node.id == "fal_submit_and_poll" and not isinstance(node.ctx, ast.Load):
            return
        parent = getattr(node, "parent", None)
        if node.id == "fal_submit_and_poll" and not isinstance(parent, (ast.Import, ast.ImportFrom)):
            if isinstance(node.ctx, ast.Load):
                self.sites.add((self.function_stack[-1] if self.function_stack else "<module>", node.id))


def _provider_call_sites(root: Path) -> set[tuple[str, str, str]]:
    package_root = root / "astrid"
    sites: set[tuple[str, str, str]] = set()
    for source_path in package_root.rglob("*.py"):
        relative = source_path.relative_to(root).as_posix()
        if relative in _APPROVED_PROVIDER_MODULES:
            continue
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=relative)
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                child.parent = node
        visitor = _ProviderCallVisitor()
        visitor.visit(tree)
        sites.update((relative, function, callee) for function, callee in visitor.sites)
    return sites


def _provider_call_guard_errors(root: Path) -> list[str]:
    errors = []
    for path, function, callee in sorted(_provider_call_sites(root)):
        site = (path, function, callee)
        if path in _APPROVED_PROVIDER_MODULES or site in _PROVIDER_CALL_MIGRATIONS:
            continue
        errors.append(f"unapproved fal provider call at {path}:{function}: {callee}")
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


def test_provider_call_guard_rejects_a_synthetic_fal_call(tmp_path: Path) -> None:
    root = tmp_path
    rogue = root / "astrid" / "packs" / "unapproved" / "run.py"
    rogue.parent.mkdir(parents=True)
    rogue.write_text(
        "import fal_client\n\n"
        "def run():\n"
        "    return fal_client.subscribe('fal-ai/example', arguments={})\n",
        encoding="utf-8",
    )

    errors = _provider_call_guard_errors(root)

    assert errors == [
        "unapproved fal provider call at astrid/packs/unapproved/run.py:run: fal_client.subscribe"
    ]


def test_provider_call_migrations_are_explicit_and_current_sites_are_covered() -> None:
    assert _provider_call_guard_errors(Path(__file__).resolve().parents[3]) == []
    assert _PROVIDER_CALL_MIGRATIONS
    for path, function, callee in _PROVIDER_CALL_MIGRATIONS:
        assert all("*" not in component for component in (path, function, callee))
        assert path.startswith("astrid/packs/")
        assert function and callee
        assert _PROVIDER_CALL_MIGRATIONS[(path, function, callee)] in {"G08", "G09"}
