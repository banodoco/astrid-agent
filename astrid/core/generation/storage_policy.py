"""Versioned resource ceilings for narrowly admitted image routes.

The policy in this module is a hard upper bound, not a claim about typical
provider usage.  A producer may advertise these numbers only when the
executor enforces the same limits before settlement.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import math
from pathlib import Path
from typing import Any, ClassVar, Mapping


class ImageStoragePolicyError(ValueError):
    """Raised when a request or provider result is outside the bounded profile."""


RUNTIME_OBJECT_MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CloudT2IStoragePolicy:
    """Whole-task ceiling for the registry-backed cloud text-image route.

    The executor performs a sequential one-image provider call for each task
    output. Downloaded bytes are written directly to their final staging
    paths, so they are charged once to the output bucket. Scratch reserves
    cover only distinct filesystem objects: one atomic metadata rewrite, one
    atomic manifest write, and bounded provider/host control evidence.

    Model membership and cloud backend availability remain authoritative in
    ``ModelRegistry``; this policy deliberately does not copy that list.
    """

    version: str = "astrid.cloud-t2i.registry.v1"
    output_max_bytes: int = RUNTIME_OBJECT_MAX_BYTES
    manifest_max_bytes: int = 1 * 1024 * 1024
    control_max_bytes: int = 1 * 1024 * 1024
    max_prompt_chars: int = 4_096
    max_negative_prompt_chars: int = 4_096
    max_size_chars: int = 64
    max_width: int = 2_048
    max_height: int = 2_048
    max_count: int = 4
    provider_outputs_per_call: int = 1

    @property
    def estimate_components(self) -> dict[str, dict[str, int]]:
        """Return the disjoint components used to derive the host envelope."""
        return {
            "scratch": {
                "input_materialization_bytes": 0,
                "download_temp_bytes": 0,
                "metadata_rewrite_temp_bytes": self.output_max_bytes,
                "manifest_write_temp_bytes": self.manifest_max_bytes,
                "control_and_cleanup_evidence_bytes": self.control_max_bytes,
            },
            "output": {
                "generated_image_bytes": self.max_count * self.output_max_bytes,
                "result_manifest_bytes": self.manifest_max_bytes,
            },
        }

    @property
    def estimated_output_bytes(self) -> int:
        return sum(self.estimate_components["output"].values())

    @property
    def estimated_scratch_bytes(self) -> int:
        return sum(self.estimate_components["scratch"].values())

    @property
    def estimate(self) -> dict[str, int]:
        return {
            "scratch_bytes": self.estimated_scratch_bytes,
            "output_bytes": self.estimated_output_bytes,
        }

    def _validate_identity(self, *, model: str, mode: str, execution: str) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ImageStoragePolicyError("bounded cloud t2i requires a model id")
        if (mode, execution) != ("t2i", "cloud"):
            raise ImageStoragePolicyError(
                f"request is outside bounded storage policy {self.version}"
            )

    def _validate_common_params(self, params: Mapping[str, Any]) -> None:
        prompt = params.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ImageStoragePolicyError("bounded cloud t2i requires a prompt")
        if len(prompt) > self.max_prompt_chars:
            raise ImageStoragePolicyError(
                f"prompt exceeds bounded limit of {self.max_prompt_chars} characters"
            )
        negative_prompt = params.get("negative_prompt")
        if negative_prompt is not None and (
            not isinstance(negative_prompt, str)
            or len(negative_prompt) > self.max_negative_prompt_chars
        ):
            raise ImageStoragePolicyError(
                "negative_prompt must be a string no longer than "
                f"{self.max_negative_prompt_chars} characters"
            )
        size = params.get("size")
        if size in (None, ""):
            return
        if not isinstance(size, str) or len(size) > self.max_size_chars:
            raise ImageStoragePolicyError(
                f"size must be a string no longer than {self.max_size_chars} characters"
            )
        normalized = size.strip().lower()
        if not normalized:
            raise ImageStoragePolicyError("size must not be blank")
        if "x" not in normalized:
            # Registry-backed providers also advertise named presets. Their
            # concrete result is still byte- and dimension-checked below.
            return
        try:
            width_text, height_text = normalized.split("x", 1)
            width, height = int(width_text), int(height_text)
        except (TypeError, ValueError) as exc:
            raise ImageStoragePolicyError(
                "bounded cloud t2i size must be WIDTHxHEIGHT or a preset"
            ) from exc
        if not (1 <= width <= self.max_width and 1 <= height <= self.max_height):
            raise ImageStoragePolicyError(
                f"bounded cloud t2i dimensions must be at most {self.max_width}x{self.max_height}"
            )

    def validate_task_request(
        self,
        *,
        model: str,
        mode: str,
        execution: str,
        params: Mapping[str, Any],
    ) -> None:
        """Validate one complete task before creating provider output."""
        self._validate_identity(model=model, mode=mode, execution=execution)
        if params.get("prompts_file") is not None:
            raise ImageStoragePolicyError(
                "bounded cloud t2i requires the typed prompt field, not prompts_file"
            )
        count = params.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int):
            raise ImageStoragePolicyError("bounded cloud t2i count must be an integer")
        if not 1 <= count <= self.max_count:
            raise ImageStoragePolicyError(
                f"bounded cloud t2i count must be between 1 and {self.max_count}"
            )
        self._validate_common_params(params)

    def validate_request(
        self,
        *,
        model: str,
        mode: str,
        execution: str,
        params: Mapping[str, Any],
    ) -> None:
        """Validate one sequential provider call inside an admitted task."""
        self._validate_identity(model=model, mode=mode, execution=execution)
        if params.get("count", 1) != self.provider_outputs_per_call:
            raise ImageStoragePolicyError(
                "bounded cloud t2i provider calls must request exactly one output"
            )
        self._validate_common_params(params)

    def validate_download(self, data: bytes, *, index: int = 0) -> tuple[int, int]:
        """Validate byte size and decoded dimensions before staging."""
        if len(data) > self.output_max_bytes:
            raise ImageStoragePolicyError(
                f"provider output {index} exceeds {self.output_max_bytes} bytes"
            )
        try:
            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                if width > self.max_width or height > self.max_height:
                    raise ImageStoragePolicyError(
                        f"provider output {index} dimensions {width}x{height} exceed "
                        f"{self.max_width}x{self.max_height}"
                    )
                image.load()
        except Exception as exc:
            if isinstance(exc, ImageStoragePolicyError):
                raise
            raise ImageStoragePolicyError(
                f"provider output {index} is not a decodable image"
            ) from exc
        return width, height

    def validate_final_output(self, path: str | Path, *, index: int = 0) -> int:
        """Check final bytes after the atomic metadata rewrite."""
        size = Path(path).stat().st_size
        if size > self.output_max_bytes:
            raise ImageStoragePolicyError(
                f"final provider output {index} exceeds {self.output_max_bytes} bytes "
                "after metadata embedding"
            )
        return size

    def validate_manifest(self, path: str | Path) -> int:
        size = Path(path).stat().st_size
        if size > self.manifest_max_bytes:
            raise ImageStoragePolicyError(
                f"image result manifest exceeds {self.manifest_max_bytes} bytes"
            )
        return size


@dataclass(frozen=True, slots=True)
class CloudI2IStoragePolicy:
    """The first bounded cloud image-to-image execution profile."""

    version: str = "astrid.cloud-i2i.z-image.v1"
    source_max_bytes: int = 512_000
    output_max_bytes: int = RUNTIME_OBJECT_MAX_BYTES
    manifest_max_bytes: int = 1 * 1024 * 1024
    control_max_bytes: int = 1 * 1024 * 1024
    max_prompt_chars: int = 4_096
    max_width: int = 2_048
    max_height: int = 2_048
    max_count: int = 1

    @property
    def estimated_output_bytes(self) -> int:
        """Maximum final image plus the published result manifest."""
        return self.output_max_bytes + self.manifest_max_bytes

    @property
    def estimated_scratch_bytes(self) -> int:
        """Input, staged/temporary copies, and bounded control evidence.

        The two output terms cover the downloaded/staged copy and the
        sibling temporary copy used by metadata embedding.  Published output
        remains a separate settlement estimate.
        """
        return (
            self.source_max_bytes
            + 2 * self.output_max_bytes
            + self.manifest_max_bytes
            + self.control_max_bytes
        )

    @property
    def estimate(self) -> dict[str, int]:
        return {
            "scratch_bytes": self.estimated_scratch_bytes,
            "output_bytes": self.estimated_output_bytes,
        }

    _IMAGE_EXTENSIONS = {
        "image/png": {".png"},
        "image/jpeg": {".jpg", ".jpeg"},
        "image/webp": {".webp"},
        "image/gif": {".gif"},
    }
    _IMAGE_FORMATS = {
        "image/png": "PNG",
        "image/jpeg": "JPEG",
        "image/webp": "WEBP",
        "image/gif": "GIF",
    }
    _MAX_SEED = 2_147_483_647

    def _validate_size(self, params: Mapping[str, Any]) -> tuple[int, int]:
        size = params.get("size")
        if not isinstance(size, str) or "x" not in size.lower():
            raise ImageStoragePolicyError("bounded cloud i2i requires explicit WIDTHxHEIGHT size")
        try:
            width_text, height_text = size.lower().split("x", 1)
            width, height = int(width_text), int(height_text)
        except (TypeError, ValueError) as exc:
            raise ImageStoragePolicyError("bounded cloud i2i size must be WIDTHxHEIGHT") from exc
        if not (1 <= width <= self.max_width and 1 <= height <= self.max_height):
            raise ImageStoragePolicyError(
                f"bounded cloud i2i dimensions must be at most {self.max_width}x{self.max_height}"
            )
        return width, height

    def _validate_controls(
        self,
        params: Mapping[str, Any],
        *,
        require_strength: bool,
    ) -> None:
        count = params.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count != self.max_count:
            raise ImageStoragePolicyError(
                f"bounded cloud i2i requires count={self.max_count} as an integer"
            )
        prompt = params.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ImageStoragePolicyError("bounded cloud i2i requires a prompt")
        if len(prompt) > self.max_prompt_chars:
            raise ImageStoragePolicyError(
                f"prompt exceeds bounded limit of {self.max_prompt_chars} characters"
            )
        strength = params.get("strength")
        if require_strength and strength is None:
            raise ImageStoragePolicyError("bounded cloud i2i requires numeric strength")
        if strength is not None and (
            isinstance(strength, bool)
            or not isinstance(strength, (int, float))
            or not math.isfinite(float(strength))
            or not 0 <= float(strength) <= 1
        ):
            raise ImageStoragePolicyError("bounded cloud i2i strength must be a finite number from 0 through 1")
        seed = params.get("seed")
        if seed is not None and (
            isinstance(seed, bool)
            or not isinstance(seed, int)
            or not 0 <= seed <= self._MAX_SEED
        ):
            raise ImageStoragePolicyError(
                f"bounded cloud i2i seed must be an integer from 0 through {self._MAX_SEED}"
            )

    def validate_admission_request(
        self,
        *,
        model: str,
        mode: str,
        execution: str,
        params: Mapping[str, Any],
    ) -> None:
        """Validate the immutable values before host stringification."""
        if (model, mode, execution) != ("z-image", "i2i", "cloud"):
            raise ImageStoragePolicyError(
                f"request is outside bounded storage policy {self.version}"
            )
        self._validate_controls(params, require_strength=True)
        self._validate_size(params)
        image_ref = params.get("image_ref")
        if not isinstance(image_ref, Mapping):
            raise ImageStoragePolicyError(
                "bounded cloud i2i image_ref must be a typed CAS descriptor"
            )
        digest = image_ref.get("digest")
        normalized = str(digest).removeprefix("sha256:") if isinstance(digest, str) else ""
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ImageStoragePolicyError("bounded cloud i2i image_ref digest must be a SHA-256 object ID")
        filename = image_ref.get("filename")
        if not isinstance(filename, str) or not filename or Path(filename).name != filename:
            raise ImageStoragePolicyError("bounded cloud i2i image_ref filename must be a safe basename")
        media_type = image_ref.get("media_type")
        if not isinstance(media_type, str) or media_type.lower() not in self._IMAGE_EXTENSIONS:
            raise ImageStoragePolicyError("bounded cloud i2i image_ref media_type is not a supported image type")
        if Path(filename).suffix.lower() not in self._IMAGE_EXTENSIONS[media_type.lower()]:
            raise ImageStoragePolicyError(
                "bounded cloud i2i image_ref filename and media_type disagree"
            )

    def _validate_source_file(self, source: Path, *, media_type: str | None = None) -> None:
        if source.stat().st_size > self.source_max_bytes:
            raise ImageStoragePolicyError(
                f"image_ref exceeds bounded source limit of {self.source_max_bytes} bytes"
            )
        try:
            from PIL import Image

            with Image.open(source) as image:
                expected_format = self._IMAGE_FORMATS.get(media_type.lower()) if media_type else None
                if expected_format is not None and image.format != expected_format:
                    raise ImageStoragePolicyError(
                        "image_ref bytes do not match the admitted media_type"
                    )
                width, height = image.size
                if width > self.max_width or height > self.max_height:
                    raise ImageStoragePolicyError(
                        f"image_ref dimensions {width}x{height} exceed bounded limits"
                    )
                image.load()
        except ImageStoragePolicyError:
            raise
        except Exception as exc:
            raise ImageStoragePolicyError("bounded cloud i2i image_ref is not a decodable image") from exc

    def validate_materialized_source(self, path: str | Path, *, media_type: str) -> None:
        """Reconcile the fetched bytes with the immutable media-type descriptor."""
        self._validate_source_file(Path(path), media_type=media_type)

    def _validate_materialized_request(
        self,
        *,
        params: Mapping[str, Any],
        require_strength: bool,
    ) -> None:
        self._validate_controls(params, require_strength=require_strength)
        self._validate_size(params)
        image_ref = params.get("image_ref")
        if not isinstance(image_ref, str) or not image_ref.strip():
            raise ImageStoragePolicyError("bounded cloud i2i requires a materialized image_ref")
        source = Path(image_ref)
        if not source.is_file():
            raise ImageStoragePolicyError("bounded cloud i2i image_ref is not a file")
        self._validate_source_file(source)

    def validate_request(
        self,
        *,
        model: str,
        mode: str,
        execution: str,
        params: Mapping[str, Any],
    ) -> None:
        """Reject every request outside the profile; never silently clamp it."""
        if (model, mode, execution) != ("z-image", "i2i", "cloud"):
            raise ImageStoragePolicyError(
                f"request is outside bounded storage policy {self.version}"
            )
        self._validate_materialized_request(params=params, require_strength=True)

    def validate_download(self, data: bytes, *, index: int = 0) -> tuple[int, int]:
        """Validate byte size and decoded image dimensions before staging."""
        if len(data) > self.output_max_bytes:
            raise ImageStoragePolicyError(
                f"provider output {index} exceeds {self.output_max_bytes} bytes"
            )
        try:
            from PIL import Image

            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                if width > self.max_width or height > self.max_height:
                    raise ImageStoragePolicyError(
                        f"provider output {index} dimensions {width}x{height} exceed "
                        f"{self.max_width}x{self.max_height}"
                    )
                image.load()
        except Exception as exc:
            if isinstance(exc, ImageStoragePolicyError):
                raise
            raise ImageStoragePolicyError(
                f"provider output {index} is not a decodable image"
            ) from exc
        return width, height

    def validate_final_output(self, path: str | Path, *, index: int = 0) -> int:
        """Check the bytes after metadata embedding and any atomic rewrite."""
        size = Path(path).stat().st_size
        if size > self.output_max_bytes:
            raise ImageStoragePolicyError(
                f"final provider output {index} exceeds {self.output_max_bytes} bytes "
                "after metadata embedding"
            )
        return size

    def validate_manifest(self, path: str | Path) -> int:
        """Check the published manifest before the host harvests it."""
        size = Path(path).stat().st_size
        if size > self.manifest_max_bytes:
            raise ImageStoragePolicyError(
                f"image result manifest exceeds {self.manifest_max_bytes} bytes"
            )
        return size


@dataclass(frozen=True, slots=True)
class CloudEditStoragePolicy(CloudI2IStoragePolicy):
    """Bounded source-only Qwen cloud edit profile.

    Masked edits are intentionally not included: the public Qwen template and
    provider contract currently expose no verified mask input.  This separate
    identity prevents source-only evidence from being reused for inpaint or
    Klein routes.
    """

    version: str = "astrid.cloud-edit.qwen-source.v1"

    def validate_admission_request(
        self,
        *,
        model: str,
        mode: str,
        execution: str,
        params: Mapping[str, Any],
    ) -> None:
        """Validate the immutable source-only Qwen request before materialization."""
        if (model, mode, execution) != ("qwen-image-edit-2511", "edit", "cloud"):
            raise ImageStoragePolicyError(
                f"request is outside bounded storage policy {self.version}"
            )
        if "mask_ref" in params:
            raise ImageStoragePolicyError(
                "source-only Qwen edit profile does not admit mask_ref"
            )
        if "strength" in params:
            raise ImageStoragePolicyError(
                "source-only Qwen edit profile does not admit strength"
            )
        self._validate_controls(params, require_strength=False)
        self._validate_size(params)
        image_ref = params.get("image_ref")
        if not isinstance(image_ref, Mapping):
            raise ImageStoragePolicyError(
                "source-only Qwen image_ref must be a typed CAS descriptor"
            )
        digest = image_ref.get("digest")
        normalized = str(digest).removeprefix("sha256:") if isinstance(digest, str) else ""
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ImageStoragePolicyError(
                "source-only Qwen image_ref digest must be a SHA-256 object ID"
            )
        filename = image_ref.get("filename")
        if not isinstance(filename, str) or not filename or Path(filename).name != filename:
            raise ImageStoragePolicyError(
                "source-only Qwen image_ref filename must be a safe basename"
            )
        media_type = image_ref.get("media_type")
        if not isinstance(media_type, str) or media_type.lower() not in self._IMAGE_EXTENSIONS:
            raise ImageStoragePolicyError(
                "source-only Qwen image_ref media_type is not a supported image type"
            )
        if Path(filename).suffix.lower() not in self._IMAGE_EXTENSIONS[media_type.lower()]:
            raise ImageStoragePolicyError(
                "source-only Qwen image_ref filename and media_type disagree"
            )

    def validate_request(
        self,
        *,
        model: str,
        mode: str,
        execution: str,
        params: Mapping[str, Any],
    ) -> None:
        if (model, mode, execution) != ("qwen-image-edit-2511", "edit", "cloud"):
            raise ImageStoragePolicyError(
                f"request is outside bounded storage policy {self.version}"
            )
        if "mask_ref" in params:
            raise ImageStoragePolicyError(
                "source-only Qwen edit profile does not admit mask_ref"
            )
        if "strength" in params:
            raise ImageStoragePolicyError(
                "source-only Qwen edit profile does not admit strength"
            )
        self._validate_materialized_request(params=params, require_strength=False)

@dataclass(frozen=True, slots=True)
class CloudUnifiedEditStoragePolicy(CloudI2IStoragePolicy):
    """One typed edit envelope with explicit source and mask profiles."""

    version: str = "astrid.cloud-edit.unified.v1"
    mask_max_bytes: int = 512_000

    _SOURCE_PROFILES: ClassVar[frozenset[tuple[str, str, str]]] = frozenset({
        ("qwen-image-edit-2511", "edit", "cloud"),
        ("flux2-klein-4b", "edit", "cloud"),
        ("flux2-klein-9b", "edit", "cloud"),
    })
    _MASK_PROFILES: ClassVar[frozenset[tuple[str, str, str]]] = frozenset({
        ("qwen-image-edit-inpaint", "inpaint", "cloud"),
    })

    @property
    def estimated_scratch_bytes(self) -> int:
        return (
            self.source_max_bytes
            + self.mask_max_bytes
            + 2 * self.output_max_bytes
            + self.manifest_max_bytes
            + self.control_max_bytes
        )

    def _profile(self, *, model: str, mode: str, execution: str) -> str:
        identity = (model, mode, execution)
        if identity in self._SOURCE_PROFILES:
            return "source"
        if identity in self._MASK_PROFILES:
            return "mask"
        raise ImageStoragePolicyError(
            f"request is outside bounded storage policy {self.version}"
        )

    def _validate_controls(self, params: Mapping[str, Any]) -> None:
        count = params.get("count", 1)
        if isinstance(count, bool) or not isinstance(count, int) or count != self.max_count:
            raise ImageStoragePolicyError(
                f"bounded cloud edit requires count={self.max_count} as an integer"
            )
        prompt = params.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ImageStoragePolicyError("bounded cloud edit requires a prompt")
        if len(prompt) > self.max_prompt_chars:
            raise ImageStoragePolicyError(
                f"prompt exceeds bounded limit of {self.max_prompt_chars} characters"
            )
        seed = params.get("seed")
        if seed is not None and (
            isinstance(seed, bool)
            or not isinstance(seed, int)
            or not 0 <= seed <= self._MAX_SEED
        ):
            raise ImageStoragePolicyError(
                f"bounded cloud edit seed must be an integer from 0 through {self._MAX_SEED}"
            )
        self._validate_size(params)

    def _validate_descriptor(self, params: Mapping[str, Any], name: str) -> None:
        descriptor = params.get(name)
        if not isinstance(descriptor, Mapping):
            raise ImageStoragePolicyError(
                f"bounded cloud edit {name} must be a typed CAS descriptor"
            )
        digest = descriptor.get("digest")
        normalized = str(digest).removeprefix("sha256:") if isinstance(digest, str) else ""
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ImageStoragePolicyError(
                f"bounded cloud edit {name} digest must be a SHA-256 object ID"
            )
        filename = descriptor.get("filename")
        if not isinstance(filename, str) or not filename or Path(filename).name != filename:
            raise ImageStoragePolicyError(
                f"bounded cloud edit {name} filename must be a safe basename"
            )
        media_type = descriptor.get("media_type")
        if not isinstance(media_type, str) or media_type.lower() not in self._IMAGE_EXTENSIONS:
            raise ImageStoragePolicyError(
                f"bounded cloud edit {name} media_type is not a supported image type"
            )
        if Path(filename).suffix.lower() not in self._IMAGE_EXTENSIONS[media_type.lower()]:
            raise ImageStoragePolicyError(
                f"bounded cloud edit {name} filename and media_type disagree"
            )

    def validate_admission_request(
        self,
        *,
        model: str,
        mode: str,
        execution: str,
        params: Mapping[str, Any],
    ) -> None:
        profile = self._profile(model=model, mode=mode, execution=execution)
        self._validate_controls(params)
        self._validate_descriptor(params, "image_ref")
        mask_ref = params.get("mask_ref")
        strength = params.get("strength")
        if profile == "mask":
            if mask_ref is None:
                raise ImageStoragePolicyError("bounded cloud inpaint requires mask_ref")
            self._validate_descriptor(params, "mask_ref")
            if (
                isinstance(strength, bool)
                or not isinstance(strength, (int, float))
                or not math.isfinite(float(strength))
                or not 0 <= float(strength) <= 1
            ):
                raise ImageStoragePolicyError(
                    "bounded cloud inpaint strength must be a finite number from 0 through 1"
                )
        elif mask_ref is not None or strength is not None:
            raise ImageStoragePolicyError(
                "source-only cloud edit does not admit mask_ref or strength"
            )

    def _validate_materialized_image(
        self,
        path: str | Path,
        *,
        media_type: str,
        role: str,
        max_bytes: int,
    ) -> tuple[int, int]:
        source = Path(path)
        if not source.is_file():
            raise ImageStoragePolicyError(f"bounded cloud edit {role} is not a file")
        if source.stat().st_size > max_bytes:
            raise ImageStoragePolicyError(
                f"{role} exceeds bounded input limit of {max_bytes} bytes"
            )
        try:
            from PIL import Image

            with Image.open(source) as image:
                expected_format = self._IMAGE_FORMATS[media_type.lower()]
                if image.format != expected_format:
                    raise ImageStoragePolicyError(
                        f"{role} bytes do not match the admitted media_type"
                    )
                width, height = image.size
                if width > self.max_width or height > self.max_height:
                    raise ImageStoragePolicyError(
                        f"{role} dimensions {width}x{height} exceed bounded limits"
                    )
                image.load()
                return width, height
        except ImageStoragePolicyError:
            raise
        except Exception as exc:
            raise ImageStoragePolicyError(
                f"bounded cloud edit {role} is not a decodable image"
            ) from exc

    def validate_materialized_inputs(
        self,
        *,
        model: str,
        mode: str,
        execution: str,
        params: Mapping[str, Any],
        image_media_type: str,
        mask_media_type: str | None = None,
    ) -> None:
        profile = self._profile(model=model, mode=mode, execution=execution)
        self._validate_controls(params)
        if not isinstance(params.get("image_ref"), (str, Path)):
            raise ImageStoragePolicyError(
                "bounded cloud edit requires a materialized image_ref"
            )
        source_size = self._validate_materialized_image(
            params.get("image_ref"),
            media_type=image_media_type,
            role="image_ref",
            max_bytes=self.source_max_bytes,
        )
        mask_path = params.get("mask_ref")
        if profile == "mask":
            if not isinstance(mask_path, (str, Path)) or not mask_media_type:
                raise ImageStoragePolicyError("bounded cloud inpaint mask materialization is incomplete")
            mask_size = self._validate_materialized_image(
                mask_path,
                media_type=mask_media_type,
                role="mask_ref",
                max_bytes=self.mask_max_bytes,
            )
            if mask_size != source_size:
                raise ImageStoragePolicyError(
                    "bounded cloud inpaint image_ref and mask_ref dimensions must match"
                )
        elif mask_path is not None:
            raise ImageStoragePolicyError(
                "source-only cloud edit does not admit a materialized mask_ref"
            )

    def validate_request(
        self,
        *,
        model: str,
        mode: str,
        execution: str,
        params: Mapping[str, Any],
    ) -> None:
        """Validate the provider call after CAS descriptors are materialized."""
        profile = self._profile(model=model, mode=mode, execution=execution)
        self._validate_controls(params)
        image_ref = params.get("image_ref")
        if not isinstance(image_ref, (str, Path)):
            raise ImageStoragePolicyError("bounded cloud edit requires a materialized image_ref")
        mask_ref = params.get("mask_ref")
        if profile == "mask" and not isinstance(mask_ref, (str, Path)):
            raise ImageStoragePolicyError("bounded cloud inpaint requires a materialized mask_ref")
        if profile == "source" and mask_ref is not None:
            raise ImageStoragePolicyError("source-only cloud edit does not admit mask_ref")


CLOUD_T2I_STORAGE_POLICY = CloudT2IStoragePolicy()
CLOUD_I2I_STORAGE_POLICY = CloudI2IStoragePolicy()
CLOUD_EDIT_STORAGE_POLICY = CloudEditStoragePolicy()
CLOUD_UNIFIED_EDIT_STORAGE_POLICY = CloudUnifiedEditStoragePolicy()


__all__ = [
    "CLOUD_I2I_STORAGE_POLICY",
    "CLOUD_EDIT_STORAGE_POLICY",
    "CLOUD_UNIFIED_EDIT_STORAGE_POLICY",
    "CLOUD_T2I_STORAGE_POLICY",
    "CloudT2IStoragePolicy",
    "CloudEditStoragePolicy",
    "CloudUnifiedEditStoragePolicy",
    "CloudI2IStoragePolicy",
    "ImageStoragePolicyError",
]
