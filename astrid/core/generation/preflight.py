"""Read-only readiness checks for generation backends.

The public generation facade performs these checks before handing work to the
kernel. Structural checks inspect local installation state; target selection
may probe one selected loopback endpoint. Neither starts ComfyUI, imports the
optional VibeComfy stack, creates staging directories, or writes target config.

Astrid's local adapter uses VibeComfy to start a managed ComfyUI server for a
one-shot run.  Therefore a live server endpoint is *not* a prerequisite for
the managed path and an offline endpoint must not be reported as an
unsupported installation.  Endpoint reachability belongs to the runtime
startup error, after admission, when the installation is otherwise ready.
"""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from astrid.packs.runpod.prepared_task import PreparedWorkerSelection


@dataclass(frozen=True)
class PreparedRunPodGeneration:
    """R03/F07 handoff; endpoint custody stays with the retained host owner.

    A URL is never a handoff.
    """
    selection: PreparedWorkerSelection
    selection_path: str
    owner_tick: Callable[[Mapping[str, Any]], None]


class RunPodGenerationOwner(Protocol):
    """Typed fake boundary pending R04 and F05/F06/F07 publication.

    validation_target is read-only and must not claim/install. prepare checks
    a fresh offer/rate/consent and compatibility before beginning warm custody.
    monitor/retain own activation, timers, recovery and exact endpoint checkout.
    release dispatches runpod-release:<operation_id>; paid expiry cancels/fences
    the active task before release_prepared_worker(resume=True). Admission is
    never release proof: P1 quiescence + GET404 + list absence are required.

    """
    def validation_target(self) -> Any: ...
    def prepare(self, *, workflow: Any, inputs: Mapping[str, Any],
                consent: tuple[str, ...], max_usd: float, deadline: str) -> PreparedRunPodGeneration: ...
    def describe(self) -> Mapping[str, Any]: ...
    def release(self, *, wait: bool) -> Any: ...
    def retain(self, task: Mapping[str, Any], monitor: Callable[..., None]) -> None: ...


def runpod_generation_owner(client: Any) -> RunPodGenerationOwner:
    owner = getattr(client, 'runpod_generation_owner', None)
    if owner is None:
        raise generation_error(ValueError('RunPod host owner is unavailable; R04/F05/F06/F07 publication is required'),
                               code='missing_dependency', details={'field': 'target'})
    return owner


def call_runpod_owner(method: Callable[..., Any], /, **kwargs: Any) -> Any:
    """Project new host-boundary failures without changing older SDK errors."""
    from astrid.sdk.exceptions import AstridSDKError
    try:
        return method(**kwargs)
    except (AstridError, AstridSDKError):
        raise
    except (ValueError, RuntimeError, OSError) as exc:
        raise generation_error(exc) from exc

from astrid.core.contracts.errors import AstridError
from astrid.core.generation.interfaces import ReadinessEnvelope, ReadinessFix, ReadinessIssue


_CORE_CODES = {
    'COMFY_TARGET_UNAVAILABLE': 'target_unreachable',
    'COMFY_TARGET_INVALID': 'input_rejected',
    'COMFY_TARGET_UNSUPPORTED': 'missing_dependency',
    'COMFY_TARGET_NOT_READY': 'engine_not_found',
    'COMFY_TARGET_NO_MODEL': 'missing_dependency',
    'engine_not_installed': 'engine_not_found',
    'comfyui.missing_node': 'missing_dependency',
    'comfyui.models_missing': 'missing_dependency',
}


# Readiness ladder

LADDER_RUNGS = ("installed", "reachable", "identified", "compatible",
                "computes", "workflow_ready", "delivered")
LADDER_OWNERS = {
    "installed": "existing-install",
    "reachable": "existing-install",
    "identified": "existing-install",
    "compatible": "existing-install",
    "computes": "environment",
    "workflow_ready": "workflow",
    "delivered": "delivery",
}
LADDER_THROUGH = LADDER_RUNGS[:6]
LADDER_ISSUE_CODES = {
    "installed": "target_not_installed",
    "reachable": "target_unreachable",
    "identified": "target_not_identified",
    "compatible": "target_incompatible",
    "computes": "target_not_computing",
    "workflow_ready": "workflow_not_ready",
}


def new_ladder(through):
    """Readiness ladder.

    Seven ordered rungs: installed, reachable, identified, compatible, computes,
    workflow_ready, delivered. Each rung is observed, failed or unchecked; its
    owner supplies the observation or reason. Through is the last rung this
    answer owns (never delivered). Ready requires positive observations through
    that rung. The first non-observed rung blocks with one next action: a person
    step (optionally followed by an Astrid recheck) or an agent command. No
    timestamps or durations are added. Without a ladder the envelope is unchanged.
    Delivery evidence belongs to the result path.

    Pinned tests owed to later packets: `tests/packs/test_vibecomfy_local.py::test_scan_reports_pinokio_when_no_script_is_running` and `::test_scan_continues_when_pinokio_is_absent` compare the whole scan response and gain `"installations": []` (R16-P93-06). No other pinned test changes: envelopes without a ladder are unchanged, and the tests that call `generate targets` (`test_workflow_input_a01.py`, `test_settings_set_placement.py`, `test_generation_readiness_fields.py`) assert only `target` and `placement`.
    """
    if through not in LADDER_THROUGH:
        raise ValueError("invalid readiness ladder through")
    return {"through": through, "rungs": [
        {"rung": rung, "owner": LADDER_OWNERS[rung], "state": "unchecked",
         "observation": None, "reason": None, "message": None}
        for rung in LADDER_RUNGS
    ], "stopped_at": None}


