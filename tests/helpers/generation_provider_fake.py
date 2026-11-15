"""Run catalog provider adapters with in-process fake transports.

This is intended for tests that intercept ``GenerationFacade._invoke`` and
need to prove that the selected catalog entry reaches its real request builder
without reading credentials or making network calls.
"""

from __future__ import annotations

import base64
import importlib
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from astrid.core.generation.backends.registry import (
    GenerationBackendRegistry,
    load_default_generation_backend_registry,
)
from astrid.core.util.http import HttpClient


def exercise_provider_request(
    entry: Any,
    mode: str,
    inputs: dict[str, Any],
    tmp_path: Path,
    monkeypatch: Any,
    *,
    backend_registry: GenerationBackendRegistry | None = None,
) -> dict[str, Any]:
    """Exercise one selected provider adapter and return captured request data.

    Backend selection comes from the default generation backend registry and
    the supplied execution value. The returned mapping includes the adapter,
    endpoint when the backend declares one, request payload when available,
    and the adapter result.
    """
    registry = backend_registry or load_default_generation_backend_registry()
    execution = str(inputs["execution"])
    descriptor = registry.get_descriptor(execution)
    backend = descriptor.load_class()
    provider = importlib.import_module(descriptor.module)
    params = dict(inputs)
    mode_spec = entry.modes[mode]
    backend_spec = mode_spec.backends[execution]
    endpoint = getattr(backend_spec, "endpoint", None)
    captured: dict[str, Any] = {
        "backend": backend,
        "endpoint": endpoint,
        "payload": None,
        "result": None,
    }

    if hasattr(backend, "submit"):
        from tests.core.generation.backends.test_fal_cost import _make_fal_result

        client = Mock(spec=HttpClient)
        submitter = Mock(return_value=_make_fal_result())
        uploader = Mock(side_effect=lambda _client, path, _key: f"https://fake.invalid/{Path(path).name}")
        adapter = registry.create(execution, init_overrides={"client": client})
        result = adapter.submit(
            entry,
            mode,
            params,
            api_key="fake-provider-key",
            uploader=uploader,
            submitter=submitter,
        )
        call = submitter.call_args
        captured.update(payload=call.args[2], endpoint=call.args[1], result=result,
                        transport_call=call)
        if backend_spec.endpoint:
            assert call.args[1] in {backend_spec.endpoint, backend_spec.lora_endpoint}
        return captured

    if "key_resolver" in inspect.signature(backend).parameters:
        from tests.core.generation.backends.test_openai_route import _Response, _png

        png = _png("green")
        body = base64.b64encode(png).decode("ascii")
        calls: list[Any] = []

        def fake_urlopen(request: Any, *, timeout: int) -> _Response:
            calls.append(request)
            declared = provider.API_URL.rsplit("/", 1)[0] + "/" + (
                "edits" if mode == "edit" else "generations"
            )
            assert request.full_url == declared
            assert request.get_header("Authorization") == "Bearer fake-provider-key"
            captured["endpoint"] = declared
            captured["payload"] = request.data
            return _Response({"data": [{"b64_json": body}], "created": 1})

        monkeypatch.setattr(provider, "urlopen", fake_urlopen)
        adapter = registry.create(
            execution,
            init_overrides={"key_resolver": lambda **_kwargs: "fake-provider-key"},
        )
        result = adapter.generate(entry, mode, params, tmp_path / "out")
        captured.update(result=result, transport_calls=calls)
        return captured

    if hasattr(backend, "_resolve_api_key") and hasattr(backend, "generate"):
        client = Mock(spec=HttpClient)
        submit_call: dict[str, Any] = {}

        def fake_submit(actual_client: Any, actual_endpoint: str, payload: dict[str, Any], key: str) -> dict[str, Any]:
            assert actual_client is client
            assert key == "fake-provider-key"
            submit_call.update(endpoint=actual_endpoint, payload=payload)
            return {}

        monkeypatch.setattr(provider, "wavespeed_submit_and_poll", fake_submit)
        adapter = registry.create(execution, init_overrides={"client": client})
        monkeypatch.setattr(adapter, "_resolve_api_key", lambda: "fake-provider-key")
        result = adapter.generate(entry, mode, params, tmp_path / "out")
        captured.update(endpoint=submit_call.get("endpoint"), payload=submit_call.get("payload"),
                        result=result, transport_call=submit_call)
        if backend_spec.endpoint:
            assert submit_call["endpoint"] == backend_spec.endpoint
        return captured

    raise AssertionError(
        f"No fake provider transport is defined for registered adapter "
        f"{descriptor.module}.{descriptor.class_name}"
    )
