"""Deterministic black/blank-frame metadata filter stage."""

from __future__ import annotations

from astrid.pack_api import pack_root

PACK_ROOT = pack_root('training')

import re
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any


from ....shared.interfaces import FilterResult
from ._common import (
    build_filter_stats,
    increment_reason,
    nested_metadata,
    pass_item,
    record_warning,
    reject_item,
    resolve_media_path,
)

Runner = Callable[..., subprocess.CompletedProcess[str]]
BLACK_DURATION_RE = re.compile(r"black_duration:(?P<duration>[0-9.]+)")


class BlackFrameFilter:
    def __init__(self, *, runner: Runner = subprocess.run, repo_root: Path = PACK_ROOT, **_: Any) -> None:
        self._runner = runner
        self._repo_root = repo_root

    @property
    def stage_id(self) -> str:
        return "black_frame_filter"

    @property
    def stage_order(self) -> int:
        return 3

    def apply(self, items: list[dict[str, Any]], state: dict[str, Any], config: dict[str, Any]) -> FilterResult:
        started = time.perf_counter()
        threshold = float(config.get("max_black_frame_ratio", 0.98))
        probe_media = bool(config.get("probe_media", False))
        passed: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        reasons: dict[str, int] = {}
        warnings: list[str] = []

        for item in items:
            ratio = _metadata_black_ratio(item)
            if ratio is None and probe_media:
                ratio = self._probe_black_ratio(item, warnings)
            if ratio is None:
                record_warning(warnings, "missing_black_frame_metadata")
                passed.append(pass_item(item, self.stage_id, reason="missing_black_frame_metadata"))
                continue
            if ratio >= threshold:
                increment_reason(reasons, "black_frame_ratio_too_high")
                rejected.append(reject_item(item, self.stage_id, reason="black_frame_ratio_too_high", score=ratio))
                continue
            passed.append(pass_item(item, self.stage_id, score=ratio))

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

    def _probe_black_ratio(self, item: Mapping[str, Any], warnings: list[str]) -> float | None:
        media_path = resolve_media_path(item, repo_root=self._repo_root, must_exist=True)
        duration = _optional_float(item.get("duration_s"))
        if duration is None or duration <= 0:
            record_warning(warnings, "missing_duration_for_black_frame_probe")
            return None
        if media_path is None:
            record_warning(warnings, "missing_media_for_black_frame_probe")
            return None
        command = [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(media_path),
            "-vf",
            "blackdetect=d=0.05:pix_th=0.10",
            "-an",
            "-f",
            "null",
            "-",
        ]
        completed = self._runner(command, capture_output=True, text=True, check=True)
        stderr = getattr(completed, "stderr", "") or ""
        black_duration = sum(float(match.group("duration")) for match in BLACK_DURATION_RE.finditer(stderr))
        return max(0.0, min(1.0, black_duration / duration))


def _metadata_black_ratio(item: Mapping[str, Any]) -> float | None:
    for key in ("black_frame_ratio", "blank_frame_ratio"):
        value = nested_metadata(item, key)
        parsed = _optional_float(value)
        if parsed is not None:
            return parsed
    quality = nested_metadata(item, "quality")
    if isinstance(quality, Mapping):
        for key in ("black_frame_ratio", "blank_frame_ratio"):
            parsed = _optional_float(quality.get(key))
            if parsed is not None:
                return parsed
    return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
