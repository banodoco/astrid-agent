"""Requested scene answers from the existing warm owner, with a bounded cache.

Legacy planner queries remain distinct from final scene visibility queries.
"""
from __future__ import annotations

import hashlib
import json
import math
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

from .remotion_frame_owner import FrameOwnerError
from .remotion_host_session import _read, host_remotion_client
from .remotion_pipe import executor_selection


class RemotionEvaluationClient:
    def __init__(self, *, project_dir, node_executable, attempt_root, renderer_closure,
                 environment=None, port=31500):
        """Use a host-admitted task and a callable for its current renderer closure.

        Trusted-local callers recompute renderer_closure on each query. A pipe
        executor uses only its immutable host selection; the host checks closure
        bytes on every exchange, including validation before cache reuse. Never
        invoke the caller's filesystem closure callback in that executor.
        """
        if not callable(renderer_closure):
            raise TypeError("renderer_closure must supply the current admitted identity")
        self._client = host_remotion_client()
        self._selection = self._installed_selection()
        if self._selection is not None:
            if (str(project_dir) != self._selection.project or str(node_executable) != self._selection.node
                    or str(attempt_root) != self._selection.attempt):
                raise FrameOwnerError("evaluation_selection_mismatch")
            self.project = Path(self._selection.project)
            self.node = Path(self._selection.node)
            self.cli = self._selection.cli
        else:
            self.project = Path(project_dir).resolve(strict=True)
            self.node = Path(node_executable).resolve(strict=True)
            self.cli = str(self.project / "node_modules/@remotion/cli/remotion-cli.js")
        self.attempt = Path(attempt_root).resolve(strict=True)
        if self._selection is not None and str(self.attempt) != self._selection.attempt:
            raise FrameOwnerError("evaluation_selection_mismatch")
        self.closure = renderer_closure
        self.environment = dict(environment or {})
        self.port = port
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._lock = threading.RLock()

    def _installed_selection(self):
        try:
            return executor_selection(host_remotion_client())
        except ValueError as error:
            raise FrameOwnerError("evaluation_binding_invalid: " + str(error)) from error

    def _current_client(self):
        client = host_remotion_client()
        if client is None:
            raise FrameOwnerError("frame_owner_unavailable")
        if client is not self._client or self._installed_selection() != self._selection:
            raise FrameOwnerError("evaluation_binding_changed")
        return client

    def _closure(self):
        return self._selection.renderer if self._selection is not None else self.closure()

    def query_scene(self, *, revision_digest, scene, frames):
        """Final requested-frame visibility/coverage/bounds through the existing owner."""
        return self._query(revision_digest=revision_digest, props={"scene": scene, "frames": frames}, frames=frames, scene=True)

    def query_track(self, *, revision_digest, spans, transitions, frames):
        """Return public queryTrackFrame records, including zero-opacity mounts."""
        return self._query(revision_digest=revision_digest,
            props={"spans": spans, "transitions": transitions, "frames": frames}, frames=frames)

    def _query(self, *, revision_digest, props, frames, scene=False):
        if (not isinstance(revision_digest, str) or not revision_digest
                or not isinstance(frames, (list, tuple)) or not 0 < len(frames) <= 120
                or any(type(frame) is not int or not 0 <= frame <= 2**53 - 1 for frame in frames)):
            raise FrameOwnerError("evaluation_inputs_invalid")
        # Copy callers' mutable inputs before admission/cache lookup.
        encoded = json.dumps(props, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        if len(encoded) > 8 * 1024 * 1024:
            raise FrameOwnerError("frame_request_too_large")
        frames = json.loads(encoded)["frames"]
        with self._lock:
            client = self._current_client()
            closure = self._closure()
            if not isinstance(closure, str) or not closure:
                raise FrameOwnerError("renderer_closure_unavailable")
            key = hashlib.sha256(json.dumps([revision_digest, closure, scene]).encode() + b"\0" + encoded).digest()
            cached = key in self._cache
            if cached and self._selection is None:
                self._current_client()
                self._cache.move_to_end(key)
                return json.loads(self._cache[key])
            with tempfile.TemporaryDirectory(prefix="evaluation-", dir=self.attempt) as temporary:
                root = Path(temporary).resolve(strict=True)
                path = root / "props.json"
                path.write_bytes(encoded)
                request = {
                    "type": "validate" if cached else "evaluate", "projectDir": str(self.project),
                    "compositionId": "AstridTrackEvaluation", "nodeExecutable": str(self.node),
                    "remotionCli": self.cli,
                    "propsPath": str(path), "outputDir": str(root / "unused-frames"),
                    "frames": list(frames), "resolution": None,
                    "port": self.port, "environment": dict(self.environment),
                }
                response = client.request(closure, request, _read)
            self._current_client()
            if not isinstance(response, dict) or response.get("ok") is not True:
                raise FrameOwnerError("evaluation_failed: " + str(response.get("error", "invalid reply")
                                      if isinstance(response, dict) else "invalid reply"))
            if cached:
                if response.get("validated") is not True:
                    raise FrameOwnerError("evaluation_response_invalid")
                self._cache.move_to_end(key)
                return json.loads(self._cache[key])
            result = response.get("evaluation")
            if (not isinstance(result, list) or len(result) != len(frames)
                    or any(not isinstance(value, dict) or type(value.get("frame")) is not int
                           or value["frame"] != frame or not isinstance(value.get("layers"), list)
                           for value, frame in zip(result, frames))):
                raise FrameOwnerError("evaluation_response_invalid")
            if scene:
                for value in result:
                    if (value.get("coverage") not in {"COVERED", "DIP", "GAP", "UNKNOWN"}
                            or value.get("content") not in {"CONTENT", "EMPTY", "UNKNOWN", None}
                            or not isinstance(value.get("findings"), list)):
                        raise FrameOwnerError("evaluation_response_invalid")
                    for layer in value["layers"]:
                        if (not isinstance(layer, dict) or not isinstance(layer.get("clipId"), str)
                                or layer.get("visible") not in (True, False, "unknown")
                                or not isinstance(layer.get("bounds"), dict)
                                or layer["bounds"].get("status") not in {"known", "unknown"}
                                or not isinstance(layer.get("findings"), list)):
                            raise FrameOwnerError("evaluation_response_invalid")
                        opacity = layer.get("opacity")
                        if opacity is not None and (type(opacity) not in (int, float)
                                or not math.isfinite(opacity) or not 0 <= opacity <= 1):
                            raise FrameOwnerError("evaluation_response_invalid")
                        bounds = layer["bounds"]
                        if bounds["status"] == "known":
                            rect = bounds.get("rect")
                            if (not isinstance(rect, dict) or set(rect) != {"x", "y", "width", "height"}
                                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in rect.values())
                                    or rect["width"] < 0 or rect["height"] < 0):
                                raise FrameOwnerError("evaluation_response_invalid")
            if self._closure() != closure:
                raise FrameOwnerError("renderer_closure_changed_during_query")
            self._current_client()
            answer = json.dumps(result, separators=(",", ":"), allow_nan=False).encode()
            if len(answer) > 8 * 1024 * 1024:
                raise FrameOwnerError("evaluation_response_too_large")
            self._cache[key] = answer
            self._cache_bytes += len(answer)
            while len(self._cache) > 128 or self._cache_bytes > 8 * 1024 * 1024:
                _, retired = self._cache.popitem(last=False)
                self._cache_bytes -= len(retired)
            return json.loads(answer)
