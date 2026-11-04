"""Internal Runtime snapshot handoff across the owned Python process tree.

The inherited environment anchors the binding file, not the file's own claims.
Only GenericPackHost mints it after Runtime claim and CAS materialization.
Public request JSON and backend configuration cannot carry this authority.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

SOURCE_ENV = "ASTRID_VERIFIED_TIMELINE_SOURCE"
RESERVED_SOURCE_KEYS = frozenset({"verified_source", "timeline_source", SOURCE_ENV})
_MINT = object()


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> str:
    """Encoded form, so True/1 and 2/2.0 (equal in Python, different on the wire) differ."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def reject_source_keys(value: Mapping[str, Any]) -> None:
    if RESERVED_SOURCE_KEYS.intersection(value):
        raise ValueError("verified timeline source is host-owned")


@dataclass(frozen=True, init=False)
class VerifiedTimelineSource:
    """Immutable anchor to the exact admitted source and prepared input pair."""

    _anchor: str

    def __init__(self, anchor: str, *, _mint: object = None) -> None:
        if _mint is not _MINT:
            raise ValueError("verified timeline source is host-owned")
        object.__setattr__(self, "_anchor", anchor)

    def read(self) -> dict[str, Any]:
        path, digest = json.loads(self._anchor)
        data = Path(path).read_bytes()
        if _digest(data) != digest:
            raise ValueError("verified source binding changed")
        return json.loads(data)

    def validate(self, timeline_path: str | Path, assets_path: str | Path | None) -> None:
        binding = self.read()
        for key, path in (("timeline", timeline_path), ("registry", assets_path)):
            expected = binding["prepared"][key]
            if path is None or str(Path(path).resolve()) != expected["path"]:
                raise ValueError(f"verified source prepared {key} path differs")
            if _digest(Path(path).read_bytes()) != expected["digest"]:
                raise ValueError(f"verified source prepared {key} bytes differ")

    def validate_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        reject_source_keys(snapshot)
        original = self.read()["snapshot"]
        for key in ("config", "registry"):
            if _canonical(snapshot.get(key)) != _canonical(original.get(key)):
                raise ValueError(f"verified source capture {key} differs")
        if (_canonical(snapshot.get("metadata", {}).get("capture_authority"))
                != _canonical(original.get("metadata", {}).get("capture_authority"))):
            raise ValueError("verified source capture authority differs")
        if (_canonical(snapshot.get("metadata", {}).get("host_transition_contract"))
                != _canonical(original.get("metadata", {}).get("host_transition_contract"))):
            raise ValueError("verified source host transition contract differs")