def _validate_rung_state(state, observation, reason, message):
    if state not in ("observed", "failed", "unchecked"):
        raise ValueError("invalid readiness rung state")
    if (observation is not None and not isinstance(observation, dict)
            or state == "observed" and not isinstance(observation, dict)):
        raise ValueError("an observed rung needs an observation object")
    if (reason is not None and not isinstance(reason, str)
            or state == "failed" and (not isinstance(reason, str) or not reason.strip())):
        raise ValueError("a failed rung needs a nonempty reason")
    if message is not None and not isinstance(message, str):
        raise ValueError("readiness rung message must be a string")


def _validate_ladder(ladder):
    if not isinstance(ladder, dict) or ladder.get("through") not in LADDER_THROUGH:
        raise ValueError("invalid readiness ladder through")
    rungs = ladder.get("rungs")
    if not isinstance(rungs, list) or len(rungs) != len(LADDER_RUNGS):
        raise ValueError("readiness ladder needs all seven ordered rungs")
    for name, item in zip(LADDER_RUNGS, rungs):
        if (not isinstance(item, dict)
                or set(item) != {"rung", "owner", "state", "observation", "reason", "message"}
                or item["rung"] != name or item["owner"] != LADDER_OWNERS[name]):
            raise ValueError("invalid readiness ladder rung or owner")
        _validate_rung_state(item["state"], item["observation"], item["reason"], item["message"])


def with_rung(ladder, rung, state, *, observation=None, reason=None, message=None):
    """Return a separate ladder with precisely one rung's evidence replaced."""
    from copy import deepcopy

    _validate_ladder(ladder)
    if rung not in LADDER_RUNGS:
        raise ValueError("unknown readiness rung")
    _validate_rung_state(state, observation, reason, message)
    result = deepcopy(ladder)
    result["rungs"][LADDER_RUNGS.index(rung)] = {
        "rung": rung, "owner": LADDER_OWNERS[rung], "state": state,
        "observation": deepcopy(observation), "reason": reason, "message": message,
    }
    return result


def ladder_stop(ladder):
    """Return the first missing observation in this answer's owned prefix."""
    _validate_ladder(ladder)
    return next((item for item in ladder["rungs"][:LADDER_RUNGS.index(ladder["through"]) + 1]
                 if item["state"] != "observed"), None)


def make_next_action(who, step, *, argv=None, then_argv=None, alternative=None, choices=None):
    """Describe exactly one action, not an implicit sequence of repairs."""
    from copy import deepcopy

    def command(value):
        if (not isinstance(value, (list, tuple)) or not value
                or not all(isinstance(word, str) and word.strip() for word in value)):
            raise ValueError("readiness action command must be a nonempty string array")
        return list(value)

    if not isinstance(step, str) or not step.strip():
        raise ValueError("readiness action needs a step")
    if who == "person" and argv is None:
        then_argv = command(then_argv) if then_argv is not None else None
    elif who == "agent" and then_argv is None:
        argv = command(argv)
    else:
        raise ValueError("readiness action must be one person step or agent command")
    return {"who": who, "step": step, "argv": argv, "then_argv": then_argv,
            "alternative": deepcopy(alternative), "choices": deepcopy(choices)}


# Observations of the human-owned existing-install prefix.

LADDER_REASONS = {
    "files_missing": "installed",
    "not_answering": "reachable",
    "access_denied": "reachable",
    "not_comfyui": "identified",
    "api_unsupported": "compatible",
}

LADDER_TEXT = {
    "files_missing": {
        "message": "The folder recorded for this ComfyUI no longer exists.",
        "step": "Tell me the address of a ComfyUI that is running, or reinstall it, then ask me to check again.",
        "who": "person",
    },
    "not_answering": {
        "message": "Nothing is answering at {url}.",
        "step": "Start your ComfyUI so it answers at {url}, then ask me to check again.",
        "who": "person",
    },
    "access_denied": {
        "message": "Access to {url} was denied.",
        "step": "Retry the same check through the normal permission request.",
        "who": "agent",
    },
    "not_comfyui": {
        "message": "Something that is not ComfyUI answers at {url}.",
        "step": "Close it, or tell me the address of your ComfyUI.",
        "who": "person",
    },
    "api_unsupported": {
        "message": "The ComfyUI at {url} does not offer the API Astrid needs.",
        "step": "Update that ComfyUI, then ask me to check again.",
        "who": "person",
    },
}


def _comfy_get_json(url, path):
    """Read one bounded JSON observation from this exact attached endpoint."""
    from .backends.comfy_target import MAX_JSON_BYTES, ComfyTarget, ComfyTargetClient

    client = ComfyTargetClient(ComfyTarget(url, "attached"))
    if path == "/object_info":
        return client.request_json(path, timeout=5.0, response_limit=MAX_JSON_BYTES)
    return client.request_json(path, timeout=1.0)


