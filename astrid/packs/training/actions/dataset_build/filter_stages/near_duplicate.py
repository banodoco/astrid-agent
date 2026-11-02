"""Near-duplicate media filter using lightweight perceptual hashes."""

from __future__ import annotations

from astrid.pack_api import pack_root

PACK_ROOT = pack_root('training')

import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any


from ..acquisition import string_set
from ....shared.interfaces import FilterResult
from ._common import (
    build_filter_stats,
    increment_reason,
    pass_item,
    reject_item,
    resolve_media_path,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]


class NearDuplicateFilter:
    def __init__(self, *, runner: Runner = subprocess.run, repo_root: Path = PACK_ROOT, **_: Any) -> None:
        self._runner = runner
        self._repo_root = repo_root

    @property
    def stage_id(self) -> str:
        return "near_duplicate_filter"

    @property
    def stage_order(self) -> int:
        return 7

    def apply(self, items: list[dict[str, Any]], state: dict[str, Any], config: dict[str, Any]) -> FilterResult:
        started = time.perf_counter()
        sample_count = max(1, int(config.get("sample_count", 3)))
        threshold = max(0, int(config.get("hamming_threshold", 3)))
        excluded_candidate_ids, excluded_source_ids, excluded_media_hashes = _exclusions(config, state)
        passed: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        reasons: dict[str, int] = {}
        warnings: list[str] = []
        seen: list[tuple[dict[str, Any], tuple[int, ...]]] = []

        for item in items:
            item_id = str(item.get("item_id") or "")
            source_id = str(item.get("source_id") or "")
            content_hash = str(item.get("content_hash") or "")
            exclusion_reason = _exclusion_reason(item_id, source_id, content_hash, excluded_candidate_ids, excluded_source_ids, excluded_media_hashes)
            if exclusion_reason:
                rejected_item = reject_item(item, self.stage_id, reason=exclusion_reason)
                rejected.append(rejected_item)
                increment_reason(reasons, exclusion_reason)
                continue

            try:
                signature = _signature(item, config, sample_count=sample_count, runner=self._runner, repo_root=self._repo_root)
            except Exception as exc:  # noqa: BLE001 - missing media/frame decode should be a warning, not a pipeline crash
                warning = f"near_duplicate_hash_unavailable:{item_id or source_id or 'unknown'}:{type(exc).__name__}"
                warnings.append(warning)
                passed.append(pass_item(item, self.stage_id, reason="hash_unavailable", extra={"warning": warning}))
                continue

            duplicate_of = _duplicate_of(signature, seen, threshold=threshold)
            if duplicate_of is not None:
                duplicate_item, distance = duplicate_of
                reason = "near_duplicate"
                rejected_item = reject_item(
                    item,
                    self.stage_id,
                    reason=reason,
                    score=float(distance),
                    extra={
                        "duplicate_of_item_id": duplicate_item.get("item_id"),
                        "duplicate_of_source_id": duplicate_item.get("source_id"),
                        "hamming_distance": distance,
                    },
                )
                rejected.append(rejected_item)
                increment_reason(reasons, reason)
                continue

            passed_item = pass_item(item, self.stage_id, reason="", extra={"perceptual_hashes": [f"{value:016x}" for value in signature]})
            passed.append(passed_item)
            seen.append((passed_item, signature))

        stats = build_filter_stats(
            stage_id=self.stage_id,
            stage_order=self.stage_order,
            items_in=len(items),
            items_passed=len(passed),
            items_rejected=len(rejected),
            rejection_reasons=reasons,
            warnings=warnings,
            started=started,
        )
        return FilterResult(passed=passed, rejected=rejected, stats=stats)


