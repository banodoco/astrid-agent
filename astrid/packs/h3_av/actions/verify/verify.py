"""Fail-closed verification of H3 candidate custody and preservation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from astrid.packs.h3_av.shared.compose import (
    CompositionError,
    _audio_witness,
    _continuation_timing,
    _is_source_free_generation,
    _probe,
    _sample_digest,
    _spatial_video_masks,
    _validate_candidate_coverage,
    _verify_spatial_mask_complement,
)


class VerificationError(ValueError):
    """Candidate evidence is incomplete or contradictory."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_offset(preparation: Mapping[str, Any]) -> float:
    request = preparation.get("request")
    source = request.get("source") if isinstance(request, Mapping) else None
    value = source.get("range") if isinstance(source, Mapping) else None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return float(value[0])
    if isinstance(request, Mapping) and request.get("version") == 2:
        for item in request.get("media", []):
            if (
                isinstance(item, Mapping)
                and item.get("role") == "timeline"
                and item.get("modality") == "video"
            ):
                value = item.get("range")
                if isinstance(value, (list, tuple)) and len(value) == 2:
                    return float(value[0])
    return 0.0


def _interval_key(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    try:
        return float(value[0]), float(value[1])
    except (TypeError, ValueError):
        return None


def _covers_whole(intervals: Any, duration: Any) -> bool:
    if not isinstance(intervals, list):
        return False
    try:
        target = float(duration)
        ordered = sorted((_interval_key(item) for item in intervals), key=lambda item: item[0] if item else 0.0)
    except (TypeError, ValueError):
        return False
    cursor = 0.0
    for item in ordered:
        if item is None or item[0] > cursor or item[1] <= item[0]:
            return False
        cursor = max(cursor, item[1])
    return cursor >= target


def _verify_sample_evidence(
    *,
    source: Path,
    candidate: Path,
    protected: Mapping[str, Any],
    preparation: Mapping[str, Any],
) -> None:
    for stream_type in ("video", "audio"):
        expected = [_interval_key(item) for item in protected[stream_type]]
        if any(item is None for item in expected):
            raise VerificationError(f"protected {stream_type} permissions are malformed")
        entries = protected.get("_evidence", {}).get(stream_type, [])
        if not isinstance(entries, list):
            raise VerificationError(f"protected {stream_type} samples are missing")
        by_interval: dict[tuple[float, float], Mapping[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise VerificationError(f"protected {stream_type} sample evidence is malformed")
            key = _interval_key(entry.get("interval"))
            if key is None or key in by_interval:
                raise VerificationError(f"protected {stream_type} sample evidence has an invalid interval")
            by_interval[key] = entry
        for key in expected:
            assert key is not None
            entry = by_interval.get(key)
            if entry is None:
                raise VerificationError(f"protected {stream_type} sample evidence does not cover {list(key)}")
            source_hash = entry.get("source_sha256")
            candidate_hash = entry.get("candidate_sha256")
            method = entry.get("method")
            sample_count = entry.get("sample_count")
            if (
                not isinstance(source_hash, str)
                or not isinstance(candidate_hash, str)
                or not isinstance(method, str)
                or not method.strip()
                or isinstance(sample_count, bool)
                or not isinstance(sample_count, int)
                or sample_count <= 0
            ):
                raise VerificationError(f"protected {stream_type} evidence is not a positive matching sample witness")
            if method == "ffmpeg-decoded-samples-v1":
                if source_hash != candidate_hash:
                    raise VerificationError(f"protected {stream_type} evidence is not a positive matching sample witness")
                try:
                    source_actual, source_count = _sample_digest(
                        source, stream_type, _source_offset(preparation) + key[0], key[1] - key[0]
                    )
                    candidate_actual, candidate_count = _sample_digest(
                        candidate, stream_type, key[0], key[1] - key[0]
                    )
                except Exception as exc:  # noqa: BLE001 - verification boundary
                    raise VerificationError(f"protected {stream_type} samples could not be decoded") from exc
                if (
                    source_actual != source_hash
                    or candidate_actual != candidate_hash
                    or source_count <= 0
                    or candidate_count <= 0
                ):
                    raise VerificationError(f"protected {stream_type} sample digest does not match candidate")
            elif method == "ffmpeg-decoded-audio-similarity-v1":
                if stream_type != "audio":
                    raise VerificationError("audio similarity evidence is only valid for audio")
                declared_similarity = entry.get("similarity")
                if (
                    isinstance(declared_similarity, bool)
                    or not isinstance(declared_similarity, (int, float))
                    or not 0.0 <= float(declared_similarity) <= 1.0
                ):
                    raise VerificationError("protected audio evidence is not a positive matching sample witness")
                try:
                    witness = _audio_witness(
                        source=source,
                        candidate=candidate,
                        source_start=_source_offset(preparation) + key[0],
                        candidate_start=key[0],
                        duration=key[1] - key[0],
                    )
                except Exception as exc:  # noqa: BLE001 - verification boundary
                    raise VerificationError("protected audio samples could not be decoded") from exc
                if float(witness["similarity"]) < 0.985 or witness["sample_count"] <= 0:
                    raise VerificationError("protected audio evidence is not a positive matching sample witness")
            elif not isinstance(entry.get("witness_digest"), str) or not entry["witness_digest"]:
                raise VerificationError(f"protected {stream_type} non-ffmpeg evidence lacks a witness digest")


def _verify_provenance(preparation: Mapping[str, Any], composition: Mapping[str, Any]) -> None:
    if composition.get("request_digest") != preparation.get("request_digest"):
        raise VerificationError("composition request provenance does not match preparation")
    schedule = preparation.get("mask_schedule")
    if not isinstance(schedule, Mapping) or composition.get("schedule_digest") != schedule.get("digest"):
        raise VerificationError("composition schedule provenance does not match preparation")
    provenance = composition.get("provenance")
    if provenance in (None, {}):
        return
    if not isinstance(provenance, Mapping) or provenance.get("request_digest") != preparation.get("request_digest"):
        raise VerificationError("composition provenance is missing the request identity")
    assets = provenance.get("assets")
    prepared_assets = preparation.get("assets")
    if assets is not None:
        # Bundle resolution binds each asset to an attempt-local materialized
        # path. That path is deliberately absent from the portable provenance
        # recorded by the transform, and it can differ between compose and
        # verify attempts. Compare every other field so the binding, member,
        # digest, size, and status remain exact.
        def portable_asset_rows(value: Any) -> list[dict[str, Any]] | None:
            if not isinstance(value, list):
                return None
            if any(not isinstance(row, Mapping) for row in value):
                return None
            return [
                {key: item for key, item in row.items() if key != "path"}
                for row in value
            ]

        portable_assets = portable_asset_rows(assets)
        portable_prepared_assets = portable_asset_rows(prepared_assets)
        if portable_assets is None or portable_assets != portable_prepared_assets:
            raise VerificationError("composition asset provenance does not match preparation")


def verify_candidate(
    *,
    preparation: Mapping[str, Any],
    composition: Mapping[str, Any],
    source: str | Path | None = None,
    baseline: str | Path | None = None,
    candidate: str | Path | None = None,
) -> dict[str, Any]:
    """Verify artifact custody and concrete preservation evidence."""

    if preparation.get("status") != "prepared":
        raise VerificationError("preparation is not ready")
    if composition.get("status") != "composed":
        raise VerificationError("composition is not settled")
    _verify_provenance(preparation, composition)
    candidate_info = composition.get("candidate")
    if not isinstance(candidate_info, Mapping):
        raise VerificationError("composition is missing candidate evidence")
    candidate_path = Path(candidate if candidate is not None else str(candidate_info.get("path", ""))).expanduser().resolve()
    if not candidate_path.is_file():
        raise VerificationError(f"candidate is missing: {candidate_path}")
    actual_digest = _sha256(candidate_path)
    if candidate_info.get("sha256") != actual_digest:
        raise VerificationError("candidate digest does not match composition evidence")
    if "size" in candidate_info and candidate_info.get("size") != candidate_path.stat().st_size:
        raise VerificationError("candidate size does not match composition evidence")

    if _probe(candidate_path) is not None:
        try:
            actual_coverage = _validate_candidate_coverage(
                preparation=preparation,
                candidate=candidate_path,
            )
        except CompositionError as exc:
            raise VerificationError(str(exc)) from exc
        declared_coverage = composition.get("coverage")
        declared_candidate = (
            declared_coverage.get("candidate")
            if isinstance(declared_coverage, Mapping)
            else None
        )
        # Paths are observational attempt-local details, never identity. All
        # measured coverage fields still have to match the settled evidence.
        def portable_coverage(value):
            if not isinstance(value, Mapping):
                return value
            return {
                key: {field: item for field, item in entry.items() if field != "path"}
                if key in {"video", "audio"} and isinstance(entry, Mapping) else entry
                for key, entry in value.items()
            }

        if portable_coverage(declared_candidate) != portable_coverage(actual_coverage):
            raise VerificationError("candidate coverage does not match composition evidence")
    else:
        try:
            continuation = _continuation_timing(preparation)
        except CompositionError as exc:
            raise VerificationError(str(exc)) from exc
        if continuation is not None or _is_source_free_generation(preparation):
            raise VerificationError("H3 candidate is not decodable audiovisual media")

    schedule = preparation["mask_schedule"]
    protected_video = schedule["video"]["protected_intervals"]
    protected_audio = schedule["audio"]["protected_intervals"]
    protected = {"video": protected_video, "audio": protected_audio}
    evidence = composition.get("preservation_evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    if source is not None and baseline is not None:
        raise VerificationError("source and baseline are mutually exclusive")
    if source is None:
        source = baseline
    source_path: Path | None = None
    if source is not None:
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            raise VerificationError(f"source is missing: {source_path}")
    source_info = composition.get("source")
    if source_path is not None and isinstance(source_info, Mapping):
        if source_info.get("sha256") != _sha256(source_path):
            raise VerificationError("authoritative source does not match composition evidence")

    wholly_protected = (
        _covers_whole(protected_video, schedule.get("duration"))
        and _covers_whole(protected_audio, schedule.get("duration"))
    )
    if schedule.get("source_protected") or wholly_protected:
        if source_path is None:
            raise VerificationError("a source is required to verify a protected request")
        if not protected_video or not protected_audio:
            raise VerificationError("protected request has incomplete protected permissions")
        if _sha256(source_path) != actual_digest:
            raise VerificationError("fully protected candidate is not byte-identical to source")
        preservation_status = "exact_whole_file_match"
    elif protected_video or protected_audio:
        if source_path is None:
            raise VerificationError("partial preservation requires an authoritative source")
        if _probe(source_path) is None or _probe(candidate_path) is None:
            raise VerificationError("partial preservation requires decodable media, not a copied/non-media candidate")
        samples = evidence.get("protected_samples")
        if not isinstance(samples, Mapping):
            raise VerificationError("partial preservation requires protected_samples evidence")
        sample_evidence = {"video": samples.get("video", []), "audio": samples.get("audio", [])}
        _verify_sample_evidence(
            source=source_path,
            candidate=candidate_path,
            protected={**protected, "_evidence": sample_evidence},
            preparation=preparation,
        )
        preservation_status = "protected_sample_evidence"
    else:
        preservation_status = "no_protected_permissions"

    spatial_evidence: list[dict[str, Any]] = []
    spatial_masks = _spatial_video_masks(preparation)
    if spatial_masks:
        if source_path is None:
            raise VerificationError("regional video preservation requires an authoritative source")
        try:
            spatial_evidence = _verify_spatial_mask_complement(
                source=source_path,
                candidate=candidate_path,
                masks=spatial_masks,
                source_offset=_source_offset(preparation),
            )
        except CompositionError as exc:
            raise VerificationError(str(exc)) from exc
        if spatial_evidence and preservation_status == "no_protected_permissions":
            preservation_status = "protected_spatial_complement"

    return {
        "schema_version": 1,
        "kind": "h3_av_verification",
        "request_digest": preparation.get("request_digest"),
        "candidate_sha256": actual_digest,
        "preservation": {
            "status": preservation_status,
            "video": protected_video,
            "audio": protected_audio,
            "spatial_complement": spatial_evidence,
        },
        "provenance": composition.get("provenance", {}),
        "coverage": composition.get("coverage", {}).get("candidate", {}),
        "lifecycle": {"prepared": True, "composed": True, "verified": True},
        "status": "verified",
    }


__all__ = ["VerificationError", "verify_candidate"]