def _failure_kind(exc):
    """Classify a bounded exception graph without leaking transport details."""
    def attr(item, name):
        try:
            return getattr(item, name, None)
        except Exception:  # noqa: BLE001 - classification must not raise
            return None

    pending = [(exc, 0)]
    seen = set()
    denied = False
    while pending:
        item, depth = pending.pop(0)
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        if isinstance(attr(item, "code"), int):
            return "answered_not_comfyui"
        if isinstance(item, PermissionError):
            denied = True
        if depth < 6:
            pending.extend((attr(item, name), depth + 1) for name in ("__cause__", "reason"))
    return "access_denied" if denied else "not_answering"


def observe_target_rungs(record, *, get_json=None):
    """Observe the four existing-install rungs without adopting or starting it."""
    from pathlib import Path

    from .targets import TargetRecord

    if not isinstance(record, TargetRecord) or record.owner != "human" or not record.endpoint:
        raise ValueError("existing-install observations require a human-owned endpoint")
    url = record.endpoint
    probe = _comfy_get_json if get_json is None else get_json
    ladder = new_ladder("compatible")

    def failed(rung, reason):
        result = with_rung(ladder, rung, "failed", reason=reason,
                           message=LADDER_TEXT[reason]["message"].format(url=url))
        for later in LADDER_RUNGS[LADDER_RUNGS.index(rung) + 1:]:
            result = with_rung(result, later, "unchecked", reason="not_reached")
        return result

    if record.root is None:
        installed = {"identity": "endpoint_only"}
    else:
        try:
            exists = Path(record.root).is_dir()
        except OSError:
            exists = False
        if not exists:
            return failed("installed", "files_missing")
        installed = {"root_exists": True}
    ladder = with_rung(ladder, "installed", "observed", observation=installed)

    try:
        stats = probe(url, "/system_stats")
    except Exception as exc:  # noqa: BLE001 - injected/transport failures are observations
        reason = _failure_kind(exc)
        if reason != "answered_not_comfyui":
            return failed("reachable", reason)
        stats = None
    ladder = with_rung(ladder, "reachable", "observed", observation={"endpoint": url})

    if not isinstance(stats, dict) or not isinstance(stats.get("system"), dict):
        return failed("identified", "not_comfyui")
    ladder = with_rung(ladder, "identified", "observed", observation={"tied_to_install": False})

    try:
        schema = probe(url, "/object_info")
    except Exception:  # noqa: BLE001 - an unavailable API is a stable compatibility failure
        return failed("compatible", "api_unsupported")
    if not isinstance(schema, dict) or not schema:
        return failed("compatible", "api_unsupported")
    version = stats["system"].get("comfyui_version")
    return with_rung(ladder, "compatible", "observed", observation={
        "comfyui_version": version if isinstance(version, str) else None,
    })


def ladder_next_action(ladder, *, url=None, installation=None):
    """Project the first missing observation into one person or agent action."""
    stop = ladder_stop(ladder)
    if stop is None:
        return None
    command = ["astrid", "generate", "targets", "--placement", url or "auto"]
    wording = LADDER_TEXT.get(stop["reason"])
    if wording is None:
        step = f"Check the {stop['rung']} step ({stop['reason'] or 'not checked'}), then ask me to check again."
        return make_next_action("person", step, then_argv=command)
    step = wording["step"].format(url=url or "auto")
    if wording["who"] == "agent":
        return make_next_action("agent", step, argv=command)
    return make_next_action("person", step, then_argv=command)


def _mirror_next_action(action):
    from copy import deepcopy

    if action["who"] == "person":
        return {"scope": "attached_target", "details": {
            "guidance": action["step"], "who": "person",
            "then_argv": deepcopy(action["then_argv"]),
        }}
    return {"argv": deepcopy(action["argv"]), "scope": "read_only",
            "details": {"guidance": action["step"], "who": "agent"}}