class PreparedTimelineInputs(dict[str, Any]):
    """Keep authority outside the public executor input namespace."""

    timeline_source: VerifiedTimelineSource | None = None

    @classmethod
    def prepare(cls, input_spec: Mapping[str, Any], values: Mapping[str, Any]) -> PreparedTimelineInputs:
        reject_source_keys(input_spec)
        if isinstance(input_spec.get("params"), Mapping):
            reject_source_keys(input_spec["params"])
        reject_source_keys(values)
        for config in (values["backend_config"].values() if isinstance(values.get("backend_config"), Mapping) else ()):
            if isinstance(config, Mapping):
                reject_source_keys(config)
        return cls(values)

    def prepare_snapshot(
        self, input_spec: Mapping[str, Any], spec: Mapping[str, Any], snapshot: Any,
        hydrate_snapshot: Callable[[Mapping[str, Any], str], Mapping[str, Any]],
    ) -> Any:
        # Only Runtime-admitted immutable refs can mint the internal source.
        # Inline snapshots and caller provenance remain ordinary native inputs.
        self._source_ref: Any = input_spec.get("timeline_snapshot_ref") or self.get("timeline_snapshot_ref")
        self._source_authority: Any = self.get("timeline_authority")
        if (isinstance(input_spec.get("timeline_snapshot_ref"), Mapping)
                and isinstance(self.get("timeline_snapshot_ref"), Mapping)
                and input_spec["timeline_snapshot_ref"] != self["timeline_snapshot_ref"]):
            raise ValueError("timeline snapshot reference differs from admitted context")
        self._source_snapshot = snapshot
        context = spec.get("authority_context") or input_spec.get("authority_context")
        if (isinstance(context, Mapping) and isinstance(context.get("capture_snapshot_ref"), Mapping)
                and context["capture_snapshot_ref"].get("source_kind") in {"revision", "working_copy"}):
            self._source_ref = context["capture_snapshot_ref"]
            self._source_snapshot = hydrate_snapshot(self._source_ref, "capture_snapshot")
            self._source_authority = self._source_snapshot.get("metadata", {}).get("capture_authority")
            snapshot = self._source_snapshot
            filmstrip = self.get("filmstrip_authority")
            if isinstance(filmstrip, str):
                if json.loads(filmstrip).get("capture_snapshot") != self._source_snapshot:
                    raise ValueError("capture snapshot differs from admitted context")
        self._canonical_ref = isinstance(self._source_ref, Mapping) and self._source_ref.get("source_kind") in {"revision", "working_copy"}
        if self._canonical_ref:
            if not isinstance(self._source_authority, Mapping) or not isinstance(self._source_snapshot, Mapping):
                raise ValueError("verified source requires admitted authority and snapshot")
            if any(name in self for name in ("timeline", "assets_registry")):
                raise ValueError("verified source prepared inputs cannot be caller replaced")
            if "timeline_snapshot" in self or "timeline_snapshot" in input_spec:
                raise ValueError("verified source snapshot cannot be caller replaced")
        return snapshot

    def mint_source(self, attempt: Path) -> PreparedTimelineInputs:
        if self._canonical_ref:
            try:
                self.timeline_source = mint_prepared_source(
                    ref=self._source_ref, authority=self._source_authority, snapshot=self._source_snapshot,
                    timeline_path=self["timeline"], assets_path=self["assets_registry"], attempt=attempt,
                )
            except KeyError as exc:
                raise ValueError(str(exc)) from exc
        return self

    @staticmethod
    def handoff_source(inputs: Mapping[str, Any], env: dict[str, str]) -> None:
        # This value is an attribute of host-owned prepared inputs, never a
        # manifest input or binding.env override. Clear any ambient handoff.
        env.pop(SOURCE_ENV, None)
        source = getattr(inputs, "timeline_source", None)
        if source is not None:
            source.validate(inputs["timeline"], inputs["assets_registry"])
            env[SOURCE_ENV] = source._anchor


