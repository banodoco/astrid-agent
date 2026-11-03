"""Backend-neutral orchestration for one committed timeline render.

Every render is selected by its qualified registry id.  The service resolves
that id through the rendering registries and invokes it through protocol v1.
Backends write private artifacts in an invocation workspace; the service
validates them and performs exactly one locked publication at the end.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

from astrid.core.env_vars import ASTRID_INTERNAL_INVOCATION, ASTRID_PACKS_PATH, ASTRID_TASK_PROJECT, ASTRID_TASK_RUN_ID
from astrid.core.foundation.atomic_io import write_json_atomic
from astrid.core.foundation.hash import sha256_file
from astrid.core.foundation.project_paths import resolve_projects_root, run_dir
from astrid.core.media import ffprobe_metadata_strict

from .artifacts import validate_render_result
from .contracts import (
    SCHEMA_VERSION,
    AudioOwnership,
    FinalizeRequest,
    FinalizerResolution,
    FrameWindow,
    PlannerResolution,
    RendererResolution,
    RenderPlan,
    RenderRequest,
    RenderResult,
    RenderSegment,
    SupportReport,
    compute_request_digest,
)
from .delivery_audio import (
    FRAGMENT_NAMESPACE,
    METADATA_KEY,
    DeliveryAudioError,
    DeliveryAudioTarget,
    apply_delivery_audio,
    find_ffmpeg,
)
from .errors import (
    RendererException,
    raise_internal_error,
    raise_invalid_artifact_error,
    raise_protocol_error,
    raise_renderer_error,
    raise_unsupported_error,
)
from .output_policy import RenderOutputPolicyError, validate_render_output_policy
from .provenance import assemble_provenance_v2
from .publication import publish_render_result
from .registry import (
    FinalizerRegistry,
    PlannerRegistry,
    RendererRegistry,
    RenderingCandidate,
    RenderingRegistryError,
    load_default_registries,
)
from .replay import (
    BUNDLE_SCHEMA_VERSION,
    ReplayBundle,
    write_replay_bundle,
)
from .transport import CommandTransport
from .window_inputs import materialize_window_input

_QUALIFIED_ID_RE = re.compile(
    r"^[a-z0-9][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+$"
)
_CORE_BACKEND_ID = "astrid.core"
_DIRECT_PLANNER_ID = "astrid.direct"
_DIRECT_PLANNER_DIGEST = hashlib.sha256(b"astrid.direct/v1").hexdigest()
_DIRECT_FINALIZER_ID = "astrid.direct-finalizer"
_DIRECT_FINALIZER_DIGEST = hashlib.sha256(
    b"astrid.direct-finalizer/v1"
).hexdigest()

CapabilityKind = Literal["renderer", "planner", "finalizer"]
StageObserver = Callable[[str, Mapping[str, Any]], None]
AudioCompleter = Callable[..., RenderResult]


@contextmanager
def _pack_discovery_scope(extra_pack_roots: Sequence[str]) -> Any:
    """Carry explicit SDK pack roots into nested renderer processes."""
    roots = tuple(str(Path(root).expanduser().resolve()) for root in extra_pack_roots if root)
    if not roots:
        yield
        return
    previous = os.environ.get(ASTRID_PACKS_PATH)
    existing = tuple(item for item in (previous or "").split(os.pathsep) if item)
    merged: list[str] = []
    for root in (*roots, *existing):
        if root not in merged:
            merged.append(root)
    os.environ[ASTRID_PACKS_PATH] = os.pathsep.join(merged)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ASTRID_PACKS_PATH, None)
        else:
            os.environ[ASTRID_PACKS_PATH] = previous


@dataclass(frozen=True)
class _SelectionPolicy:
    requested: str
    kind: Literal["renderer", "planner"]
    targets: tuple[str, ...]


@dataclass(frozen=True)
class _ResolvedCapability:
    candidate: RenderingCandidate[Any]
    evidence: dict[str, Any]
    support: SupportReport
    rejected: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _InvocationContext:
    """The most recent backend command issued by :class:`RenderService`."""

    candidate: RenderingCandidate[Any]
    verb: str
    payload: Any
    request_path: Path
    result_path: Path
    workspace: Path


#: Backend verbs whose failures (and opt-in successes) produce replay bundles.
#: ``support`` is included because a support probe can itself fail with a
#: backend bug and must be replayable like any other invocation.
_REPLAY_VERBS = frozenset({"render", "finalize", "plan", "support"})


_CANONICAL_RENDERER_IDS = ("rendering.ffmpeg", "rendering.remotion", "rendering.threejs")


def _render_workspace_parent(output: Path) -> Path:
    """Keep managed render scratch outside the host output subtree.

    Generic-pack attempts expose an attempt/outputs directory as the declared
    output root. Intermediate frame/audio files belong to the attempt scratch
    budget, so managed output places its temporary workspace beside that
    directory. Direct callers retain historical output-parent placement.
    """

    output_parent = output.resolve(strict=False).parent
    if output_parent.name == "outputs" and output_parent.parent != output_parent:
        return output_parent.parent
    return output_parent



def _render_support_workspace_parent(output: Path) -> Path | None:
    """Confine managed support beside its host-owned output subtree.

    The output-derived attempt and inherited progress parent must agree.
    Standalone support retains its OS-temporary admission workspace.
    """
    inherited = os.environ.get("ASTRID_PROGRESS_PATH")
    if not inherited or os.environ.get(ASTRID_INTERNAL_INVOCATION) != "1":
        return None
    progress = Path(inherited)
    attempt = _render_workspace_parent(output)
    if (output.parent.name != "outputs"
            or not progress.is_absolute() or progress.name != ".astrid-progress.json"
            or progress.is_symlink() or not progress.parent.is_dir()
            or any(part.is_symlink() for part in (progress.parent, *progress.parent.parents))
            or any(part.is_symlink() for part in (output, *output.parents))
            or attempt != progress.parent.resolve()
            or not output.resolve(strict=False).is_relative_to(attempt)):
        # Retain the standalone admission route. The unchanged transport
        # rejects any inherited progress path that cannot contain its files.
        return None
    return attempt


def _select_capability(
    selector: str | None,
    registries: tuple[Any, Any, Any] | None = None,
) -> _SelectionPolicy:
    """Resolve one qualified renderer or planner id without translation."""

    selected = "rendering.remotion" if selector is None else selector
    if isinstance(selected, str) and _QUALIFIED_ID_RE.fullmatch(selected):
        if registries is not None:
            renderers, planners, _finalizers = registries
            try:
                renderers.get(selected)
                return _SelectionPolicy(selected, "renderer", (selected,))
            except Exception:
                pass
            try:
                planners.get(selected)
                return _SelectionPolicy(selected, "planner", (selected,))
            except Exception:
                pass
        return _SelectionPolicy(selected, "renderer", (selected,))
    raise_unsupported_error(
        backend=_CORE_BACKEND_ID,
        message=f"renderer selector must be a qualified capability id, got {selected!r}",
        recovery_command=(
            "select one canonical renderer id: " + ", ".join(_CANONICAL_RENDERER_IDS)
        ),
        details={
            "selector": selected if isinstance(selected, str) else repr(selected),
            "canonical_renderer_ids": list(_CANONICAL_RENDERER_IDS),
        },
    )


class RenderService:
    """Resolve, invoke, validate, finalize, and publish one timeline render.

    Registries and lifecycle functions are injectable so callers can embed the
    service without importing backend code, and so the orchestration order can
    be tested without spawning media tools.
    """

    def __init__(
        self,
        renderer_registry: RendererRegistry | None = None,
        planner_registry: PlannerRegistry | None = None,
        finalizer_registry: FinalizerRegistry | None = None,
        *,
        registries: tuple[
            RendererRegistry, PlannerRegistry, FinalizerRegistry
        ]
        | None = None,
        project_root: str | Path | None = None,
        extra_pack_roots: tuple[str, ...] = (),
        transport: Any | None = None,
        transport_factory: Callable[[str], Any] = CommandTransport,
        validator: Callable[..., RenderResult] = validate_render_result,
        publisher: Callable[..., Path] = publish_render_result,
        provenance_builder: Callable[..., dict[str, Any]] = assemble_provenance_v2,
        audio_completer: AudioCompleter | None = None,
        stage_observer: StageObserver | None = None,
        finalizer_id: str | None = None,
        replay_root: str | Path | None = None,
        capture_success: bool = False,
    ) -> None:
        supplied = (
            renderer_registry,
            planner_registry,
            finalizer_registry,
        )
        if registries is not None and any(item is not None for item in supplied):
            raise ValueError(
                "pass either registries= or individual rendering registries, not both"
            )
        if registries is None:
            if all(item is None for item in supplied):
                registries = load_default_registries(
                    project_root,
                    extra_pack_roots=extra_pack_roots,
                )
            elif any(item is None for item in supplied):
                raise ValueError("all three rendering registries must be supplied together")
            else:
                registries = supplied  # type: ignore[assignment]
        self.renderers, self.planners, self.finalizers = registries
        self.extra_pack_roots = tuple(str(root) for root in extra_pack_roots)
        self.renderer_registry = self.renderers
        self.planner_registry = self.planners
        self.finalizer_registry = self.finalizers
        self._transport = transport
        self._transport_factory = transport_factory
        self._validator = validator
        self._publisher = publisher
        self._provenance_builder = provenance_builder
        self._audio_completer = audio_completer
        self._stage_observer = stage_observer
        # Direct renders need no executable finalizer.  An embedding host may
        # nevertheless request a registered finalizer identity for direct-plan
        # provenance; otherwise a core no-op resolution is recorded.  Planned
        # renders always use the finalizer pinned by their RenderPlan.
        self.finalizer_id = finalizer_id
        # Replay-bundle capture hooks (additive).  Backend failures always
        # attempt a best-effort bundle; success capture is opt-in.
        self._replay_root = (
            Path(replay_root).expanduser() if replay_root is not None else None
        )
        self._capture_success = bool(capture_success)
        self._active_output: Path | None = None
        self._last_invocation: _InvocationContext | None = None
        #: Most recent successful request-sensitive support report per
        #: candidate id, captured so a later failing invocation's replay
        #: bundle can record the support evidence that preceded it.
        self._support_reports: dict[str, SupportReport] = {}

    def render(
        self,
        request: RenderRequest | Mapping[str, Any],
        *,
        selector: str | None = None,
        out_path: str | Path,
        sidecar_path: str | Path | None = None,
        previous_outputs: Iterable[object] = (),
    ) -> Path:
        """Render one canonical wire request and publish its result."""
        parsed = request if isinstance(request, RenderRequest) else RenderRequest.from_dict(request)
        return self.render_request(
            parsed,
            selector=selector,
            out_path=out_path,
            sidecar_path=sidecar_path,
            previous_outputs=previous_outputs,
        )

    def render_request(
        self,
        request: RenderRequest | Mapping[str, Any],
        *,
        selector: str | None = None,
        out_path: str | Path,
        sidecar_path: str | Path | None = None,
        previous_outputs: Iterable[object] = (),
    ) -> Path:
        """Execute the frozen selection lifecycle for one protocol request."""

        self._last_invocation = None
        # Support evidence is request-scoped.  Keeping a prior invocation's
        # report would make a later support/render failure appear to have
        # passed admission for the current request, producing misleading
        # replay metadata (especially when a backend fails during support).
        self._support_reports.clear()
        try:
            parsed = (
                request
                if isinstance(request, RenderRequest)
                else RenderRequest.from_dict(request)
            )
            from .verified_source import inherited_source

            if parsed.verified_source != inherited_source(parsed.timeline_path, parsed.assets_registry_path):
                raise ValueError("verified source authority is unavailable or replaced")
            if parsed.verified_source is not None:
                parsed.verified_source.validate(parsed.timeline_path, parsed.assets_registry_path)
            self._validate_output_name(parsed)
            localized = self._absolute_input_paths(parsed)
            # Keep the caller's absolute-but-unresolved spellings for the
            # publication layer's symlink guard.  The private workspace uses
            # the resolved parent so its final move stays on the destination
            # filesystem.
            output = Path(out_path).expanduser().absolute()
            sidecar = Path(
                sidecar_path or f"{output}.provenance.json"
            ).expanduser().absolute()
            if sidecar == output:
                raise_protocol_error(
                    backend=_CORE_BACKEND_ID,
                    message="video and provenance sidecar paths must be different",
                    recovery_command="choose a distinct .provenance.json sidecar path",
                    details={"path": str(output)},
                )
            policy = _select_capability(
                selector,
                registries=(
                    self.renderers,
                    self.planners,
                    self.finalizers,
                ),
            )
            self._observe("selection", selected=policy.requested, kind=policy.kind)
            # Request-sensitive support is a deterministic admission check.
            # Keep admission scratch beside a proven managed outputs root,
            # or in OS temporary storage for standalone calls, before creating
            # anything below the caller's output parent. Otherwise an unsupported
            # timeline can trigger replay capture and leave a
            # ``.<output>.replay`` tree even though no renderer was admitted.
            # Runtime/backend failures still use the output-local workspace
            # and retain their replay evidence through the inner try/finally.
            self._active_output = output
            with _pack_discovery_scope(self.extra_pack_roots):
                with TemporaryDirectory(
                    prefix="astrid-render-support-",
                    dir=_render_support_workspace_parent(output),
                ) as support_text:
                    try:
                        preselected = self._select(
                            localized,
                            policy=policy,
                            workspace=Path(support_text),
                        )
                    except RendererException as exc:
                        # Support is a real backend invocation.  It runs in
                        # the admission workspace before the render workspace
                        # exists, so capture its failure here rather than
                        # letting the outer validation handler lose the
                        # invocation context.
                        self._capture_failure_bundle(exc, request=localized)
                        raise

            workspace_parent = _render_workspace_parent(output)
            workspace_parent.mkdir(parents=True, exist_ok=True)
            with TemporaryDirectory(
                prefix=f".{output.name}.render-service-",
                dir=str(workspace_parent),
            ) as workspace_text:
                workspace = Path(workspace_text)
                try:
                    with _pack_discovery_scope(self.extra_pack_roots):
                        published = self._render_in_workspace(
                            localized,
                            policy=policy,
                            selected=preselected,
                            workspace=workspace,
                            out_path=output,
                            sidecar_path=sidecar,
                            previous_outputs=tuple(previous_outputs),
                        )
                except RendererException as exc:
                    self._capture_failure_bundle(exc, request=localized)
                    raise
                if self._capture_success:
                    self._capture_success_bundle(request=localized)
                return published
        except RendererException as exc:
            if exc.error.recovery_command is None:
                raise_renderer_error(
                    replace(
                        exc.error,
                        recovery_command=self._default_error_recovery(
                            exc.error.kind
                        ),
                    )
                )
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except (TypeError, ValueError) as exc:
            raise_protocol_error(
                backend=_CORE_BACKEND_ID,
                message=f"render service received invalid data: {exc}",
                details={"error_type": type(exc).__name__},
            )
        except BaseException as exc:
            raise_internal_error(
                backend=_CORE_BACKEND_ID,
                message=f"render service failed: {exc or type(exc).__name__}",
                recovery_command="retry the render in a fresh invocation workspace",
                details={"error_type": type(exc).__name__},
            )

    @staticmethod
    def _validate_output_name(request: RenderRequest) -> None:
        """Apply the shared output/alpha policy before creating a workspace."""
        try:
            validate_render_output_policy(
                request.output_name,
                timeline=request.timeline_path,
                profile=request.profile,
            )
        except RenderOutputPolicyError as exc:
            raise_protocol_error(
                backend=_CORE_BACKEND_ID,
                message=str(exc),
                recovery_command=exc.recovery_command,
                details=exc.details,
            )

    @staticmethod
    def _absolute_input_paths(request: RenderRequest) -> RenderRequest:
        timeline = Path(request.timeline_path).expanduser()
        assets = (
            None
            if request.assets_registry_path is None
            else Path(request.assets_registry_path).expanduser()
        )
        return replace(
            request,
            timeline_path=str(timeline.resolve(strict=False)),
            assets_registry_path=(
                None if assets is None else str(assets.resolve(strict=False))
            ),
        )

    def _render_in_workspace(
        self,
        request: RenderRequest,
        *,
        policy: _SelectionPolicy,
        selected: _ResolvedCapability | None = None,
        workspace: Path,
        out_path: Path,
        sidecar_path: Path,
        previous_outputs: tuple[object, ...],
    ) -> Path:
        selected = selected or self._select(request, policy=policy, workspace=workspace)
        if policy.kind == "planner":
            plan, segment_results, pinned_finalizer = self._execute_planner(
                request,
                policy=policy,
                selected=selected,
                workspace=workspace,
            )
            if not segment_results:
                raise_unsupported_error(
                    backend=selected.candidate.id,
                    message="render planner produced no video segments",
                    recovery_command="use a non-empty timeline or select a direct renderer",
                    details={"total_frames": plan.total_frames},
                )
            final_result, plan = self._finish_plan(
                request,
                plan=plan,
                segment_results=segment_results,
                pinned_finalizer=pinned_finalizer,
                workspace=workspace,
            )
            finalizer_ran = plan.finalizer.id != _DIRECT_FINALIZER_ID
            artifact_lineage = [item.video for item in segment_results]
            fragment_results = (
                ([*segment_results, final_result] if finalizer_ran else segment_results)
                if len(segment_results) == 1
                else [*segment_results, final_result]
            )
        else:
            final_result = self._invoke_renderer(
                request,
                selected=selected,
                workspace=workspace,
                output_name=request.output_name,
                expected_profile=request.profile,
            )
            plan = self._direct_plan(
                request,
                selected=selected,
                result=final_result,
                requested_policy=policy.requested,
            )
            renderer_result = final_result
            final_result = self.complete_audio(
                final_result,
                request=request,
                plan=plan,
                workspace=workspace,
                backend=selected.candidate.id,
                # The direct plan may pin an executable finalizer; defer
                # completion to it so a normalizable profile/audio mismatch
                # is normalized before publication.
                defer_to_finalizer=(
                    plan.finalizer.id != _DIRECT_FINALIZER_ID
                ),
            )
            finalizer_ran = plan.finalizer.id != _DIRECT_FINALIZER_ID
            if finalizer_ran:
                # An embedding host pinned a registered finalizer for direct
                # renders; honor it exactly like planner-produced plans.
                finalizer, finalizer_evidence = self._resolve_candidate(
                    self.finalizers,
                    plan.finalizer.id,
                    kind="finalizer",
                    observe=False,
                )
                final_result, plan = self._finish_plan(
                    request,
                    plan=plan,
                    segment_results=[renderer_result],
                    pinned_finalizer=(finalizer, finalizer_evidence),
                    workspace=workspace,
                )
            elif final_result.video.profile != plan.profile or (
                final_result.video.duration_frames
                != (
                    plan.window.duration_frames
                    if plan.window is not None
                    else plan.total_frames
                )
            ):
                plan = self._direct_plan(
                    request,
                    selected=selected,
                    result=final_result,
                    requested_policy=policy.requested,
            )
            artifact_lineage = [renderer_result.video]
            fragment_results = (
                [renderer_result, final_result]
                if finalizer_ran
                else [final_result]
            )

        source_video = self._artifact_path(final_result, workspace)
        fragments = self._merge_backend_fragments(fragment_results)
        delivery_target = self._delivery_audio_target(request)
        if delivery_target is not None:
            source_video, delivery_receipt = self._master_delivery_audio(
                final_result, source_video, workspace=workspace,
                target=delivery_target, backend=selected.candidate.id,
            )
            fragments[FRAGMENT_NAMESPACE] = delivery_receipt
        provenance = self._provenance_builder(
            engine=policy.requested,
            output=out_path,
            timeline=request.timeline_path,
            assets_registry=request.assets_registry_path,
            plan=plan,
            artifact_profiles=artifact_lineage,
            audio_ownership=final_result.audio_ownership,
            normalization=final_result.normalization,
            attachments=final_result.attachments,
            backend_fragments=fragments,
        )
        self._observe(
            "publish",
            backend=(
                plan.planner.id if policy.kind == "planner" else selected.candidate.id
            ),
            output=str(out_path),
            sidecar=str(sidecar_path),
        )
        published = self._publisher(
            source_video,
            provenance,
            out_path=out_path,
            sidecar_path=sidecar_path,
            previous_outputs=previous_outputs,
        )
        return Path(published)

    def _select(
        self,
        request: RenderRequest,
        *,
        policy: _SelectionPolicy,
        workspace: Path,
    ) -> _ResolvedCapability:
        registry: RendererRegistry | PlannerRegistry = (
            self.renderers if policy.kind == "renderer" else self.planners
        )
        rejected: list[dict[str, Any]] = []
        target = policy.targets[0]
        candidate, evidence = self._resolve_candidate(
            registry,
            target,
            kind=policy.kind,
        )
        report = self._support(
            candidate,
            request=request,
            workspace=workspace,
            registry=registry,
        )
        if not report.supported:
            rejected.append(report.to_dict())
            self._unsupported_report(report, registry=registry)
        return _ResolvedCapability(
            candidate,
            evidence,
            report,
            rejected=list(rejected),
        )

        alternatives = self._alternatives(registry)
        raise_unsupported_error(
            backend=(policy.targets[-1] if policy.targets else _CORE_BACKEND_ID),
            message=f"renderer {policy.requested!r} does not support this request",
            recovery_command=self._recovery_for(alternatives),
            details={"attempts": rejected, "alternatives": alternatives},
        )

    def _resolve_candidate(
        self,
        registry: RendererRegistry | PlannerRegistry | FinalizerRegistry,
        requested_id: str,
        *,
        kind: CapabilityKind,
        observe: bool = True,
    ) -> tuple[RenderingCandidate[Any], dict[str, Any]]:
        try:
            candidate = registry.get(requested_id)
            evidence = registry.resolve_evidence(requested_id)
        except RenderingRegistryError as exc:
            evidence: dict[str, Any] = {}
            try:
                evidence = registry.resolve_evidence(requested_id)
            except RenderingRegistryError:
                evidence = dict(exc.details)
            if observe:
                self._observe_resolution(requested_id, evidence, candidate=None)
            alternatives = self._alternatives(registry)
            details = {
                "registry_error": exc.to_dict(),
                "alternatives": alternatives,
            }
            raise_unsupported_error(
                backend=(
                    requested_id
                    if _QUALIFIED_ID_RE.fullmatch(requested_id)
                    else _CORE_BACKEND_ID
                ),
                message=str(exc),
                recovery_command=self._recovery_for(alternatives),
                details=details,
            )
        if observe:
            self._observe_resolution(requested_id, evidence, candidate=candidate)
        if (
            evidence.get("resolved_id") != candidate.id
            or evidence.get("manifest_digest") != candidate.manifest_digest
            or evidence.get("priority_index", evidence.get("priority"))
            != candidate.priority_index
        ):
            raise_internal_error(
                backend=_CORE_BACKEND_ID,
                message=(
                    f"{kind} registry changed while resolving {requested_id!r}"
                ),
                recovery_command="retry after renderer registry updates have completed",
                details={
                    "requested_id": requested_id,
                    "candidate": candidate.to_dict(),
                    "resolution_evidence": evidence,
                },
            )
        if not candidate.execution_eligible:
            alternatives = self._alternatives(registry)
            raise_unsupported_error(
                backend=candidate.id,
                message=f"{kind} {candidate.id!r} is not execution-eligible",
                recovery_command=self._recovery_for(alternatives),
                details={
                    "eligibility": candidate.eligibility.to_dict(),
                    "alternatives": alternatives,
                },
            )
        return candidate, evidence

    def _observe_resolution(
        self,
        requested_id: str,
        evidence: Mapping[str, Any],
        *,
        candidate: RenderingCandidate[Any] | None,
    ) -> None:
        alias_chain = list(evidence.get("alias_chain") or [])
        self._observe(
            "alias",
            requested_id=requested_id,
            canonical_id=evidence.get("canonical_id", requested_id),
            alias_chain=alias_chain,
        )
        self._observe(
            "override",
            requested_id=requested_id,
            override=evidence.get("override"),
        )
        self._observe(
            "winner",
            requested_id=requested_id,
            resolved_id=(
                candidate.id if candidate is not None else evidence.get("resolved_id")
            ),
            priority=evidence.get("priority_index", evidence.get("priority")),
        )
        eligibility = (
            candidate.eligibility.to_dict()
            if candidate is not None
            else evidence.get("eligibility", {})
        )
        self._observe(
            "eligibility",
            requested_id=requested_id,
            eligible=(
                candidate.execution_eligible
                if candidate is not None
                else evidence.get("execution_eligible", evidence.get("eligible", False))
            ),
            evidence=eligibility,
        )

    def _support(
        self,
        candidate: RenderingCandidate[Any],
        *,
        request: RenderRequest,
        workspace: Path,
        registry: RendererRegistry | PlannerRegistry | FinalizerRegistry,
    ) -> SupportReport:
        manifest = candidate.manifest
        projected = request.for_backend(candidate.id)
        self._observe("support", backend=candidate.id)
        if "support" in manifest.operations:
            response = self._run_command(
                candidate,
                "support",
                projected,
                workspace=workspace,
                required_binaries=(),
            )
            if not isinstance(response, SupportReport):
                raise_protocol_error(
                    backend=candidate.id,
                    message="support operation did not return a SupportReport",
                    details={"received_type": type(response).__name__},
                )
            if response.backend != candidate.id:
                raise_protocol_error(
                    backend=candidate.id,
                    message="support report names a different backend",
                    details={"reported_backend": response.backend},
                )
            if response.backend_version != candidate.manifest.version:
                raise_protocol_error(
                    backend=candidate.id,
                    message="support report version does not match its manifest",
                    recovery_command="update the backend command and manifest as one versioned unit",
                    details={
                        "reported_version": response.backend_version,
                        "manifest_version": candidate.manifest.version,
                    },
                )
            # Retain the evidence for replay bundles captured later in this
            # service's lifetime (a render/finalize/plan failure records the
            # support report that preceded it).
            self._support_reports[candidate.id] = response
            return response
        return self._static_support(candidate, projected, registry=registry)

    def _static_support(
        self,
        candidate: RenderingCandidate[Any],
        request: RenderRequest,
        *,
        registry: RendererRegistry | PlannerRegistry | FinalizerRegistry,
    ) -> SupportReport:
        capabilities = candidate.manifest.capabilities
        reasons: list[str] = []
        if isinstance(registry, RendererRegistry):
            support_key = (
                "supports_windows"
                if request.window is not None
                else "supports_full_timeline"
            )
            if capabilities.get(support_key) is not True:
                mode = "frame windows" if request.window is not None else "full timelines"
                reasons.append(
                    f"renderer does not declare static support for {mode}"
                )

            ownership = capabilities.get("audio_ownership")
            if request.audio is not None:
                if not isinstance(ownership, list):
                    reasons.append(
                        "renderer does not declare static audio ownership support"
                    )
                elif request.audio.value not in ownership:
                    reasons.append(
                        f"audio ownership {request.audio.value!r} is not statically supported"
                    )

            if request.profile is not None:
                profiles = capabilities.get("output_profiles")
                expected_profiles = {
                    request.profile.container,
                    f"video/{request.profile.container}",
                }
                if not isinstance(profiles, list):
                    reasons.append("renderer does not declare static output profiles")
                elif expected_profiles.isdisjoint(profiles):
                    reasons.append(
                        f"output container {request.profile.container!r} is not statically supported"
                    )

            reasons.extend(self._static_timeline_reasons(capabilities, request))
        elif isinstance(registry, PlannerRegistry):
            if not capabilities:
                reasons.append("planner does not declare static capability evidence")
        else:
            containers = capabilities.get("containers")
            if request.profile is not None:
                if not isinstance(containers, list):
                    reasons.append("finalizer does not declare static containers")
                elif request.profile.container not in containers:
                    reasons.append(
                        f"output container {request.profile.container!r} is not statically supported"
                    )
            ownership = capabilities.get("audio_ownership")
            if request.audio is not None:
                if not isinstance(ownership, list):
                    reasons.append(
                        "finalizer does not declare static audio ownership support"
                    )
                elif request.audio.value not in ownership:
                    reasons.append(
                        f"audio ownership {request.audio.value!r} is not statically supported"
                    )
            if capabilities.get("preserves_attachments") is not True:
                reasons.append("finalizer does not declare attachment preservation")
        alternatives = self._alternatives(registry, exclude=candidate.id) if reasons else []
        return SupportReport(
            schema_version=SCHEMA_VERSION,
            supported=not reasons,
            reasons=reasons,
            features={
                str(key): value
                for key, value in capabilities.get("features", {}).items()
                if isinstance(value, (bool, str))
            },
            alternatives=alternatives,
            backend=candidate.id,
            backend_version=candidate.manifest.version,
        )

    @staticmethod
    def _static_timeline_reasons(
        capabilities: Mapping[str, Any], request: RenderRequest
    ) -> list[str]:
        """Compare coarse renderer declarations with the concrete timeline.

        A renderer without a ``support`` verb has only its manifest as
        evidence, so omitted declarations are unknown and therefore fail
        closed when the request actually exercises them.
        """

        try:
            payload = json.loads(Path(request.timeline_path).read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise TypeError("timeline must contain a JSON object")
            raw_clips = payload.get("clips", [])
            raw_tracks = payload.get("tracks", [])
            if not isinstance(raw_clips, list) or not isinstance(raw_tracks, list):
                raise TypeError("timeline clips and tracks must be arrays")
            clip_types = {
                str(item.get("clipType", "media"))
                for item in raw_clips
                if isinstance(item, Mapping)
            }
            track_types = {
                str(item.get("kind"))
                for item in raw_tracks
                if isinstance(item, Mapping) and item.get("kind") is not None
            }
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            return [f"timeline cannot be evaluated against static support: {exc}"]

        reasons: list[str] = []
        for index, clip in enumerate(raw_clips):
            if not isinstance(clip, Mapping):
                continue
            clip_type = clip.get("clipType")
            text = clip.get("text")
            if isinstance(text, Mapping) and clip_type != "text":
                reasons.append(
                    f"clips[{index}] contains structured text but is missing clipType: 'text'"
                )
            elif clip_type == "text":
                if not isinstance(text, Mapping):
                    reasons.append(
                        f"clips[{index}].text must be an object for clipType: 'text'"
                    )
                elif not isinstance(text.get("content"), str):
                    reasons.append(
                        f"clips[{index}].text.content must be a string for clipType: 'text'"
                    )
        declared_clips = capabilities.get("clip_types")
        if clip_types:
            if not isinstance(declared_clips, list):
                reasons.append("renderer does not declare static clip types")
            else:
                missing = sorted(clip_types - set(declared_clips))
                if missing:
                    reasons.append(
                        "timeline uses statically unsupported clip types: "
                        + ", ".join(missing)
                    )
        declared_tracks = capabilities.get("track_types")
        if track_types:
            if not isinstance(declared_tracks, list):
                reasons.append("renderer does not declare static track types")
            else:
                missing = sorted(track_types - set(declared_tracks))
                if missing:
                    reasons.append(
                        "timeline uses statically unsupported track types: "
                        + ", ".join(missing)
                    )
        return reasons

    def _unsupported_report(
        self,
        report: SupportReport,
        *,
        registry: RendererRegistry | PlannerRegistry | FinalizerRegistry,
    ) -> None:
        alternatives = list(report.alternatives) or self._alternatives(
            registry, exclude=report.backend
        )
        raise_unsupported_error(
            backend=report.backend,
            message=f"{report.backend} does not support this render request",
            recovery_command=self._recovery_for(alternatives),
            details={
                "reasons": list(report.reasons),
                "features": dict(report.features),
                "alternatives": alternatives,
            },
        )

    def _invoke_renderer(
        self,
        request: RenderRequest,
        *,
        selected: _ResolvedCapability,
        workspace: Path,
        output_name: str,
        expected_profile: Any,
    ) -> RenderResult:
        backend_request = replace(request, output_name=output_name).for_backend(
            selected.candidate.id
        )
        self._observe("invoke", backend=selected.candidate.id, verb="render")
        response = self._run_command(
            selected.candidate,
            "render",
            backend_request,
            workspace=workspace,
        )
        if not isinstance(response, RenderResult):
            raise_protocol_error(
                backend=selected.candidate.id,
                message="render operation did not return a RenderResult",
                details={"received_type": type(response).__name__},
            )
        # A null request profile deliberately leaves the backend's output
        # profile open (the DTO contract permits this).  Validation still
        # recomputes hashes, probes the media, and checks the probe against the
        # declared profile.  Planned renders are subsequently checked or
        # normalized against their canonical plan profile in _finish_plan.
        expected = expected_profile or response.video.profile
        self._observe("validate", backend=selected.candidate.id)
        return self._validator(
            response,
            expected_profile=expected,
            workspace_root=workspace,
        )

    def _segment_request(
        self,
        request: RenderRequest,
        *,
        candidate: RenderingCandidate[Any],
        segment: RenderSegment,
        index: int,
        workspace: Path,
    ) -> tuple[RenderRequest, dict[str, str]]:
        """Adapt a planned window for full-timeline-only renderers.

        Window-aware renderers retain the canonical ``window`` and clip clocks.
        LayerRef track isolation and alpha stamps remain host-owned. A renderer
        that explicitly declares
        ``supports_windows: false`` receives an invocation-private sliced
        timeline and a null window, preserving the behavior of Astrid's
        existing full-timeline backends without teaching the service any
        concrete backend identities.
        """

        return materialize_window_input(
            request,
            candidate=candidate,
            segment=segment,
            index=index,
            workspace=workspace,
            slice_timeline=self._window_timeline,
        )

    @classmethod
    def _window_timeline(
        cls,
        timeline_data: Mapping[str, Any],
        window: FrameWindow,
        *,
        tracks: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        fps = Fraction(*window.fps_rational)
        start = Fraction(window.start_frame, 1) / fps
        end = Fraction(window.end_frame, 1) / fps
        raw_clips = timeline_data.get("clips", [])
        raw_tracks = timeline_data.get("tracks", [])
        if not isinstance(raw_clips, list) or not isinstance(raw_tracks, list):
            raise ValueError("timeline clips and tracks must be arrays")

        # Optional track allowlist (one z-layer's visual tracks): the host
        # slice keeps ONLY those tracks AND only their clips, so a layer
        # renderer never sees other layers' material.  None preserves today's
        # prune-to-used-tracks behavior byte-for-byte.
        allowlist = set(tracks) if tracks is not None else None

        clips: list[dict[str, Any]] = []
        for raw_clip in raw_clips:
            if not isinstance(raw_clip, Mapping):
                raise TypeError("timeline clips must contain objects")
            if allowlist is not None and raw_clip.get("track") not in allowlist:
                continue
            clipped = cls._window_clip(raw_clip, start=start, end=end, window=window)
            if clipped is not None:
                clips.append(clipped)
        # An allowlisted track survives even when it has no clips in the
        # window: the renderer must know its layer exists (it renders the
        # background/transparent fill for that span).
        used_tracks = (
            allowlist if allowlist is not None else {clip.get("track") for clip in clips}
        )
        tracks_out = [
            dict(track)
            for track in raw_tracks
            if isinstance(track, Mapping) and track.get("id") in used_tracks
        ]
        metadata = timeline_data.get("metadata", {})
        if not isinstance(metadata, Mapping):
            metadata = {}
        result = dict(timeline_data)
        result["tracks"] = tracks_out
        result["clips"] = clips
        result["metadata"] = {
            **dict(metadata),
            "source_window_start_seconds": float(start),
            "source_window_end_seconds": float(end),
            "duration_seconds": float(end - start),
        }
        return result

    @classmethod
    def _window_clip(
        cls,
        clip: Mapping[str, Any],
        *,
        start: Fraction,
        end: Fraction,
        window: FrameWindow,
    ) -> dict[str, Any] | None:
        clip_start = cls._timeline_number(clip.get("at", 0), "clip.at")
        clip_end = cls._clip_end(clip, clip_start=clip_start)
        visible_start = max(clip_start, start)
        visible_end = min(clip_end, end)
        if visible_end <= visible_start:
            return None

        result = dict(clip)
        # The slice rewrites at/from/to, so a fade would restart at the cut.
        # Record the original source phase (host-owned; overwrites any
        # authored value) for the audio envelope to translate against.
        params = clip.get("params")
        app = clip.get("app")
        if isinstance(app, Mapping) and "astrid_slice_origin" in app:
            result["app"] = {k: v for k, v in app.items() if k != "astrid_slice_origin"}
        if isinstance(params, Mapping) and (params.get("fadeIn") or params.get("fadeOut")):
            result["app"] = {
                **(result["app"] if isinstance(result.get("app"), Mapping) else {}),
                "astrid_slice_origin": {
                    "id": clip.get("id"),
                    "at": clip.get("at", 0),
                    "from": clip.get("from", 0),
                    "to": clip.get("to"),
                },
            }
        result["at"] = float(visible_start - start)
        result["id"] = (
            f"{clip.get('id', 'clip')}_{window.start_frame}_{window.end_frame}"
        )
        if clip.get("clipType", "media") == "media" or (
            isinstance(clip.get("app"), Mapping) and "liveScene" in clip["app"]
        ):
            speed = cls._timeline_number(clip.get("speed", 1), "clip.speed")
            if speed <= 0:
                raise ValueError("clip.speed must be positive")
            source_from = cls._timeline_number(clip.get("from", 0), "clip.from")
            source_from += (visible_start - clip_start) * speed
            result["from"] = float(source_from)
            result["to"] = float(
                source_from + (visible_end - visible_start) * speed
            )
            if clip.get("clipType", "media") != "media" and "hold" in result:
                result["hold"] = float((visible_end - visible_start) * speed)
        elif isinstance(clip.get("hold"), (int, float)) and not isinstance(
            clip.get("hold"), bool
        ):
            result["hold"] = float(visible_end - visible_start)
        return result

    @classmethod
    def _clip_end(
        cls, clip: Mapping[str, Any], *, clip_start: Fraction
    ) -> Fraction:
        if clip.get("clipType", "media") == "media" or (
            isinstance(clip.get("app"), Mapping) and "liveScene" in clip["app"]
        ):
            if clip.get("clipType", "media") != "media" and isinstance(clip.get("hold"), (int, float)):
                speed = cls._timeline_number(clip.get("speed", 1), "clip.speed")
                if speed <= 0:
                    raise ValueError("clip.speed must be positive")
                return clip_start + cls._timeline_number(clip["hold"], "clip.hold") / speed
            source_from = cls._timeline_number(clip.get("from", 0), "clip.from")
            if "to" not in clip:
                raise ValueError("media clip must declare a source to bound")
            source_to = cls._timeline_number(clip["to"], "clip.to")
            speed = cls._timeline_number(clip.get("speed", 1), "clip.speed")
            if source_from < 0 or source_to <= source_from or speed <= 0:
                raise ValueError("media clip must have positive bounds and speed")
            return clip_start + (source_to - source_from) / speed
        hold = clip.get("hold")
        if isinstance(hold, (int, float)) and not isinstance(hold, bool):
            return clip_start + max(Fraction(0), cls._timeline_number(hold, "clip.hold"))
        if isinstance(clip.get("to"), (int, float)) and not isinstance(
            clip.get("to"), bool
        ):
            return cls._timeline_number(clip["to"], "clip.to")
        return clip_start

    @staticmethod
    def _timeline_number(value: Any, label: str) -> Fraction:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{label} must be a finite number")
        if not math.isfinite(float(value)):
            raise ValueError(f"{label} must be finite")
        return Fraction(str(value))

    @staticmethod
    def _validate_segment_duration(
        result: RenderResult,
        *,
        segment: RenderSegment,
        canonical_profile: Any,
        backend: str,
        workspace: Path,
    ) -> None:
        # Frame-count authority: a backend may declare a duration derived
        # from an audio-extended container (Remotion's always-rendered audio
        # pads past the last video frame), which inflates duration_frames.
        # The video stream's ACTUAL frame count is authoritative; probe it
        # and validate against the plan window with the same tolerance the
        # finalizer uses, so a correct segment is not rejected on container
        # padding alone.
        actual = result.video.duration_frames
        try:
            video_path = Path(result.video.path)
            if not video_path.is_absolute():
                video_path = workspace / video_path
            if video_path.is_file():
                probe = ffprobe_metadata_strict(video_path)
                if probe.frames is not None and probe.frames > 0:
                    actual = probe.frames
        except Exception:  # noqa: BLE001 - probe is best-effort
            pass
        RenderService._validate_planned_duration(
            RenderService._with_duration_frames(result, actual),
            planned_frames=segment.window.duration_frames,
            canonical_profile=canonical_profile,
            backend=backend,
            label="renderer artifact",
        )

    @staticmethod
    def _with_duration_frames(result: RenderResult, frames: int) -> RenderResult:
        """Return a copy of *result* whose video duration is *frames*."""
        if result.video.duration_frames == frames:
            return result
        from dataclasses import replace

        return replace(
            result,
            video=replace(result.video, duration_frames=frames),
        )

    @staticmethod
    def _validate_planned_duration(
        result: RenderResult,
        *,
        planned_frames: int,
        canonical_profile: Any,
        backend: str,
        label: str,
    ) -> None:
        artifact_seconds = Fraction(
            result.video.duration_frames, 1
        ) / Fraction(*result.video.profile.fps_rational)
        canonical_fps = Fraction(*canonical_profile.fps_rational)
        planned_seconds = Fraction(planned_frames, 1) / canonical_fps
        delta_frames = abs(artifact_seconds - planned_seconds) * canonical_fps
        if delta_frames <= canonical_profile.duration_tolerance:
            return
        raise_invalid_artifact_error(
            backend=backend,
            message=f"{label} duration does not match its planned frame window",
            recovery_command="rerender the exact planned segment window and retry",
            details={
                "planned_duration_frames": planned_frames,
                "artifact_duration_frames": result.video.duration_frames,
                "canonical_delta_frames": [
                    delta_frames.numerator,
                    delta_frames.denominator,
                ],
                "tolerance_frames": canonical_profile.duration_tolerance,
            },
        )

    def _execute_planner(
        self,
        request: RenderRequest,
        *,
        policy: _SelectionPolicy,
        selected: _ResolvedCapability,
        workspace: Path,
    ) -> tuple[
        RenderPlan,
        list[RenderResult],
        tuple[RenderingCandidate[Any], dict[str, Any]],
    ]:
        planner_request = request.for_backend(selected.candidate.id)
        self._observe("invoke", backend=selected.candidate.id, verb="plan")
        response = self._run_command(
            selected.candidate,
            "plan",
            planner_request,
            workspace=workspace,
        )
        if not isinstance(response, RenderPlan):
            raise_protocol_error(
                backend=selected.candidate.id,
                message="plan operation did not return a RenderPlan",
                details={"received_type": type(response).__name__},
            )
        # The registry selection is authoritative.  A planner response may
        # still carry the requested identity it was asked to replace; normalize that
        # self-description to the selected candidate and its complete
        # resolution evidence below.
        planner_resolution = self._planner_resolution(selected)
        normalized_segments: list[RenderSegment] = []
        segment_results: list[RenderResult] = []
        input_hashes = self._input_hashes(request)
        for index, segment in enumerate(response.segments):
            candidate, evidence = self._resolve_candidate(
                self.renderers,
                segment.renderer.id,
                kind="renderer",
            )
            # The planner already resolved aliases/overrides itself and
            # recorded that lineage on the segment.  Re-resolving the emitted
            # id from scratch would discard the alias chain, so merge: the
            # service's resolution is authoritative for identity/trust while
            # the planner's recorded lineage survives when present.
            planner_renderer = segment.renderer
            native_request = replace(
                request,
                window=segment.window,
                output_name=f"segment-{index:04d}.mp4",
            )
            segment_request, materialized_hashes = self._segment_request(
                native_request,
                candidate=candidate,
                segment=segment,
                index=index,
                workspace=workspace,
            )
            report = self._support(
                candidate,
                request=segment_request,
                workspace=workspace,
                registry=self.renderers,
            )
            if not report.supported:
                self._unsupported_report(report, registry=self.renderers)
            resolved = _ResolvedCapability(candidate, evidence, report)
            merged_renderer = replace(
                planner_renderer,
                id=candidate.id,
                source_pack=self._source_pack(candidate, evidence),
                manifest_digest=candidate.manifest_digest,
                trust_eligibility=candidate.eligibility.to_dict(),
                alias_chain=(
                    planner_renderer.alias_chain
                    or list(evidence.get("alias_chain") or [])
                ),
                override=planner_renderer.override or evidence.get("override"),
                support_decision=report,
            )
            normalized_segment = replace(
                segment,
                renderer=merged_renderer,
                input_hashes={
                    **segment.input_hashes,
                    **input_hashes,
                    **materialized_hashes,
                },
            )
            normalized_segments.append(normalized_segment)
            result = self._invoke_renderer(
                segment_request,
                selected=resolved,
                workspace=workspace,
                output_name=segment_request.output_name,
                # Segment renderers may emit a profile that the registered
                # finalizer must normalize.  The artifact is first validated
                # against its own declaration; a one-segment exact match is
                # checked against the plan in _finish_plan, while every
                # mismatch and every multi-segment plan goes through the
                # pinned finalizer.
                expected_profile=None,
            )
            completed = self.complete_audio(
                result,
                request=segment_request,
                plan=response,
                workspace=workspace,
                backend=candidate.id,
                # The plan pins an explicit finalizer; segment audio is
                # deferred to it (single- and multi-segment alike) so a
                # normalizable profile/audio mismatch cannot fail the segment
                # before the finalizer can normalize it.
                defer_to_finalizer=response.finalizer.id != _DIRECT_FINALIZER_ID,
            )
            self._validate_segment_duration(
                completed,
                segment=segment,
                canonical_profile=response.profile,
                backend=candidate.id,
                workspace=workspace,
            )
            segment_results.append(completed)

        finalizer, finalizer_evidence = self._resolve_candidate(
            self.finalizers,
            response.finalizer.id,
            kind="finalizer",
            observe=False,
        )
        finalizer_resolution = replace(
            response.finalizer,
            id=finalizer.id,
            source_pack=self._source_pack(finalizer, finalizer_evidence),
            manifest_digest=finalizer.manifest_digest,
            trust_eligibility=finalizer.eligibility.to_dict(),
            alias_chain=(
                response.finalizer.alias_chain
                or list(finalizer_evidence.get("alias_chain") or [])
            ),
            override=response.finalizer.override or finalizer_evidence.get("override"),
            # The planner's finalizer support_decision names its pre-alias
            # identity; _finish_plan re-evaluates support for the resolved
            # finalizer and records the authoritative decision.
            support_decision=None,
        )
        plan = replace(
            response,
            request_digest=compute_request_digest(request.to_dict()),
            requested_policy=policy.requested,
            planner=planner_resolution,
            segments=normalized_segments,
            finalizer=finalizer_resolution,
        )
        return plan, segment_results, (finalizer, finalizer_evidence)

    def _finish_plan(
        self,
        request: RenderRequest,
        *,
        plan: RenderPlan,
        segment_results: list[RenderResult],
        pinned_finalizer: tuple[RenderingCandidate[Any], dict[str, Any]],
        workspace: Path,
    ) -> tuple[RenderResult, RenderPlan]:
        candidate, evidence = pinned_finalizer
        if candidate.id == _DIRECT_FINALIZER_ID:
            # No executable finalizer pinned: the segment must already match
            # the canonical plan profile exactly.
            if len(segment_results) != 1:
                raise_internal_error(
                    backend=_CORE_BACKEND_ID,
                    message="direct finalizer received multiple segments",
                    recovery_command="select a planner that pins an executable finalizer",
                    details={"segment_count": len(segment_results)},
                )
            result = self._validator(
                segment_results[0],
                expected_profile=plan.profile,
                workspace_root=workspace,
            )
            return result, plan

        ownerships = {item.audio_ownership for item in segment_results}
        if ownerships == {AudioOwnership.PASSTHROUGH}:
            requested_audio = AudioOwnership.PASSTHROUGH
        elif plan.profile.has_audio:
            requested_audio = AudioOwnership.RENDERED
        else:
            requested_audio = AudioOwnership.NONE
        support_audio = (
            None
            if requested_audio is AudioOwnership.PASSTHROUGH
            and plan.profile.has_audio
            else requested_audio
        )
        support_request = RenderRequest(
            schema_version=SCHEMA_VERSION,
            timeline_path=request.timeline_path,
            assets_registry_path=request.assets_registry_path,
            output_name=request.output_name,
            audio=support_audio,
            profile=plan.profile,
            backend_config=request.backend_config,
            metadata=request.metadata,
        )
        report = self._support(
            candidate,
            request=support_request,
            workspace=workspace,
            registry=self.finalizers,
        )
        if not report.supported:
            self._unsupported_report(report, registry=self.finalizers)
        prior_finalizer = plan.finalizer
        finalizer_resolution = replace(
            self._finalizer_resolution(
                candidate,
                evidence,
                support=report,
            ),
            alias_chain=(
                prior_finalizer.alias_chain
                or list(evidence.get("alias_chain") or [])
            ),
            override=prior_finalizer.override or evidence.get("override"),
        )
        plan = replace(plan, finalizer=finalizer_resolution)
        finalize_request = FinalizeRequest(
            schema_version=SCHEMA_VERSION,
            plan=plan,
            artifacts=[item.video for item in segment_results],
            output_name=request.output_name,
            backend_config={
                candidate.id: dict(request.backend_config.get(candidate.id, {}))
            }
            if candidate.id in request.backend_config
            else {},
            metadata=request.metadata,
        )
        self._observe("finalize", backend=candidate.id)
        response = self._run_command(
            candidate,
            "finalize",
            finalize_request,
            workspace=workspace,
        )
        if not isinstance(response, RenderResult):
            raise_protocol_error(
                backend=candidate.id,
                message="finalize operation did not return a RenderResult",
                details={"received_type": type(response).__name__},
            )
        try:
            response = finalize_request.validate_final_result(response)
        except (TypeError, ValueError) as exc:
            raise_invalid_artifact_error(
                backend=candidate.id,
                message=f"finalizer returned an invalid result: {exc}",
                recovery_command="rerun finalization in a fresh invocation workspace",
                details={"error_type": type(exc).__name__},
            )
        self._observe("validate", backend=candidate.id)
        # The finalizer validates its stream-copied concat internally with
        # frame-count authority (see _validate_concat_output); the strict
        # probe-based validator would reject the output on the concat's
        # AAC-grid timestamp rounding alone (avg fps 9.98 vs 10/1).
        self._validate_planned_duration(
            response,
            planned_frames=(
                plan.window.duration_frames
                if plan.window is not None
                else plan.total_frames
            ),
            canonical_profile=plan.profile,
            backend=candidate.id,
            label="finalized artifact",
        )
        completed = self.complete_audio(
            response,
            request=request,
            plan=plan,
            workspace=workspace,
            backend=candidate.id,
        )
        self._validate_planned_duration(
            completed,
            planned_frames=(
                plan.window.duration_frames
                if plan.window is not None
                else plan.total_frames
            ),
            canonical_profile=plan.profile,
            backend=candidate.id,
            label="audio-completed artifact",
        )
        return completed, plan

    def complete_audio(
        self,
        result: RenderResult,
        *,
        request: RenderRequest,
        plan: RenderPlan,
        workspace: Path,
        backend: str = _CORE_BACKEND_ID,
        defer_to_finalizer: bool = False,
    ) -> RenderResult:
        """Apply host-owned completion semantics after renderer validation.

        ``rendered`` is already complete. ``none`` is an intentional
        visual-only result, while ``passthrough`` must be completed by the
        embedding host before publication.  A configured completer may also
        apply its declared policy to ``none`` without requiring
        arbitrary renderers to synthesize silence.
        """

        self._observe("audio", ownership=result.audio_ownership.value)
        if result.audio_ownership is AudioOwnership.RENDERED:
            return result
        if result.video.profile.has_audio:
            raise_invalid_artifact_error(
                backend=backend,
                message=(
                    f"audio_ownership={result.audio_ownership.value!r} requires "
                    "a visual-only renderer artifact"
                ),
                recovery_command="rerender with an audio/profile pair that agrees",
            )
        if defer_to_finalizer:
            # A registered finalizer owns cross-segment normalization: it may
            # synthesize silence for NONE segments or preserve a uniform set
            # of PASSTHROUGH segments.  Completion, if still necessary, runs
            # once on the finalized result below.
            return result
        if (
            result.audio_ownership is AudioOwnership.NONE
            and (
                plan.profile.has_audio
                or (
                    request.profile is not None
                    and request.profile.has_audio
                )
            )
        ):
            raise_invalid_artifact_error(
                backend=backend,
                message="audio_ownership='none' cannot satisfy a requested audio profile",
                recovery_command="request passthrough/rendered audio or a visual-only profile",
            )
        if self._audio_completer is None:
            if result.audio_ownership is AudioOwnership.PASSTHROUGH:
                raise_unsupported_error(
                    backend=backend,
                    message=(
                        "renderer requested passthrough audio but no host audio "
                        "completer is configured"
                    ),
                    recovery_command=(
                        "configure an audio completer or select a renderer that "
                        "returns rendered audio"
                    ),
                    details={"audio_ownership": AudioOwnership.PASSTHROUGH.value},
                )
            return result
        completed = self._audio_completer(
            result,
            request=request,
            plan=plan,
            workspace=workspace,
        )
        if not isinstance(completed, RenderResult):
            raise_protocol_error(
                backend=_CORE_BACKEND_ID,
                message="audio completer did not return a RenderResult",
                details={"received_type": type(completed).__name__},
            )
        if (
            completed.audio_ownership is AudioOwnership.PASSTHROUGH
            or (
                result.audio_ownership is AudioOwnership.PASSTHROUGH
                and completed.audio_ownership is not AudioOwnership.RENDERED
            )
        ):
            raise_invalid_artifact_error(
                backend=backend,
                message="host audio completer left passthrough audio incomplete",
                recovery_command="return a completed rendered-audio result",
                details={"audio_ownership": AudioOwnership.PASSTHROUGH.value},
            )
        missing_attachments = sorted(
            set(result.attachments) - set(completed.attachments)
        )
        changed_attachments = sorted(
            name
            for name, attachment in result.attachments.items()
            if name in completed.attachments
            and completed.attachments[name] != attachment
        )
        if missing_attachments or changed_attachments:
            raise_invalid_artifact_error(
                backend=backend,
                message="host audio completion did not preserve renderer attachments",
                recovery_command="preserve every named attachment while completing audio",
                details={
                    "missing": missing_attachments,
                    "changed": changed_attachments,
                },
            )
        original_profile = result.video.profile.to_dict()
        completed_profile = completed.video.profile.to_dict()
        audio_fields = {
            "audio_codec",
            "audio_sample_rate",
            "audio_channel_layout",
        }
        changed_video_fields = sorted(
            key
            for key, value in original_profile.items()
            if key not in audio_fields and completed_profile.get(key) != value
        )
        if (
            changed_video_fields
            or completed.video.duration_frames != result.video.duration_frames
        ):
            raise_invalid_artifact_error(
                backend=backend,
                message="host audio completion changed the renderer's video contract",
                recovery_command="complete audio without changing video profile or duration",
                details={
                    "changed_profile_fields": changed_video_fields,
                    "before_duration_frames": result.video.duration_frames,
                    "after_duration_frames": completed.video.duration_frames,
                },
            )
        return self._validator(
            completed,
            expected_profile=completed.video.profile,
            workspace_root=workspace,
        )

    def _direct_plan(
        self,
        request: RenderRequest,
        *,
        selected: _ResolvedCapability,
        result: RenderResult,
        requested_policy: str,
    ) -> RenderPlan:
        finalizer_resolution = self._direct_finalizer_resolution()
        reasons: dict[str, str] = {"0": "direct renderer selection"}
        if selected.rejected:
            # Preserve rejected support evidence for the selected capability.
            reasons["0"] = (
                "direct renderer selection; rejected candidates: "
                + json.dumps(selected.rejected, sort_keys=True)
            )
        if request.window is not None:
            if request.window.fps_rational != result.video.profile.fps_rational:
                raise_invalid_artifact_error(
                    backend=selected.candidate.id,
                    message="renderer artifact FPS does not match the requested frame window",
                    recovery_command="render the requested window at its declared rational FPS",
                    details={
                        "window_fps": list(request.window.fps_rational),
                        "artifact_fps": list(result.video.profile.fps_rational),
                    },
                )
            segment_window = request.window
            total_frames = request.window.end_frame
            plan_window = request.window
            self._validate_planned_duration(
                result,
                planned_frames=request.window.duration_frames,
                canonical_profile=result.video.profile,
                backend=selected.candidate.id,
                label="renderer artifact",
            )
        else:
            segment_window = FrameWindow(
                start_frame=0,
                end_frame=result.video.duration_frames,
                fps_rational=result.video.profile.fps_rational,
            )
            total_frames = result.video.duration_frames
            plan_window = None
        segment = RenderSegment(
            window=segment_window,
            renderer=self._renderer_resolution(selected),
            input_hashes=self._input_hashes(request),
        )
        return RenderPlan(
            schema_version=SCHEMA_VERSION,
            request_digest=compute_request_digest(request.to_dict()),
            requested_policy=requested_policy,
            planner=PlannerResolution(
                id=_DIRECT_PLANNER_ID,
                source_pack={"id": _CORE_BACKEND_ID, "source_kind": "core"},
                manifest_digest=_DIRECT_PLANNER_DIGEST,
                trust_eligibility={"eligible": True, "reason": "core direct plan"},
            ),
            segments=[segment],
            finalizer=finalizer_resolution,
            profile=result.video.profile,
            total_frames=total_frames,
            reasons=reasons,
            window=plan_window,
        )

    def _direct_finalizer_resolution(self) -> FinalizerResolution:
        if self.finalizer_id is not None:
            candidate, evidence = self._resolve_candidate(
                self.finalizers,
                self.finalizer_id,
                kind="finalizer",
                observe=False,
            )
            return self._finalizer_resolution(candidate, evidence, support=None)
        return FinalizerResolution(
            id=_DIRECT_FINALIZER_ID,
            source_pack={"id": _CORE_BACKEND_ID, "source_kind": "core"},
            manifest_digest=_DIRECT_FINALIZER_DIGEST,
            trust_eligibility={"eligible": True, "reason": "core direct pass-through"},
        )

    @staticmethod
    def _source_pack(
        candidate: RenderingCandidate[Any], evidence: Mapping[str, Any]
    ) -> dict[str, Any]:
        source = {
            "id": candidate.pack_id,
            "source_kind": candidate.source_kind,
            "root": str(candidate.pack_root),
            "priority_index": candidate.priority_index,
        }
        manifest_path = evidence.get("manifest_path")
        if isinstance(manifest_path, str):
            source["manifest_path"] = manifest_path
        return source

    def _renderer_resolution(
        self, selected: _ResolvedCapability
    ) -> RendererResolution:
        candidate = selected.candidate
        evidence = selected.evidence
        return RendererResolution(
            id=candidate.id,
            source_pack=self._source_pack(candidate, evidence),
            manifest_digest=candidate.manifest_digest,
            alias_chain=list(evidence.get("alias_chain") or []),
            override=evidence.get("override"),
            support_decision=selected.support,
            trust_eligibility=candidate.eligibility.to_dict(),
        )

    def _planner_resolution(
        self, selected: _ResolvedCapability
    ) -> PlannerResolution:
        candidate = selected.candidate
        evidence = selected.evidence
        return PlannerResolution(
            id=candidate.id,
            source_pack=self._source_pack(candidate, evidence),
            manifest_digest=candidate.manifest_digest,
            trust_eligibility=candidate.eligibility.to_dict(),
            alias_chain=list(evidence.get("alias_chain") or []),
            override=evidence.get("override"),
            support_decision=selected.support,
        )

    def _finalizer_resolution(
        self,
        candidate: RenderingCandidate[Any],
        evidence: Mapping[str, Any],
        *,
        support: SupportReport | None,
    ) -> FinalizerResolution:
        return FinalizerResolution(
            id=candidate.id,
            source_pack=self._source_pack(candidate, evidence),
            manifest_digest=candidate.manifest_digest,
            trust_eligibility=candidate.eligibility.to_dict(),
            alias_chain=list(evidence.get("alias_chain") or []),
            override=evidence.get("override"),
            support_decision=support,
        )

    def _run_command(
        self,
        candidate: RenderingCandidate[Any],
        verb: str,
        payload: Any,
        *,
        workspace: Path,
        required_binaries: Sequence[str] | None = None,
    ) -> Any:
        token = hashlib.sha256(
            f"{candidate.id}:{verb}:{len(list(workspace.iterdir()))}".encode()
        ).hexdigest()[:12]
        request_path = workspace / f"{token}-{verb}-request.json"
        result_path = workspace / f"{token}-{verb}-result.json"
        write_json_atomic(request_path, payload.to_dict())
        transport = (
            self._transport
            if self._transport is not None
            else self._transport_factory(candidate.id)
        )
        self._last_invocation = _InvocationContext(
            candidate=candidate,
            verb=verb,
            payload=payload,
            request_path=request_path,
            result_path=result_path,
            workspace=workspace,
        )
        return transport.run(
            verb,
            candidate.manifest.command,
            backend=candidate.id,
            request_path=request_path,
            result_path=result_path,
            cwd=candidate.pack_root,
            timeout=candidate.manifest.timeout_seconds,
            required_binaries=(
                candidate.manifest.required_binaries
                if required_binaries is None
                else required_binaries
            ),
        )

    # -- Replay-bundle capture hooks (additive) -------------------------------

    def _capture_failure_bundle(
        self,
        exc: RendererException,
        *,
        request: RenderRequest,
    ) -> None:
        """Best-effort replay bundle for a failed backend invocation.

        Only failures attributable to a ``render``/``finalize``/``plan``/
        ``support`` invocation are captured; registry failures are not.
        Capture errors are swallowed so the original failure always
        propagates unchanged.
        """

        invocation = self._last_invocation
        if invocation is None or invocation.verb not in _REPLAY_VERBS:
            return
        try:
            bundle = self._build_replay_bundle(
                invocation=invocation,
                request=request,
                error=exc.error,
                success=False,
            )
            dest = self._replay_destination(
                renderer_id=bundle.renderer_id,
                request_digest=bundle.request_digest,
            )
            write_replay_bundle(bundle, dest)
        except Exception:
            # Diagnostics must never mask the render failure they describe.
            pass

    def _capture_success_bundle(
        self,
        *,
        request: RenderRequest,
    ) -> None:
        """Capture a bundle for a completed render when explicitly requested."""

        invocation = self._last_invocation
        if invocation is None or invocation.verb not in _REPLAY_VERBS:
            return
        try:
            bundle = self._build_replay_bundle(
                invocation=invocation,
                request=request,
                error=None,
                success=True,
            )
            dest = self._replay_destination(
                renderer_id=bundle.renderer_id,
                request_digest=bundle.request_digest,
            )
            write_replay_bundle(bundle, dest)
        except Exception:
            pass

    def _build_replay_bundle(
        self,
        *,
        invocation: _InvocationContext,
        request: RenderRequest,
        error: Any,
        success: bool,
    ) -> ReplayBundle:
        """Assemble the bundle record for the most recent backend invocation."""

        payload = invocation.payload
        payload_dict = payload.to_dict()
        # Attempt-local media handles are deliberately not replay authority;
        # the replay bundle captures the registry bytes and must be
        # re-materialized by a future host invocation.
        payload_dict.pop("materialized_root", None)
        payload_dict.pop("materialized_objects", None)
        eligibility = invocation.candidate.eligibility
        metadata: dict[str, Any] = {
            "bundle_schema_version": BUNDLE_SCHEMA_VERSION,
            "backend_version": invocation.candidate.manifest.version,
            "verb": invocation.verb,
            "success": success,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            # Qualified-ID discovery identity: which pack contributed the
            # candidate, how it was discovered, and the trust/eligibility
            # method that made it executable.
            "source_pack": invocation.candidate.pack_id,
            "source_kind": invocation.candidate.source_kind,
            "eligibility": eligibility.to_dict(),
            "trust_method": eligibility.trust_method,
        }
        if error is not None:
            metadata["error_kind"] = error.kind
            metadata["error_message"] = error.message
            metadata["recovery_command"] = (
                error.recovery_command
                or self._default_error_recovery(error.kind)
            )
        else:
            metadata["error_kind"] = None
            metadata["error_message"] = None
            metadata["recovery_command"] = None
        argv = [
            *invocation.candidate.manifest.command,
            invocation.verb,
            "--request",
            str(invocation.request_path),
            "--result",
            str(invocation.result_path),
        ]
        support_report = self._support_reports.get(invocation.candidate.id)
        return ReplayBundle(
            renderer_id=invocation.candidate.id,
            request_digest=compute_request_digest(
                {key: value for key, value in request.to_dict().items()
                 if key not in {"materialized_root", "materialized_objects"}}
            ),
            manifest_digest=invocation.candidate.manifest_digest,
            argv=argv,
            inputs=self._collect_replay_inputs(invocation, request),
            logs=self._replay_logs(error),
            partial_result=self._read_partial_result(invocation.result_path),
            payload=payload_dict,
            metadata=metadata,
            support_report=support_report.to_dict() if support_report is not None else None,
            backend_config=dict(payload_dict.get("backend_config") or {}),
            result_path=str(invocation.result_path),
        )

    def _collect_replay_inputs(
        self,
        invocation: _InvocationContext,
        request: RenderRequest,
    ) -> dict[str, str]:
        """Return no source paths: runtime documents are the authority.

        Render requests contain attempt-local materializations so the backend
        can use its path-oriented Node/FFmpeg interface.  A replay bundle is
        diagnostic output, not a second timeline authority; copying those
        paths (or discovering a theme by opening the timeline file) would make
        the render service a live file reader.  Runtime request/settlement
        metadata is already retained in ``payload`` and ``bundle.json``.
        """

        del invocation, request
        return {}

    def _replay_logs(self, error: Any) -> dict[str, str]:
        """Collect the failing invocation's stdout/stderr diagnostics."""

        logs: dict[str, str] = {}
        if error is not None:
            details = error.details or {}
            for stream in ("stdout", "stderr"):
                value = details.get(stream)
                if isinstance(value, str) and value:
                    logs[stream] = value
        if not logs:
            transport = self._transport
            last_logs = getattr(transport, "last_logs", None)
            if isinstance(last_logs, Mapping):
                for stream, value in last_logs.items():
                    if isinstance(value, str) and value:
                        logs.setdefault(str(stream), value)
        return logs

    @staticmethod
    def _read_partial_result(result_path: Path) -> Any:
        """Read the backend's authoritative result file if it wrote one."""

        try:
            if not result_path.is_file():
                return None
            raw = result_path.read_text(encoding="utf-8")
        except OSError:
            return None
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def _replay_destination(
        self,
        *,
        renderer_id: str,
        request_digest: str,
    ) -> Path:
        """Resolve the bundle directory for the current failure.

        Ownership precedence: an attached project run (``ASTRID_TASK_*``
        environment) owns bundles under the run's ``logs/replays`` directory;
        otherwise an explicit ``replay_root`` is used; otherwise a default
        sibling of the render output.
        """

        project_slug = os.environ.get(ASTRID_TASK_PROJECT)
        run_id = os.environ.get(ASTRID_TASK_RUN_ID)
        if project_slug and run_id:
            base = (
                run_dir(project_slug, run_id, root=resolve_projects_root())
                / "logs"
                / "replays"
            )
        elif self._replay_root is not None:
            base = self._replay_root
        else:
            output = self._active_output
            base = (
                output.parent / f".{output.name}.replay"
                if output is not None
                else Path(".astrid-replays")
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        name = f"replay-{stamp}-{renderer_id}-{request_digest[:12]}"
        candidate = base / name
        suffix = 2
        while candidate.exists():
            candidate = base / f"{name}-{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _delivery_audio_target(request: RenderRequest) -> DeliveryAudioTarget | None:
        """The opt-in delivery audio master requested in request metadata, if any."""
        raw = request.metadata.get(METADATA_KEY)
        if raw is None:
            return None
        try:
            return DeliveryAudioTarget.from_mapping(json.loads(raw))
        except (TypeError, ValueError) as exc:
            raise_protocol_error(
                backend=_CORE_BACKEND_ID,
                message=f"invalid delivery audio target: {exc}",
                recovery_command="pass --audio-target LUFS and --true-peak DBTP with sane values",
            )

    @staticmethod
    def _master_delivery_audio(
        result: RenderResult,
        source_video: Path,
        *,
        workspace: Path,
        target: DeliveryAudioTarget,
        backend: str,
    ) -> tuple[Path, dict[str, Any]]:
        """Apply the delivery audio master to the final mix; video is stream-copied."""
        if not result.video.profile.has_audio:
            raise_unsupported_error(
                backend=backend,
                message="a delivery audio target needs a rendered audio track; this output has none",
                recovery_command="render with audio, or drop the delivery audio target",
            )
        dest = workspace / "delivery-audio.mp4"
        try:
            receipt = apply_delivery_audio(
                source_video, dest, target, ffmpeg=find_ffmpeg(),
                sample_rate=getattr(result.video.profile, "audio_sample_rate", None),
            )
        except DeliveryAudioError as exc:
            raise_unsupported_error(
                backend=backend,
                message=f"delivery audio master failed: {exc}",
                recovery_command="check ffmpeg (loudnorm, alimiter, ebur128) and the target values",
                details={"target": target.to_dict()},
            )
        receipt["source_sha256"] = result.video.sha256
        return dest, receipt

    @staticmethod
    def _artifact_path(result: RenderResult, workspace: Path) -> Path:
        candidate = (workspace / result.video.path).resolve(strict=False)
        try:
            candidate.relative_to(workspace.resolve())
        except ValueError:
            raise_invalid_artifact_error(
                backend=_CORE_BACKEND_ID,
                message="validated renderer artifact escaped its invocation workspace",
                recovery_command="rerun the renderer with a contained output path",
                details={"path": result.video.path},
            )
        return candidate

    @staticmethod
    def _input_hashes(request: RenderRequest) -> dict[str, str]:
        paths: dict[str, Path] = {"timeline": Path(request.timeline_path)}
        if request.assets_registry_path is not None:
            paths["assets_registry"] = Path(request.assets_registry_path)
        return {
            name: sha256_file(path)
            for name, path in paths.items()
            if path.is_file()
        }

    @staticmethod
    def _merge_backend_fragments(
        results: Sequence[RenderResult],
    ) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for result in results:
            for namespace, fragment in result.backend_fragments.items():
                current = merged.get(namespace)
                if current is None:
                    merged[namespace] = dict(fragment)
                elif current != fragment:
                    records = current.get("service_fragment_sequence")
                    if isinstance(records, list):
                        records.append(dict(fragment))
                    else:
                        merged[namespace] = {
                            "service_fragment_sequence": [current, dict(fragment)]
                        }
        return merged

    @staticmethod
    def _alternatives(
        registry: RendererRegistry | PlannerRegistry | FinalizerRegistry,
        *,
        exclude: str | None = None,
    ) -> list[str]:
        try:
            return [
                candidate.id
                for candidate in registry.list()
                if candidate.id != exclude
            ]
        except RenderingRegistryError:
            return []

    @staticmethod
    def _recovery_for(alternatives: Sequence[str]) -> str:
        if alternatives:
            return "select one of these alternatives and retry: " + ", ".join(
                alternatives
            )
        return "install or select an execution-eligible compatible capability and retry"

    @staticmethod
    def _default_error_recovery(kind: str) -> str:
        return {
            "protocol": "regenerate the request with renderer protocol v1",
            "unsupported": "select a compatible renderer and retry",
            "binary_missing": "install the renderer's required binaries and retry",
            "timeout": "retry the render or increase the renderer timeout",
            "interrupted": "retry the render when interruption is no longer requested",
            "invalid_artifact": "rerender the artifact in a fresh invocation workspace",
            "internal": "retry the render in a fresh invocation workspace",
        }.get(kind, "retry the render after resolving the reported failure")

    def _observe(self, stage: str, **details: Any) -> None:
        if self._stage_observer is not None:
            self._stage_observer(stage, details)


__all__ = ["RenderService"]