def readiness_envelope(*, entry=None, target=None, issues=(), fixes=(), attempt_count=None,
                       ladder=None, next_action=None) -> ReadinessEnvelope:
    """Project observations once; unknown version facts do not become issues."""
    from copy import deepcopy

    from .setup_contract import uses_setup_contract, validate_setup_envelope
    issues = [deepcopy({**issue, 'details': issue.get('details', {})}) for issue in issues]
    # An attached target's fix names what the person does on their own ComfyUI; it has no argv to run.
    fixes = [deepcopy({**fix, **({'argv': list(fix['argv'])} if isinstance(fix.get('argv'), tuple) else {}),
              'details': fix.get('details', {})})
             for fix in fixes]
    target_payload = deepcopy(target.as_dict() if hasattr(target, 'as_dict') else target)
    if ladder is None:
        if next_action is not None:
            raise ValueError("next_action requires a readiness ladder")
    else:
        ladder = deepcopy(ladder)
        stop = ladder_stop(ladder)
        ladder["stopped_at"] = stop["rung"] if stop is not None else None
        if stop is not None:
            rung = stop["rung"]
            code = "rung_unchecked" if stop["state"] == "unchecked" else LADDER_ISSUE_CODES[rung]
            issue = {"code": code, "severity": "error",
                     "message": stop["message"] or f"{rung} was not observed",
                     "details": {"rung": rung, "reason": stop["reason"], "field": "target"}}
            matching = [index for index, item in enumerate(issues)
                        if item["code"] == code and item["details"].get("rung") == rung]
            if not matching:
                issues.append(issue)
            else:
                # Current rung evidence owns this issue, even when callers compose
                # duplicate issues or carry one from an earlier observation.
                issues[matching[0]] = issue
                for index in reversed(matching[1:]):
                    del issues[index]
            if next_action is None:
                endpoint = (target_payload or {}).get("endpoint") or "auto"
                next_action = make_next_action(
                    "person", f"Check the {rung} step ({stop['reason'] or 'not checked'}), then ask me to check again.",
                    then_argv=["astrid", "generate", "targets", "--placement", endpoint],
                )
        if next_action is not None:
            if not isinstance(next_action, dict):
                raise ValueError("next_action must be an action object")
            try:
                next_action = make_next_action(**next_action)
            except TypeError as exc:
                raise ValueError("invalid readiness next_action") from exc
        mirror = [] if next_action is None else [_mirror_next_action(next_action)]
        if fixes and fixes != mirror:
            raise ValueError("ladder fixes must mirror the single next_action")
        fixes = mirror
    status = ('blocked' if any(issue['severity'] == 'error' for issue in issues) else
              'ready_with_warnings' if issues else 'ready')
    envelope = {'entry': deepcopy(entry),
            'target': target_payload,
            'status': status, 'issues': issues, 'fixes': fixes}
    if ladder is not None:
        envelope["ladder"] = ladder
        envelope["next_action"] = next_action
        if attempt_count is not None or uses_setup_contract(envelope):
            raise ValueError("readiness ladder cannot opt into the strict setup contract")
    if attempt_count is not None or uses_setup_contract(envelope):
        # The caller counts actual attempts; constructing/serializing is not one.
        envelope['attempt_count'] = 0 if attempt_count is None else attempt_count
        validate_setup_envelope(envelope)
    return envelope


class GenerationReadinessError(AstridError, ValueError):
    """The same blocked payload is available to CLI, SDK and MCP adapters."""

    def __init__(self, envelope: ReadinessEnvelope, *, task_id: str | None = None):
        from astrid.core.contracts.errors import _redact_credential_data
        self.readiness = readiness_envelope(**{key: envelope[key] for key in
            ('entry', 'target', 'issues', 'fixes', 'attempt_count', 'ladder', 'next_action') if key in envelope})
        self.readiness = _redact_credential_data(self.readiness)
        self.task_id = task_id
        if 'attempt_count' in self.readiness and self.readiness['status'] != 'blocked':
            raise ValueError('a setup readiness error requires a blocking issue')
        if not self.readiness['issues']:
            raise ValueError('a readiness error needs an issue')
        issue = next((item for item in self.readiness['issues'] if item['severity'] == 'error'), self.readiness['issues'][0])
        fix = self.readiness['fixes'][0] if self.readiness['fixes'] else None
        import shlex
        # An attached fix is a person's action on their own ComfyUI, not a command to run.
        recovery = (shlex.join(fix['argv']) if fix and 'argv' in fix
                    and fix.get('who', 'agent') == 'agent' and not fix.get('placeholders') else None)
        if issue['code'] == 'generation_route_unqualified':
            recovery = issue['details'].get('fix') or recovery
        super().__init__(issue['message'], code=issue['code'],
                         field=issue['details'].get('field'),
                         recovery_command=recovery, fix=issue['details'].get('fix'),
                         state_snapshot={'readiness': self.readiness})

    def to_envelope(self) -> dict[str, Any]:
        from copy import deepcopy
        return {**deepcopy(self.readiness), 'status': 'blocked', 'task_id': self.task_id}


def generation_error(error: Any, *, entry=None, target=None, code=None,
                     details=None, fixes=(), task_id=None, attempt_count=None) -> GenerationReadinessError:
    """Normalize old engine/ExecError spellings at the generation boundary."""
    if isinstance(error, GenerationReadinessError):
        return error
    from collections.abc import Mapping
    if isinstance(error, Mapping) and {'entry', 'target', 'status', 'issues', 'fixes'} <= error.keys():
        return GenerationReadinessError(error, task_id=task_id or error.get('task_id'))
    task_id = task_id or getattr(error, 'task_id', None)
    readiness = getattr(error, 'readiness', None)
    if isinstance(readiness, dict):
        return GenerationReadinessError(readiness, task_id=task_id)
    if callable(getattr(error, 'astrid_error_fields', None)):
        from astrid.core.contracts.errors import coerce_astrid_error

        error = coerce_astrid_error(error)
        if error.fix:
            details = {'fix': error.fix, **(details or {})}
    get = error.get if isinstance(error, Mapping) else lambda key, default=None: getattr(error, key, default)
    category = get('category') or get('sdk_category')
    native = code or get('code') or {
        'validation': 'input_rejected', 'missing_input': 'input_rejected',
        'precondition': 'missing_dependency', 'runtime': 'generation_failed',
    }.get(category, 'generation_failed')
    normalized = _CORE_CODES.get(native, native)
    detail = {**(get('details') or {}), **(details or {})}
    diagnostic = get('engine_diagnostic')
    if isinstance(diagnostic, Mapping):
        detail['engine_diagnostic'] = {key: diagnostic[key] for key in
            ('code', 'node_id', 'node_class', 'exception_type', 'oom') if key in diagnostic}
    if normalized == 'generation_route_unqualified':
        detail['fix'] = get('fix') or get('recovery_command')
    for key in ('sdk_category', 'sdk_error'):
        if get(key):
            detail.setdefault(key, get(key))
    if normalized != native:
        detail['engine_code'] = native
    if get('field'):
        detail.setdefault('field', get('field'))
    message = get('message') or get('cause') or str(error)
    if normalized == 'input_rejected':
        import re
        match = re.search(r"(?:input|inputs for) '([^']+)'", message)
        if match:
            detail.setdefault('field', match[1])
        else:
            match = re.search(r'(?:unsupported entry inputs|missing required entry inputs|duplicate input values): (.+)', message)
            if match:
                names = match[1].split(', ')
                detail.setdefault('field', names[0])
                if len(names) > 1:
                    detail.setdefault('fields', names)
        detail.setdefault('constraint', message)
    issue = {'code': normalized, 'severity': 'error', 'message': message, 'details': detail}
    from .setup_contract import uses_setup_contract
    fixes = list(fixes)
    if uses_setup_contract({'issues': [issue], 'fixes': fixes}):
        issue['fix_ids'] = [fix['id'] for fix in fixes if 'id' in fix]
    return GenerationReadinessError(readiness_envelope(entry=entry, target=target,
        issues=[issue], fixes=fixes, attempt_count=attempt_count), task_id=task_id)


