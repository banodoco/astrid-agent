"""Static inventory of underscore dictionary writes across Astrid, beyond timelines.

Only _index and _timeline are derived timeline keys. Other static underscore
writes require an explicit key, file, and enclosing-symbol exception below;
exempting a whole file would hide a future timeline data ownership mistake.
This scans Python ASTs, never imports packs or touches project/runtime data.
It covers literal keys and string constant aliases, not arbitrary computed keys.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest


OWNED_DERIVED_KEYS = frozenset({"_index", "_timeline"})
ASTRID_ROOT = Path(__file__).resolve().parents[2] / "astrid"


@dataclass(frozen=True)
class Write:
    path: str
    symbol: str
    key: str
    line: int

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.path, self.symbol, self.key


class _UnderscoreWrites(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.constants: dict[str, str] = {}
        self.writes: list[Write] = []

    def visit_Module(self, node: ast.Module) -> None:
        # Resolve named module constants too: astrid_ns uses ns[LEGACY].
        for statement in node.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                value = statement.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    for target in targets:
                        if isinstance(target, ast.Name):
                            self.constants[target.id] = value.value
        self.generic_visit(node)

    def _key(self, node: ast.expr | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self.constants.get(node.id)
        return None

    def _record(self, key: str | None, node: ast.AST) -> None:
        if key is not None and key.startswith("_"):
            self.writes.append(Write(self.path, ".".join(self.scope) or "<module>", key, node.lineno))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_ClassDef = visit_FunctionDef

    def visit_Dict(self, node: ast.Dict) -> None:
        for key in node.keys:
            self._record(self._key(key), node)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Store):
            self._record(self._key(node.slice), node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        method = node.func.attr if isinstance(node.func, ast.Attribute) else None
        if method in {"setdefault", "__setitem__"} and node.args:
            self._record(self._key(node.args[0]), node)
        if method == "update" or isinstance(node.func, ast.Name) and node.func.id == "dict":
            for keyword in node.keywords:
                self._record(keyword.arg, node)
        self.generic_visit(node)


def _scan(source: str, path: str) -> list[Write]:
    visitor = _UnderscoreWrites(path)
    visitor.visit(ast.parse(source, filename=path))
    return visitor.writes


# Every exception is scoped to an exact file, symbol, and key. Reasons describe
# the distinct storage/protocol ownership; none expands timeline derived keys.
_EXCEPTIONS: dict[tuple[str, str, str], str] = {}


def _allow(path: str, symbol: str, keys: str, reason: str) -> None:
    assert reason.strip()
    for key in keys.split():
        identity = (path, symbol, key)
        assert identity not in _EXCEPTIONS
        _EXCEPTIONS[identity] = reason


_allow("core/timeline/astrid_ns.py", "_keep", "_legacy",
       "Namespaced migration collision escrow is preserved user intent, never derived data.")
_allow("core/timeline/shot_composition_migration.py", "plan_shot_composition_migration.register_unit", "_source_index",
       "Temporary migration sorting ordinal; popped before the migrated occurrences are returned.")
for _symbol in ("_cmd_list", "_cmd_inspect"):
    _allow("core/element/cli.py", _symbol, "_capability",
           "CLI element discovery response carries its capability descriptor, outside timeline storage.")
_allow("core/execution/generic_host_worker.py", "_run_child_command", "__main__",
       "Python sys.modules bookkeeping while executing a child command, not document data.")
_allow("core/execution/persistent_supervisor.py", "PersistentJsonlSupervisor.reclaim_orphans", "_reclaim",
       "Supervisor cancellation control frame marks orphan reclamation.")
_allow("core/execution/vibecomfy_engine_adapter.py", "SelectedCompositeObserver.__init__",
       "_process_start_identity _process_parent_pid",
       "Observer execution namespace binds native process identity helpers.")
_allow("core/execution/vibecomfy_engine_adapter.py", "install_selected_listener_boundary", "_spawn_comfy_server",
       "Engine adapter maps an internal callable name to its expected implementation digest.")
_allow("core/execution/vibecomfy_engine_adapter.py", "engine_main", "_cleanup_spawn_failure _stop_managed_process",
       "Engine adapter supplies callable digest pins for process cleanup verification.")
_allow("core/rendering/scaffold.py", "create_renderer_scaffold", "__PACK_ID__ __RENDERER_ID__ __DISPLAY_NAME__",
       "Template substitution tokens for generated source files, not timeline document keys.")
_allow("packs/h3_av/actions/verify/verify.py", "verify_candidate", "_evidence",
       "Verification request includes sampled evidence in its protected prompt context.")
_allow("packs/h3_av/workflows/lanpaint_h3_av_generalized/workflow.py", "build", "_un257 _un260",
       "Imported Comfy workflow node input socket identifiers belong to the external graph schema.")
_allow("packs/iteration/actions/assemble/run.py", "_materialize_runtime_artifact", "_resolution_error",
       "Artifact materialization diagnostic on an iteration assembly working record.")
for _symbol in ("main._run", "_synthesize_entry"):
    _allow("packs/iteration/actions/experiment_import/run.py", _symbol,
           "_source_dir _manifest _outcome _has_screenshots _media_count",
           "Experiment importer staging records retain local source and classification metadata.")
_allow("packs/iteration/actions/experiment_import/run.py", "_dedupe_to_cases", "_duplicates",
       "Experiment importer staging record tracks duplicate source directories.")
_allow("packs/rendering/actions/timeline_visualize/audio_analysis.py", "_empty_bin", "_sum_squares",
       "Private audio analysis accumulator, not serialized timeline intent.")
_allow("packs/rendering/actions/timeline_visualize/frozen.py", "_schema_registry", "_defs",
       "Frozen schema lookup table uses the schema definitions document's registry name.")
_allow("packs/rendering/rendering/renderers/remotion/run.py", "_inject_clip_asset_params", "__astridAssets",
       "Renderer execution copy supplies staged asset paths to Remotion; this is not authoring persistence.")
_allow("packs/runpod/worker_process.py", "remote_dispatch", "__worker_error__",
       "Worker RPC error envelope uses a fixed secret-free transport discriminator.")
_allow("packs/timeline/cli.py", "_apply_edit", "_allow_new",
       "Keyword control passed to clip.set authorizes new params; it is not a persisted clip key.")
_allow("packs/training/actions/dataset_build/phases.py", "_acquire_candidates", "_acquisition_results",
       "Training dataset acquisition working config carries provider receipts, outside timeline storage.")
_allow("sdk/__init__.py", "<module>", """
_apply_pack_permission_ids _build_discovery_metadata _capability_from_element
_capability_from_executor _capability_from_orchestrator _discover_pack_inventory
_element_kind_record _format_candidates _generation_backend_record _generation_feature_record
_generation_mode_record _load_element_registry _load_executor_registry _load_orchestrator_registry
_load_registries _pack_permission_ids_by_pack_id _pack_record _resolve_capability
_resolve_capability_kindless _resolve_element_capability _resolve_executor_capability
_resolve_orchestrator_capability _split_canonical_element_id _discover_invocation_manifest_path
_normalize_executor_result _normalize_orchestrator_result _payload_manifest_path
_EXPLICIT_ONLY_IMAGE_MODES _infer_image_mode _infer_video_mode _load_model_registry
_registry_load_kwargs _resolve_execution _json_safe _json_safe_mapping
_error_payload_from_internal_error _internal_error_from_result _sdk_error_from_event_exception
_sdk_error_from_exception
""", "SDK private Python symbol import registry, not persisted document fields.")
_allow("sdk/invocation.py", "invoke", "_recovery _recovery_metadata",
       "Invocation recovery control arguments belong to the execution kernel protocol.")
_allow("sdk/timeline/check.py", "_CheckMixin._native_outline_integrity", "_art_bytes",
       "Python instance __dict__ cache for integrity checks; never serialized as timeline data.")
_allow("sdk/timeline_sheet.py", "render_sheet", "_exported",
       "Python instance __dict__ caches exported sheet assets, outside persisted timeline data.")


def _violations(writes: list[Write]) -> list[Write]:
    return [write for write in writes
            if write.key not in OWNED_DERIVED_KEYS and write.identity not in _EXCEPTIONS]


def test_all_astrid_underscore_writes_have_explicit_ownership() -> None:
    sources = sorted(ASTRID_ROOT.rglob("*.py"))
    assert sources
    writes = [write for path in sources
              for write in _scan(path.read_text(encoding="utf-8"), path.relative_to(ASTRID_ROOT).as_posix())]
    violations = _violations(writes)
    assert not violations, "Unowned underscore writes require a scoped, reasoned exception:\n" + "\n".join(
        f"{write.path}:{write.line} {write.symbol}: {write.key}" for write in violations
    )
    observed = {write.identity for write in writes}
    assert not (_EXCEPTIONS.keys() - observed), "Remove obsolete underscore-write exceptions"
    assert OWNED_DERIVED_KEYS <= {write.key for write in writes}


@pytest.mark.parametrize("statement", [
    "value = {'_foreign': 1}",
    "value['_foreign'] = 1",
    "value['_foreign'] += 1",
    "value['_foreign']: int = 1",
    "value.update({'_foreign': 1})",
    "value.update(_foreign=1)",
    "value.setdefault('_foreign', 1)",
    "value.__setitem__('_foreign', 1)",
    "value = dict(_foreign=1)",
    "KEY = '_foreign'\nvalue[KEY] = 1",
])
def test_inventory_rejects_unowned_write_forms(statement: str) -> None:
    violations = _violations(_scan(statement, "sdk/timeline/doc.py"))
    assert [write.key for write in violations] == ["_foreign"]


def test_exception_does_not_allow_other_symbols_or_keys() -> None:
    path = "core/timeline/astrid_ns.py"
    assert not _violations(_scan("def _keep():\n    value['_legacy'] = {}", path))
    assert _violations(_scan("def _keep():\n    value['_new'] = {}", path))
    assert _violations(_scan("def other():\n    value['_legacy'] = {}", path))
    assert _violations(_scan("def _keep():\n    value['_legacy'] = {}", "new.py"))


def test_owned_derived_keys_and_reads_are_not_violations() -> None:
    writes = _scan("doc['_index'] = {}\nclip['_timeline'] = {}\nread = clip['_opaque']", "sample.py")
    assert {write.key for write in writes} == OWNED_DERIVED_KEYS
    assert not _violations(writes)