def mint_prepared_source(
    *, ref: Mapping[str, Any], authority: Mapping[str, Any], snapshot: Mapping[str, Any],
    timeline_path: str | Path, assets_path: str | Path, attempt: Path,
) -> VerifiedTimelineSource:
    """Called only at the Runtime-admitted host materialization boundary."""
    for name, field in (("project_id", "project_id"), ("timeline_id", "timeline_id"),
                        ("revision_id", "parent_revision_id")):
        if not isinstance(ref.get(name), str) or not ref[name] or ref[name] != authority.get(field):
            raise ValueError(f"verified source {name} differs from admitted authority")
    if ref.get("source_kind") not in {"revision", "working_copy"}:
        raise ValueError("verified source requires a revision or working copy")
    revision_digest = ref.get("revision_digest")
    if (not isinstance(revision_digest, str) or not revision_digest.startswith("sha256:")
            or len(revision_digest) != 71 or any(c not in "0123456789abcdef" for c in revision_digest[7:])):
        raise ValueError("verified source requires an admitted revision digest")
    if ref["source_kind"] == "revision":
        if revision_digest != authority.get("parent_content_digest"):
            raise ValueError("verified source revision digest differs")
    else:
        marker = snapshot.get("metadata", {}).get("working_copy", {})
        base = marker.get("base_parent", {})
        expected = {"candidate_digest": marker.get("candidate_digest"),
                    "publication_digest": marker.get("publication_digest"),
                    "base_revision_id": base.get("revision_id"),
                    "base_revision_digest": base.get("content_digest")}
        if (marker.get("candidate_parent_revision_id") != ref["revision_id"]
                or expected != ref.get("working_copy")
                or revision_digest != expected["candidate_digest"]):
            raise ValueError("verified source working copy differs")
    if authority.get("snapshot_digest") is not None and authority["snapshot_digest"] != ref.get("digest"):
        raise ValueError("verified source snapshot digest differs from Runtime authority")
    # The transition contract is minted by capture producers only after they
    # have projected the exact typed parent/shot/internal closure (saved head
    # or frozen authoring candidate). Source kind alone is not enough: native
    # TimelineConfig revisions and working copies also arrive through refs.
    expansion = authority.get("expansion")
    transition_contract = None
    host_contract = snapshot.get("metadata", {}).get("host_transition_contract")
    # The ordinary managed-render producer has already validated the exact
    # typed closure, but its renderer-facing authority intentionally carries a
    # compact expansion summary. Its host-only marker is bound to the immutable
    # saved revision here. This branch never accepts working-copy refs; those
    # require the stronger candidate/base digest contract below.
    managed_saved_closure = (
        host_contract == "runtime-parent-closure/v1"
        and ref.get("source_kind") == "revision"
        and authority.get("parent_revision_id") == ref.get("revision_id")
        and authority.get("parent_content_digest") == revision_digest
        and isinstance(expansion, Mapping)
        and all(isinstance(expansion.get(key), list) for key in ("children", "shots", "occurrences"))
        and isinstance(snapshot.get("config"), Mapping)
        and isinstance(snapshot.get("registry"), Mapping)
    )
    if managed_saved_closure:
        transition_contract = "runtime-parent-closure/v1"
    if isinstance(expansion, Mapping) and host_contract is not None:
        common_typed_closure = (
            host_contract == "runtime-parent-closure/v1"
            and expansion.get("parent_revision_id") == ref.get("revision_id")
            and authority.get("parent_revision_id") == ref.get("revision_id")
            and expansion.get("parent_content_digest") == authority.get("parent_content_digest")
            and isinstance(expansion.get("occurrences"), list)
            and isinstance(expansion.get("shots"), list)
            and isinstance(expansion.get("children"), list)
        )
        saved_typed_closure = (
            ref.get("source_kind") == "revision"
            and expansion.get("parent_content_digest") == revision_digest
        )
        working_marker = expansion.get("working_copy")
        base_parent = working_marker.get("base_parent") if isinstance(working_marker, Mapping) else None
        ref_working_copy = ref.get("working_copy")
        snapshot_working_copy = snapshot.get("metadata", {}).get("working_copy")
        typed_working_copy = (
            ref.get("source_kind") == "working_copy"
            and isinstance(working_marker, Mapping)
            and working_marker == snapshot_working_copy
            and isinstance(base_parent, Mapping)
            and isinstance(ref_working_copy, Mapping)
            and working_marker.get("candidate_parent_revision_id") == ref.get("revision_id")
            and working_marker.get("candidate_digest") == revision_digest
            and ref_working_copy.get("candidate_digest") == working_marker.get("candidate_digest")
            and ref_working_copy.get("publication_digest") == working_marker.get("publication_digest")
            and ref_working_copy.get("base_revision_id") == base_parent.get("revision_id")
            and ref_working_copy.get("base_revision_digest") == base_parent.get("content_digest")
        )
        if common_typed_closure and (saved_typed_closure or typed_working_copy):
            transition_contract = "runtime-parent-closure/v1"
    prepared = {}
    for key, path in (("timeline", timeline_path), ("registry", assets_path)):
        resolved = Path(path).resolve(strict=True)
        resolved.relative_to(attempt.resolve())
        prepared[key] = {"path": str(resolved), "digest": _digest(resolved.read_bytes())}
    binding = {"ref": dict(ref), "authority": dict(authority), "snapshot": dict(snapshot), "prepared": prepared}
    if transition_contract is not None:
        binding["transition_contract"] = transition_contract
    data = json.dumps(binding, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    path = attempt.resolve() / "inputs" / "verified-timeline-source.json"
    path.write_bytes(data)
    path.chmod(0o600)
    return VerifiedTimelineSource(json.dumps([str(path), _digest(data)]), _mint=_MINT)


def inherited_source(timeline_path: str | Path, assets_path: str | Path | None = None) -> VerifiedTimelineSource | None:
    anchor = os.environ.get(SOURCE_ENV)
    if anchor is None:
        return None
    source = VerifiedTimelineSource(anchor, _mint=_MINT)
    source.validate(timeline_path, assets_path)
    return source


def source_for_timeline(timeline_path: str | Path) -> VerifiedTimelineSource | None:
    """Serializer entry: require the same pair even if only one path was passed."""
    anchor = os.environ.get(SOURCE_ENV)
    if anchor is None:
        return None
    source = VerifiedTimelineSource(anchor, _mint=_MINT)
    source.validate(timeline_path, source.read()["prepared"]["registry"]["path"])
    return source