def compare_versions(recorded: dict[str, Any], observed: dict[str, Any]) -> list[ReadinessIssue]:
    """Different known versions warn; unknown recorded/observed facts are silent."""
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.version import InvalidVersion, Version
    issues = []
    for name, version in recorded.items():
        current = observed.get(name)
        if not isinstance(version, str) or not isinstance(current, str):
            continue
        try:
            actual = Version(current)
            try:
                different = Version(version) != actual
            except InvalidVersion:
                constraint = SpecifierSet(version)
                if not str(constraint):
                    continue
                different = actual not in constraint
        except (InvalidVersion, InvalidSpecifier):
            continue
        if different:
            issues.append({'code': 'version_drift', 'severity': 'warning',
                           'message': f'{name} recorded version differs from the selected target',
                           'details': {'name': name, 'recorded': version, 'observed': current}})
    return issues


@dataclass(frozen=True)
class LocalGenerationReadiness:
    """A deterministic, side-effect-free local runtime readiness result."""

    ready: bool
    reason: str = ""
    recovery_command: str = ""
    entry: dict[str, Any] | None = None
    target: Any = None
    issues: tuple[ReadinessIssue, ...] = ()
    fixes: tuple[ReadinessFix, ...] = ()

    def to_envelope(self) -> ReadinessEnvelope:
        issues = self.issues
        if not self.ready and not issues:
            issues = ({'code': 'engine_not_found', 'severity': 'error',
                       'message': self.reason, 'details': {}},)
        return readiness_envelope(entry=self.entry, target=self.target,
                                  issues=issues, fixes=self.fixes)


def _gpu_guidance(mode: str | None = None) -> str:
    guidance = 'Generation needs a local GPU or --placement runpod.'
    if mode == 'tts':
        guidance += (' For voice, explicitly choose Edge TTS through '
                     'generation.generate_speech with provider=edge-tts.')
    return guidance


def _feature_is_missing(feature: str, value: Any) -> bool:
    """Return whether a generation feature value is absent for preflight.

    Keep this small predicate here instead of importing the executor's
    ``_common`` module: preflight runs before capability admission and must
    remain independent of executor/runtime imports.
    """

    if value is None:
        return True
    if feature == "count":
        return not bool(value)
    if isinstance(value, str):
        return not value.strip()
    return False


def validate_generation_request(
    registry: Any,
    *,
    model: str,
    mode: str,
    execution: str,
    inputs: dict[str, Any] | None = None,
    modality: str | None = None,
    required_features: tuple[str, ...] | None = None,
) -> tuple[Any, Any]:
    """Validate a generation request before dry-run or kernel admission.

    This is the shared model → mode → execution matrix check used by typed
    generation facades and generic ``sdk.invoke``.  It also enforces the
    model-declared required inputs, notably ``image_end_ref`` for FLF video.
    The function performs no filesystem, network, runtime, or ledger writes.
    """

    from astrid.sdk.exceptions import (
        CapabilityMissingInputError,
        CapabilityValidationError,
    )

    if not isinstance(model, str) or not model.strip():
        raise CapabilityValidationError(
            "generation model must be a non-empty string"
        )
    if not isinstance(mode, str) or not mode.strip():
        raise CapabilityValidationError(
            "generation mode must be a non-empty string"
        )
    if not isinstance(execution, str) or not execution.strip():
        raise CapabilityValidationError(
            "generation execution must be one of the model's declared "
            "backends; provide execution='local' or execution='cloud'"
        )

    try:
        entry, mode_spec = registry.get_by_mode(model, mode)
    except (KeyError, TypeError, AttributeError) as exc:
        raise CapabilityValidationError(str(exc)) from exc

    if modality is not None and getattr(entry, "modality", None) != modality:
        raise CapabilityValidationError(
            f"model {model!r} is a {getattr(entry, 'modality', 'unknown')} "
            f"model, not a {modality} model"
        )

    backend_ids = tuple(sorted(getattr(mode_spec, "backends", {})))
    if execution not in backend_ids:
        available = ", ".join(backend_ids) or "none"
        raise CapabilityValidationError(
            f"Execution {execution!r} is not available for model {model!r} "
            f"mode {mode!r}. Available backends: {available}"
        )

    request_inputs = inputs or {}
    required = (
        getattr(mode_spec, "requires", ())
        if required_features is None
        else required_features
    )
    missing = [
        feature
        for feature in required
        if _feature_is_missing(feature, request_inputs.get(feature))
    ]
    if missing:
        joined = ", ".join(sorted(missing))
        pronoun = "it" if len(missing) == 1 else "them"
        raise CapabilityMissingInputError(
            f"model {model!r} mode {mode!r} requires: {joined}. "
            f"Provide {pronoun} before admission and retry."
        )

    from astrid.core.generation.availability import require_model_available
    require_model_available(model, mode, execution)
    return entry, mode_spec