def _signature(item: Mapping[str, Any], config: Mapping[str, Any], *, sample_count: int, runner: Runner, repo_root: Path) -> tuple[int, ...]:
    fixture_hashes = _fixture_hashes(item, config)
    if fixture_hashes:
        return tuple(fixture_hashes[:sample_count])
    frame_dir = _frame_dir(item, config, repo_root=repo_root)
    frame_dir.mkdir(parents=True, exist_ok=True)
    media_path = resolve_media_path(item, repo_root=repo_root, required=True, must_exist=True)
    if media_path is None:
        raise ValueError("item missing media_path")
    pattern = frame_dir / "frame_%03d.pgm"
    runner(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(media_path),
            "-vf",
            "fps=1,scale=8:8:flags=bilinear,format=gray",
            "-frames:v",
            str(sample_count),
            str(pattern),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    hashes = [_average_hash_pgm(path) for path in sorted(frame_dir.glob("frame_*.pgm"))[:sample_count]]
    if not hashes:
        raise ValueError("ffmpeg did not produce sampled frames")
    return tuple(hashes)


def _fixture_hashes(item: Mapping[str, Any], config: Mapping[str, Any]) -> list[int]:
    item_id = str(item.get("item_id") or item.get("source_id") or "")
    fixture_hashes = config.get("fixture_hashes")
    raw: Any = None
    if isinstance(fixture_hashes, Mapping):
        raw = fixture_hashes.get(item_id)
    if raw is None:
        metadata = item.get("source_metadata")
        if isinstance(metadata, Mapping):
            raw = metadata.get("perceptual_hashes")
    if raw is None:
        raw = item.get("perceptual_hashes")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    hashes: list[int] = []
    for value in raw:
        if isinstance(value, int):
            hashes.append(value)
        elif isinstance(value, str):
            hashes.append(int(value, 16))
    return hashes


def _frame_dir(item: Mapping[str, Any], config: Mapping[str, Any], *, repo_root: Path) -> Path:
    out_dir = config.get("out_dir", "runs/dataset-build/near-duplicate")
    path = Path(str(out_dir)).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    item_id = str(item.get("item_id") or item.get("source_id") or "item")
    return path.resolve() / item_id


def _average_hash_pgm(path: Path) -> int:
    width, height, pixels = _read_pgm(path)
    if width != 8 or height != 8:
        raise ValueError(f"{path} must be an 8x8 PGM frame")
    average = sum(pixels) / len(pixels)
    value = 0
    for pixel in pixels:
        value = (value << 1) | (1 if pixel >= average else 0)
    return value


def _read_pgm(path: Path) -> tuple[int, int, list[int]]:
    data = path.read_bytes()
    tokens: list[bytes] = []
    index = 0
    while len(tokens) < 4 and index < len(data):
        while index < len(data) and data[index:index + 1].isspace():
            index += 1
        if index < len(data) and data[index:index + 1] == b"#":
            while index < len(data) and data[index:index + 1] not in {b"\n", b"\r"}:
                index += 1
            continue
        start = index
        while index < len(data) and not data[index:index + 1].isspace():
            index += 1
        if start != index:
            tokens.append(data[start:index])
    if len(tokens) < 4 or tokens[0] != b"P5":
        raise ValueError(f"{path} is not a binary PGM file")
    while index < len(data) and data[index:index + 1].isspace():
        index += 1
    width = int(tokens[1])
    height = int(tokens[2])
    max_value = int(tokens[3])
    if max_value <= 0 or max_value > 255:
        raise ValueError(f"{path} has unsupported max value {max_value}")
    pixels = list(data[index:index + width * height])
    if len(pixels) != width * height:
        raise ValueError(f"{path} has incomplete pixel data")
    return width, height, pixels


def _duplicate_of(signature: tuple[int, ...], seen: list[tuple[dict[str, Any], tuple[int, ...]]], *, threshold: int) -> tuple[dict[str, Any], int] | None:
    for item, prior_signature in seen:
        distance = _signature_distance(signature, prior_signature)
        if distance <= threshold:
            return item, distance
    return None


def _signature_distance(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    distances = [(a ^ b).bit_count() for a, b in zip(left, right)]
    return min(distances) if distances else 64


def _exclusions(config: Mapping[str, Any], state: Mapping[str, Any]) -> tuple[set[str], set[str], set[str]]:
    request = config.get("acquisition_request")
    if not isinstance(request, Mapping):
        request = {}
    candidate_ids = string_set(config.get("exclude_candidate_ids"), request.get("exclude_candidate_ids"), state.get("accepted_candidate_ids"))
    source_ids = string_set(config.get("exclude_source_ids"), request.get("exclude_source_ids"), state.get("processed_source_ids"))
    media_hashes = string_set(config.get("exclude_media_hashes"), request.get("exclude_media_hashes"), state.get("accepted_media_hashes"))
    return candidate_ids, source_ids, media_hashes


def _exclusion_reason(
    item_id: str,
    source_id: str,
    content_hash: str,
    candidate_ids: set[str],
    source_ids: set[str],
    media_hashes: set[str],
) -> str:
    if item_id and item_id in candidate_ids:
        return "excluded_candidate_id"
    if source_id and source_id in source_ids:
        return "excluded_source_id"
    if content_hash and content_hash in media_hashes:
        return "excluded_media_hash"
    return ""
