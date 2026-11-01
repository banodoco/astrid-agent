"""Submit an exact selected-media swap through the durable timeline session."""
from __future__ import annotations

import re
from collections.abc import Mapping

from astrid.core.receipts.contract import CommandReceipt

from .contracts import DomainResult, ErrorObject

_SELECTION_KEYS = {"asset", "generation_id", "variant_id", "object_id", "catalog_digest"}
_ID = re.compile(r"(?!.*::)[^/]+\Z")
_NATIVE_COMMAND_KEYS = {
    "schema_version", "op", "version", "select", "args", "intent",
    "resolved", "reads", "selections", "bindings", "created", "expect",
    "authorship",
}


def _failure(message, *, key, details=None, code="validation_error"):
    return DomainResult.failure(
        ErrorObject(code, message, details or {}),
        idempotency_key=key if isinstance(key, str) else "",
    )


def _plain(value):
    if isinstance(value, Mapping) and set(value) >= {"data", "receipt"}:
        value = value["data"]
    return dict(value) if isinstance(value, Mapping) else value


def _verified(transport, project_id, selection):
    if not isinstance(selection, Mapping) or set(selection) != _SELECTION_KEYS:
        raise ValueError("native media replacement requires one complete immutable media selection")
    if not all(isinstance(value, str) and value for value in selection.values()):
        raise ValueError("native media replacement requires nonempty selection identities")
    reader = getattr(transport, "get_project_verified_media", None)
    if not callable(reader):
        raise ValueError("Runtime does not expose exact verified media for native replacement")
    verified = _plain(reader(project_id, dict(selection)))
    if not isinstance(verified, Mapping) or verified.get("selection") != dict(selection):
        raise ValueError("Runtime verification does not match the immutable media selection")
    return dict(selection), dict(verified)


def _target_pin(bundle, owner, clip_id):
    from .timeline._command_state import State

    state = State(bundle)
    clip = state.entity(owner + "/clip:" + clip_id)
    if not isinstance(clip, Mapping) or not isinstance(clip.get("asset"), str):
        raise ValueError("selected clip has no exact authored asset identity")
    binding = state.entity(owner + "/asset:" + clip["asset"])
    if not isinstance(binding, Mapping):
        raise ValueError("selected clip has no exact authored managed-media binding")
    values = {
        "asset": clip["asset"],
        "generation_id": binding.get("generationId", binding.get("generation_id")),
        "variant_id": binding.get("variantId", binding.get("variant_id")),
        "object_id": binding.get("media_id", binding.get("object_id")),
    }
    if not all(isinstance(value, str) and value for value in values.values()):
        raise ValueError("selected clip's authored media binding is not immutable and complete")
    return values


def _pending_matches(command, *, owner, clip_id, source_object_id, old_media, media):
    if not isinstance(command, Mapping):
        return False
    select = owner + "/clip:" + clip_id
    expected = {
        "schema_version": 1,
        "version": "1",
        "select": select,
        "op": "clip.swap-media" if owner == "parent" else "occurrence.swap-media",
        "args": {"old_media": dict(old_media), "media": dict(media)},
        "intent": {"ownership": "parent" if owner == "parent" else "occurrence"},
    }
    if (set(command) != _NATIVE_COMMAND_KEYS
            or any(command.get(key) != value for key, value in expected.items())):
        return False
    if media.get("object_id") != source_object_id:
        return False

    # These are recorder claims, but they are still part of the original
    # command identity.  A caller retry must not turn a changed draft into a
    # request for a different target or a different native operation.
    if command.get("resolved") != [select]:
        return False
    bindings = command.get("bindings")
    binding_keys = {"media.track", "media.target"}
    if owner != "parent":
        binding_keys |= {
            "media.owner", "collection.owner:collection.clips",
            "collection.owner:collection.tracks",
        }
    if not isinstance(bindings, Mapping) or set(bindings) != binding_keys:
        return False
    if bindings.get("media.target") != select:
        return False
    if (not isinstance(bindings.get("media.track"), str)
            or not re.fullmatch(re.escape(owner) + r"/track:[^/]+", bindings["media.track"])):
        # The exact track suffix is authored metadata and is checked by the
        # persisted flight body.  Here, reject malformed or retargeted claims
        # before a draft-only retry can flush anything.
        return False
    if owner != "parent" and any(
        bindings.get(key) != owner for key in (
            "media.owner", "collection.owner:collection.clips",
            "collection.owner:collection.tracks",
        )
    ):
        return False

    created = command.get("created")
    if not isinstance(created, list) or len(set(created)) != len(created):
        return False
    if any(not isinstance(ref, str) or not ref.startswith(owner + "/asset:") for ref in created):
        return False
    expect = command.get("expect")
    authorship = command.get("authorship")
    if not isinstance(expect, Mapping) or not isinstance(authorship, Mapping):
        return False
    target_expect = select + "::asset"
    if (expect.get(target_expect) != {"state": "present", "value": old_media.get("asset")}
            or authorship.get(target_expect) != "direct"):
        return False
    expected_expect_keys = {target_expect, *(created_ref + "::" for created_ref in created)}
    if set(expect) != expected_expect_keys:
        return False
    if set(authorship) != set(expect) or any(value != "direct" for value in authorship.values()):
        return False
    if any(value != {"state": "absent"} for key, value in expect.items() if key != target_expect):
        return False
    return isinstance(command.get("reads"), list) and isinstance(command.get("selections"), list)