def _module_available(module_name: str) -> bool:
    """Return whether *module_name* can be discovered without importing it."""

    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        # ``find_spec('comfy.cmd.main')`` raises when an optional parent package
        # is absent (and malformed __spec__ objects can raise ValueError).
        return False


def _comfyui_runtime_available() -> bool:
    """Discover a legacy local ComfyUI in the current Python environment or PATH."""

    if _module_available("comfy.cmd.main"):
        return True
    executable = shutil.which("comfyui")
    return bool(executable)


def _managed_install_fix() -> ReadinessFix | None:
    """Describe the indexed local installer for this host without running it."""
    import platform
    from astrid.core.generation.engine_contracts import EngineError
    from astrid.core.generation.engine_local_endpoint import default_engine_root
    from astrid.core.generation.engine_recipe import load_recipe_index, load_recipe_model_lock

    # The current local execution adapter supports Linux x86_64. Do not offer
    # its recipe on another machine, or infer a CUDA platform from the CPU pod.
    if platform.system() != 'Linux' or platform.machine().lower() not in {'x86_64', 'amd64'}:
        return None
    try:
        candidates = [row for row in load_recipe_index().entries
                      if row.engine == 'comfyui'
                      and row.platform.startswith('linux-x86_64-')]
        if len(candidates) != 1:
            return None
        recipe = candidates[0]
        root = default_engine_root(recipe.engine)
    except (EngineError, OSError):
        return None
    try:
        model_bytes = sum(model.bytes for model in load_recipe_model_lock(
            recipe.engine, recipe.platform).models)
    except (EngineError, OSError):
        model_bytes = None
    scope = f'installs Astrid-managed ComfyUI under {root}; user scope; no system changes'
    size = (f'at least {model_bytes} bytes of locked models; additional runtime download size unknown'
            if model_bytes is not None else 'size unknown')
    return {'argv': ['astrid', 'engines', 'install', recipe.engine,
                     '--platform', recipe.platform, '--placement', 'local'],
            'scope': 'managed_write',
            'details': {'bytes': model_bytes, 'size_scope': 'locked_models',
                        'additional_bytes': None, 'scope': scope,
                        'guidance': f'{scope}; {size}'}}


def _registered_managed_engine() -> dict[str, str] | None:
    """The registered managed ComfyUI engine's owner and loopback endpoint, or None.

    Deterministic: reads the recorded install root and the recipe's port only.
    It does not probe health; the run-time verifier does that.
    """
    from .engine_contracts import EngineError
    from .engine_local_endpoint import DEFAULT_PLATFORM, recorded_engine_root
    from .engine_recipe import load_recipe

    try:
        root = recorded_engine_root("comfyui")
    except EngineError:
        return None
    if root is None:
        return None
    recipe = load_recipe("comfyui", DEFAULT_PLATFORM)
    return {"owner": "astrid", "endpoint": f"http://127.0.0.1:{recipe.start.port}", "root": root}


def check_local_generation_readiness(
    entry: Any,
    mode: str,
    *,
    python_executable: str | None = None,
    require_managed_runtime: bool = True,
    legacy_local: bool = True,
) -> LocalGenerationReadiness:
    """Check deterministic prerequisites for a selected local model path.

    This is deliberately narrower than a runtime smoke test.  In particular,
    it does not probe a configured HTTP URL: Astrid starts a managed local
    server when the runtime is installed, and a configured/temporarily offline
    endpoint is a runtime condition rather than an unsupported installation.
    """

    mode_spec = getattr(entry, "modes", {}).get(mode)
    backend_spec = getattr(mode_spec, "backends", {}).get("local") if mode_spec else None
    template = getattr(backend_spec, "template", "") if backend_spec else ""
    if not isinstance(template, str) or not template.strip():
        return LocalGenerationReadiness(
            ready=False,
            reason=(
                f"local generation for model {getattr(entry, 'id', '<unknown>')!r} "
                f"mode {mode!r} has no configured VibeComfy template"
            ),
            recovery_command=(
                "inspect the model registry and configure a local template, "
                "or choose a declared cloud/codex backend"
            ),
        )

    if legacy_local and not _module_available("vibecomfy"):
        return LocalGenerationReadiness(
            ready=False,
            reason=(
                "local generation requires the 'vibecomfy' Python package; "
                "it is not installed in the Python environment Astrid is using"
            ),
            recovery_command=(
                "VibeComfy ships with Astrid; if it is missing, run the normal Astrid update "
                "in a terminal outside chat: "
                "curl -fsSL https://astrid.haus/install | sh -s -- --update (macOS/Linux); "
                "Windows PowerShell: & ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --update"
            ),
        )

    managed_ready = not require_managed_runtime or (
        _registered_managed_engine() is not None if not legacy_local
        else _comfyui_runtime_available()
    )
    if require_managed_runtime and not managed_ready:
        install = _managed_install_fix()
        attach = ['astrid', 'generate', 'image', '--placement', 'http://127.0.0.1:8188']
        import shlex
        guidance = f"start your existing ComfyUI and attach with {shlex.join(attach)}"
        if install:
            guidance = f"{shlex.join(install['argv'])} ({install['details']['guidance']}); or {guidance}"
        guidance += "; ComfyUI version provenance is unknown until the selected runtime is observed"
        return LocalGenerationReadiness(
            ready=False,
            reason=(
                ("local generation requires a ComfyUI runtime; no managed engine is installed or registered"
                 if not legacy_local else
                 "legacy local generation requires a ComfyUI runtime ('comfy.cmd.main' "
                 "or the 'comfyui' CLI); neither is installed or discoverable")
                + '. ' + _gpu_guidance(mode)
            ),
            recovery_command=guidance,
            fixes=(
                *((install,) if install else ()),
                {'argv': attach, 'scope': 'read_only',
                 'details': {'guidance': 'Attach after starting your existing ComfyUI'}},
            ),
        )

    return LocalGenerationReadiness(ready=True)


