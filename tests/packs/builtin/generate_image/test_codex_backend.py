from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from astrid.core.generation.backends import GenerationResult
from astrid.core.generation.backends.codex import CodexBackend
from astrid.core.model_catalog.registry import ModelRegistry

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


SESSION_ID = "11111111-1111-1111-1111-111111111111"


def _image_entry():
    return ModelRegistry.load_default().get_by_mode("flux-dev", "t2i")


def test_codex_backend_copies_fresh_png_and_attaches_reference(
    tmp_path: Path,
) -> None:
    entry, _mode_spec = _image_entry()
    generated_root = tmp_path / "generated_images"
    session_dir = generated_root / SESSION_ID
    session_dir.mkdir(parents=True)
    reference = tmp_path / "reference.png"
    reference.write_bytes(PNG_BYTES)
    seen: dict[str, object] = {}

    def fake_runner(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        (session_dir / "ig_result.png").write_bytes(PNG_BYTES)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"session id: {SESSION_ID}\nGENERATED\n",
        )

    backend = CodexBackend(runner=fake_runner, generated_images_dir=generated_root)
    out_dir = tmp_path / "out"

    result = backend.generate(
        entry=entry,
        mode="t2i",
        params={
            "prompt": "a red bicycle",
            "seed": 7,
            "size": "1024x1024",
            "quality": "low",
            "background": "transparent",
            "image_ref": str(reference),
            "timeout": 12,
        },
        out_dir=out_dir,
    )

    assert result.ok is True
    assert result.image_paths == [out_dir.resolve() / "codex_000.png"]
    assert result.image_paths[0].read_bytes() == PNG_BYTES
    assert result.model_actual == "codex/gpt-image"
    assert result.request_id == SESSION_ID
    assert result.source_urls == [str(session_dir / "ig_result.png")]
    cmd = seen["cmd"]
    assert isinstance(cmd, list)
    assert cmd[0:2] == ["codex", "exec"]
    assert any(part == f"--image={reference}" for part in cmd)
    assert seen["kwargs"]["timeout"] == 12  # type: ignore[index]
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL  # type: ignore[index]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"


def test_codex_backend_fails_loud_when_session_has_no_png(tmp_path: Path) -> None:
    entry, _mode_spec = _image_entry()
    generated_root = tmp_path / "generated_images"
    (generated_root / SESSION_ID).mkdir(parents=True)

    def fake_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=f"session id: {SESSION_ID}\nGENERATED\n",
        )


    backend = CodexBackend(runner=fake_runner, generated_images_dir=generated_root)
    with pytest.raises(RuntimeError, match="produced no ig_\\*.png"):
        backend.generate(entry=entry, mode="t2i", params={"prompt": "a red bicycle"}, out_dir=tmp_path / "out")


def test_codex_attaches_all_reference_roles_in_order(tmp_path: Path) -> None:
    from astrid.core.generation.backends.codex import _build_codex_prompt
    paths = [tmp_path / name for name in ("source.png", "style.png", "brand.png")]
    for path in paths:
        path.write_bytes(PNG_BYTES)
    params = dict(zip(("image_ref", "style_ref", "brand_ref"), map(str, paths)))
    params["prompt"] = "Restyle source using both guides"
    seen = []

    def runner(cmd, **kwargs):
        seen.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"session id: {SESSION_ID}\nGENERATED")

    CodexBackend(runner=runner)._run_codex_once(params)
    assert [arg for arg in seen if arg.startswith("--image=")] == [f"--image={p}" for p in paths]
    assert "brand guide" in _build_codex_prompt(params)
    assert seen[seen.index("--enable") + 1] == "image_generation"


def test_codex_missing_reference_fails_before_launch(tmp_path: Path) -> None:
    def runner(*args, **kwargs):
        pytest.fail("must not launch Codex with a missing reference")

    with pytest.raises(RuntimeError, match="style_ref"):
        CodexBackend(runner=runner)._run_codex_once({
            "prompt": "edit", "style_ref": str(tmp_path / "missing.png")
        })


def test_managed_codex_uses_verified_outer_sandbox(tmp_path, monkeypatch):
    seen = []
    def runner(cmd, **kwargs):
        seen.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"session id: {SESSION_ID}")
    monkeypatch.setenv("ASTRID_CODEX_ATTEMPT_HOME", str(tmp_path))
    monkeypatch.setenv("ASTRID_BROKER_PROXY", "http://127.0.0.1:12345")
    monkeypatch.setenv("ASTRID_NETWORK_POLICY", json.dumps({
        "proxy": "http://127.0.0.1:12345", "broker": {"host_managed": True, "enforced": True}
    }))
    CodexBackend(runner=runner)._run_codex_once({"prompt": "test"})
    assert seen[seen.index("--sandbox") + 1] == "danger-full-access"


def test_codex_diagnostics_redact_signed_urls():
    from astrid.core.generation.backends.codex import _diagnostic_tail
    assert _diagnostic_tail("failed https://example.test/image?sig=secret") == "failed https://example.test/image?<redacted>"