def _domain(result, key):
    data = result.get("data") if isinstance(result, Mapping) else None
    receipt = result.get("receipt") if isinstance(result, Mapping) else None
    if not isinstance(data, Mapping) or not isinstance(receipt, Mapping):
        return _failure("Runtime returned an invalid native timeline acknowledgement", key=key,
                        code="protocol_error")
    try:
        receipt = CommandReceipt.from_dict(receipt)
    except (TypeError, ValueError) as exc:
        return _failure("Runtime returned an invalid native timeline receipt", key=key,
                        details={"reason": str(exc)}, code="protocol_error")
    return DomainResult.success(
        dict(data), receipt=receipt,
        idempotency_key=key if key is not None else receipt.idempotency_key,
    )


def replace_selected_media(
    timelines,
    project,
    ref,
    *,
    occurrence_id,
    clip_id,
    source_object_id,
    expected_head,
    expected_publication=None,
    old_media=None,
    media=None,
    idempotency_key,
):
    """Flush one exact native media swap, preserving the original outbox intent."""
    key = idempotency_key
    if key is not None and (not isinstance(key, str) or not key):
        return _failure("native media replacement requires an idempotency key", key=key or "")
    if not all(isinstance(value, str) and value.strip()
               for value in (project, ref, clip_id, source_object_id, expected_head,
                             expected_publication)):
        return _failure(
            "selected native media replacement requires exact project, target, head and publication",
            key=key,
        )
    if occurrence_id is not None and (not isinstance(occurrence_id, str)
                                      or not _ID.fullmatch(occurrence_id)):
        return _failure("native media replacement requires an exact occurrence id", key=key)
    if not _ID.fullmatch(clip_id):
        return _failure("native media replacement requires an exact clip id", key=key)
    if not isinstance(old_media, Mapping) or not isinstance(media, Mapping):
        return _failure(
            "selected native media replacement requires both exact media selections", key=key
        )
    if media.get("object_id") != source_object_id:
        return _failure("new media selection does not match source_object_id", key=key)
    if old_media.get("object_id") == media.get("object_id"):
        return _failure("native media replacement requires a changed managed-media object", key=key)

    try:
        from types import SimpleNamespace

        from .timeline.native_occurrence_media_commands import (
            capture_occurrence_swap_media,
            capture_parent_swap_media,
        )
        from .timeline.sync import TimelineSession

        client = SimpleNamespace(
            timelines=timelines,
            _remote=SimpleNamespace(_transport=timelines._client),
        )
        transport = timelines._client
        session = TimelineSession.open(
            project,
            ref,
            client=client,
            base_revision_id=expected_head,
            base_publication_id=expected_publication,
        )
        state = session._state()
        owner = "parent" if occurrence_id is None else "occurrence:" + occurrence_id
        pending = state.get("pending") or []
        if pending or state.get("flight") is not None:
            if (len(pending) != 1
                    or not _pending_matches(
                        pending[0], owner=owner, clip_id=clip_id,
                        source_object_id=source_object_id,
                        old_media=old_media, media=media)):
                raise ValueError("pending native replacement does not match this exact request")
            if state.get("flight") is not None:
                flight_key = state["flight"].get("key")
                return _domain(session.flush(), flight_key)
            return _domain(session.flush(idempotency_key=key), key)

        bundle = session._bundle(state)
        target = _target_pin(bundle, owner, clip_id)
        old_pin, old_verified = _verified(transport, session.project_id, old_media)
        new_pin, new_verified = _verified(transport, session.project_id, media)
        if any(old_pin[key] != target[key] for key in target):
            raise ValueError("provided old media selection does not match the authored clip")

        select = owner + "/clip:" + clip_id
        if owner == "parent":
            command = capture_parent_swap_media(
                bundle, select, old_pin, new_pin,
                verified_media=[old_verified, new_verified],
            )
        else:
            command = capture_occurrence_swap_media(
                bundle, select, old_pin, new_pin,
                verified_media=[old_verified, new_verified],
            )
        return _domain(
            session.commit(
                command,
                idempotency_key=key,
                verified_media=[old_verified, new_verified],
            ),
            key,
        )
    except Exception as exc:  # noqa: BLE001 - preserve the DomainResult boundary
        details = getattr(exc, "details", {})
        if not isinstance(details, Mapping):
            details = {}
        return _failure(
            str(getattr(exc, "message", exc)),
            key=key,
            details=dict(details),
            code=getattr(exc, "code", "validation_error"),
        )