def require_local_generation_readiness(
    entry: Any,
    mode: str,
    *,
    python_executable: str | None = None,
) -> None:
    """Raise a public precondition error when local readiness is absent."""

    result = check_local_generation_readiness(
        entry,
        mode,
        python_executable=python_executable,
    )
    if result.ready:
        return

    from astrid.sdk.exceptions import CapabilityPreconditionError

    raise CapabilityPreconditionError(
        f"Local generation is not ready: {result.reason}. "
        f"Next: {result.recovery_command}"
    )


def _installed_managed_target(entry_id=None):
    """Bind an installed ComfyUI through its existing receipt/custody verifier."""
    from .backends.comfy_target import ComfyTarget, ComfyTargetClient
    from .engine_contracts import EngineError
    from .engine_local_endpoint import EntryAssetsNotReady, local_engine_endpoint, recorded_engine_root
    from .targets import TargetRecord

    try:
        root = recorded_engine_root('comfyui')
        if root is None:
            return None
        installed = local_engine_endpoint('comfyui', root=root,
            **({'entry_id': entry_id} if entry_id is not None else {}))
    except EntryAssetsNotReady as exc:
        from .asset_gaps import asset_readiness
        raise GenerationReadinessError(asset_readiness(exc.entry_id, models=exc.assets,
            target={'root': root, 'owner': 'astrid'})) from exc
    except EngineError as exc:
        if exc.code == 'engine_not_installed':
            # A deleted locked model can fail installed health before workflow
            # compilation. Preserve the refusal, but name the actual asset fix.
            from .asset_gaps import asset_readiness
            from .asset_alignment import selected_target
            from .model_downloader import _root_and_destination, _validate_model
            try:
                selected = selected_target()
                missing = []
                for model in selected.locked_models:
                    relative, _url = _validate_model(model)
                    _, path = _root_and_destination(str(selected.models_root), relative)
                    if not path.exists():
                        missing.append(model.destination)
            except (EngineError, OSError, ValueError):
                missing = []
            if missing:
                raise GenerationReadinessError(asset_readiness(None, models=missing,
                    target={'root': str(selected.root), 'endpoint': selected.url, 'owner': 'astrid'})) from exc
        fix = _managed_install_fix()
        raise generation_error(
            ValueError('The installed managed ComfyUI engine is not ready; repair its installation. '
                       + _gpu_guidance()),
            code='engine_not_found', details={'field': 'target', 'engine_code': exc.code},
            fixes=(() if fix is None else (fix,)),
        ) from exc
    endpoint = installed.endpoint
    facts = {'object_info_sha256': endpoint.health.object_info_sha256}
    binding = getattr(installed, 'model_root_binding', None)
    if binding is not None:
        facts['model_root'] = binding.as_dict()
    client = ComfyTargetClient(ComfyTarget(endpoint.url, 'managed', facts))
    client.generation_target = TargetRecord(
        owner='astrid', endpoint=endpoint.url, root=root, layout='git',
        facts=facts, verified_at=endpoint.health.checked_at,
    )
    return client