def test_codex_collects_current_exec_png_names(tmp_path):
    session = tmp_path / SESSION_ID
    session.mkdir()
    output = session / "exec-e483a908-d480-48e1-b94b-8e0ebb2b7c4b.png"
    output.write_bytes(PNG_BYTES)
    (session / "reference.png").write_bytes(PNG_BYTES)
    backend = CodexBackend(generated_images_dir=tmp_path)
    assert backend._collect_new_images(SESSION_ID, 0) == [output]

def test_generate_image_codex_preflight_falls_back_to_cloud(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from astrid.packs.generation.actions.generate_image import run as run_mod

    class FakeAdapter:
        def generate(self, *, entry, mode: str, params: dict[str, object], out_dir: Path):
            image_path = out_dir / "cloud_result.png"
            image_path.write_bytes(PNG_BYTES)
            assert mode == "t2i"
            assert params["prompt"] == "fallback test"
            return GenerationResult(
                image_paths=[image_path],
                seed_used=int(params["seed"]),
                model_actual="fal-ai/flux/dev",
                applied_features=["prompt", "seed"],
                request_id="fal-req",
            )

    class FakeRegistry:
        def create(self, execution: str, *, env_file: Path | None) -> FakeAdapter:
            assert execution == "cloud"
            return FakeAdapter()

    monkeypatch.setattr(
        run_mod,
        "load_default_generation_backend_registry",
        lambda: FakeRegistry(),
    )
    monkeypatch.setattr(
        run_mod,
        "codex_unavailable_reason",
        lambda: "`codex` binary not found on PATH",
    )
    monkeypatch.setattr(run_mod, "embed_png_text", lambda path, fields: None)

    out = tmp_path / "out"
    result = run_mod.generate_core(
        [
            "--model",
            "flux-dev",
            "--mode",
            "t2i",
            "--execution",
            "codex",
            "--prompt",
            "fallback test",
            "--out",
            str(out),
            "--seed",
            "9",
        ]
    )

    captured = capsys.readouterr()
    assert captured.err == ""
    assert result.model_actual == "fal-ai/flux/dev"
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["execution"] == "cloud"
    assert manifest["warnings"][0]["feature"] == "codex"
    assert "not found on PATH" in manifest["warnings"][0]["reason"]


def test_managed_attempt_home_is_bound_in_process_not_in_os_environ(tmp_path, monkeypatch):
    # The pack host's command custody binds the launch environment; a key added to
    # os.environ after launch failed the attempt's Git source checks.
    import os
    from astrid.core.generation.backends.codex import managed_attempt_home

    seen = {}
    def runner(cmd, **kwargs):
        seen.update(cmd=cmd, env=kwargs.get("env") or {})
        return subprocess.CompletedProcess(cmd, 0, stdout=f"session id: {SESSION_ID}")
    monkeypatch.delenv("ASTRID_CODEX_ATTEMPT_HOME", raising=False)
    monkeypatch.setenv("ASTRID_BROKER_PROXY", "http://127.0.0.1:12345")
    monkeypatch.setenv("ASTRID_NETWORK_POLICY", json.dumps({
        "proxy": "http://127.0.0.1:12345", "broker": {"host_managed": True, "enforced": True}
    }))
    before = dict(os.environ)
    with managed_attempt_home(tmp_path):
        CodexBackend(runner=runner)._run_codex_once({"prompt": "test"})
        assert dict(os.environ) == before
    assert seen["cmd"][seen["cmd"].index("--sandbox") + 1] == "danger-full-access"
    assert seen["env"].get("CODEX_HOME") == str(tmp_path)


def test_codex_runs_without_the_login_shell_snapshot(tmp_path):
    # Its login shell exits before its children, which the host's custody census refuses.
    seen = []
    def runner(cmd, **kwargs):
        seen.extend(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"session id: {SESSION_ID}")
    CodexBackend(runner=runner)._run_codex_once({"prompt": "test"})
    pairs = list(zip(seen, seen[1:]))
    assert ("--disable", "shell_snapshot") in pairs
    assert ("--enable", "image_generation") in pairs


def test_managed_attempt_home_restores_nested_override_after_exception(tmp_path, monkeypatch):
    import os
    import pytest
    from astrid.core.generation.backends.codex import managed_attempt_home, _managed_home

    monkeypatch.setenv("ASTRID_CODEX_ATTEMPT_HOME", str(tmp_path / "standalone"))
    before = dict(os.environ)
    assert _managed_home() == str(tmp_path / "standalone")
    with managed_attempt_home(tmp_path / "outer"):
        with pytest.raises(RuntimeError, match="failed managed operation"):
            with managed_attempt_home(tmp_path / "inner"):
                assert _managed_home() == str(tmp_path / "inner")
                raise RuntimeError("failed managed operation")
        assert _managed_home() == str(tmp_path / "outer")
        assert dict(os.environ) == before
    assert _managed_home() == str(tmp_path / "standalone")
    assert dict(os.environ) == before