def _select_generation_target(target: str, *, repo=None, entry_id=None, defer_managed=False):
    """Select and probe one Comfy target without changing machine config.

    ``None`` selects managed lifecycle; a returned client owns only HTTP I/O.
    Only unbound discovery may fall back to the existing managed lifecycle.
    A saved or transient human address remains bound when unreachable.
    """
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
    from astrid.core.generation.targets import TargetConfigError, resolve_target

    from astrid.core.generation.placement import is_runpod_placement

    if is_runpod_placement(target):
        raise AstridError(
            "RunPod generation requires an explicitly prepared target; automatic provisioning is unavailable",
            code="COMFY_TARGET_UNSUPPORTED", field="target",
            fix="Use --placement managed inside an existing prepared pod, or --placement auto locally",
        )
    try:
        selection = resolve_target(target, repo=repo)
        record = selection.record
        if record is None or record.owner == "astrid":
            if defer_managed:
                return None
            return _installed_managed_target(**({'entry_id': entry_id} if entry_id is not None else {}))
        if record.endpoint is None:
            raise AstridError(
                "Selected human ComfyUI target has no endpoint; launch it with explicit consent first",
                code="COMFY_TARGET_UNAVAILABLE", field="target",
                fix="Launch the selected ComfyUI and explicitly select its loopback URL",
            )
        url = record.endpoint
        client = ComfyTargetClient(ComfyTarget(url, "attached"))
    except GenerationReadinessError:
        raise
    except TargetConfigError as exc:
        raise AstridError(
            str(exc), code="COMFY_TARGET_INVALID", field="target",
            fix="Repair the saved ComfyUI config or explicitly select a loopback URL or managed target",
        ) from exc
    except (TypeError, ValueError) as exc:
        raise AstridError(
            str(exc), code="COMFY_TARGET_INVALID", field="target",
            fix="Use --placement auto, --placement managed, or http://127.0.0.1:<port>",
        ) from exc
    try:
        facts = client.request_json("/system_stats", timeout=1.0)
        if not isinstance(facts, dict) or not isinstance(facts.get("system"), dict):
            raise ValueError("target did not return ComfyUI system facts")
    except (OSError, ValueError, TimeoutError) as exc:
        if not selection.bound:
            return None
        raise AstridError(
            f"ComfyUI target {url} is unavailable: {exc}",
            code="COMFY_TARGET_UNAVAILABLE", field="target",
            fix=f"Start ComfyUI at {url}, or use --placement managed with an installed ComfyUI runtime",
            state_snapshot={'target': record.as_dict()},
        ) from exc
    client = ComfyTargetClient(ComfyTarget(client.target.url, "attached", {**record.facts, **facts}))
    from dataclasses import replace
    from datetime import datetime, timezone
    client.generation_target = replace(record, facts={**record.facts, **facts},
        verified_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
    return client


def select_generation_target(target: str, *, repo=None, runpod_owner: RunPodGenerationOwner | None = None,
                             entry_id: str | None = None, mode: str | None = None,
                             defer_managed: bool = False):
    """Probe the bound target, optionally leaving managed binding to the host."""
    try:
        from astrid.core.generation.placement import is_runpod_placement
        if is_runpod_placement(target) and runpod_owner is not None:
            return call_runpod_owner(runpod_owner.validation_target)
        return _select_generation_target(target, repo=repo, defer_managed=defer_managed,
            **({'entry_id': entry_id} if entry_id is not None else {}))
    except AstridError as exc:
        if isinstance(exc, GenerationReadinessError) and exc.code == 'engine_not_found':
            envelope = exc.to_envelope()
            envelope['entry'] = envelope['entry'] or (
                {'id': entry_id, 'engine': 'comfyui', 'mode': mode} if entry_id else None)
            if mode == 'tts':
                envelope['issues'] = [{**issue, 'message': issue['message'].replace(
                    _gpu_guidance(), _gpu_guidance(mode))}
                                      for issue in envelope['issues']]
            raise GenerationReadinessError(envelope) from exc
        if isinstance(exc, GenerationReadinessError) and any(
                fix.get('scope') == 'attached_target' for fix in exc.readiness.get('fixes', [])):
            # Attached advice names the person's own ComfyUI; re-wrapping it would add align, which writes to the managed engine.
            raise
        if isinstance(exc, GenerationReadinessError) and entry_id and all(
                issue['code'] in {'missing_model', 'missing_node_pack'} for issue in exc.readiness['issues']):
            from .asset_gaps import asset_readiness
            raise GenerationReadinessError(asset_readiness(entry_id,
                nodes=[issue['details']['node'] for issue in exc.readiness['issues'] if issue['code'] == 'missing_node_pack'],
                models=[issue['details']['model'] for issue in exc.readiness['issues'] if issue['code'] == 'missing_model'],
                target=exc.readiness['target'])) from exc
        snapshot = exc.state_snapshot.get('target')
        selector = (snapshot or {}).get('endpoint') or target
        argv = ['astrid', 'generate', 'targets', '--placement', selector]
        raise generation_error(exc, target=snapshot,
            fixes=[{'argv': argv, 'scope': 'read_only', 'details': {'guidance': exc.fix}}]) from exc


def require_generation_target_readiness(entry: Any, mode: str, client: Any) -> None:
    """Require a selected engine without probing the Agent Python environment."""
    from astrid.core.contracts.errors import AstridError

    readiness = check_local_generation_readiness(
        entry, mode, require_managed_runtime=client is None, legacy_local=False,
    )
    if not readiness.ready:
        error = AstridError(
            readiness.reason, code="COMFY_TARGET_NOT_READY", field="target",
            fix=(readiness.recovery_command + "; use --placement http://127.0.0.1:8188 "
                 "when your existing ComfyUI is running there"),
        )
        raise generation_error(error, entry={'id': entry.id, 'engine': 'comfyui', 'mode': mode},
            fixes=readiness.fixes or [
                {'argv': ['astrid', 'generate', 'targets', '--placement', 'http://127.0.0.1:8188'],
                 'scope': 'read_only', 'details': {'guidance': readiness.recovery_command}}])
