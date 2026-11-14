"""Paid claim consent over MCP's real CLI subprocess, with no provider egress."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from astrid import mcp_server


@pytest.fixture
def machine_mcp_process(tmp_path, monkeypatch):
    # sitecustomize applies to the CLI child too. Guard before importing product
    # code; fake the Runtime/RunPod seam without replacing MCP's subprocess path.
    audit = tmp_path / "calls.jsonl"
    (tmp_path / "sitecustomize.py").write_text(textwrap.dedent('''\
        import builtins
        import json
        import os
        import sys
        from contextlib import contextmanager
        from pathlib import Path
        from types import SimpleNamespace, ModuleType
        # Fake the SDK before credential scrubbing: the real SDK probes CPUs
        # in Python subprocesses, which would recursively load sitecustomize.
        sys.modules["runpod"] = ModuleType("runpod")
        from tests import runpod_guard
        runpod_guard.scrub_credentials()
        runpod_guard.install()
        assert not runpod_guard.live_opt_in()
        from astrid.core import machines_cli
        from astrid.sdk import AstridClient
        from astrid.sdk.generation import GenerationFacade
        from tests.packs.runpod.test_pack_actions import raw_preferences

        def record(event):
            with open(os.environ["MACHINE_MCP_AUDIT"], "a") as stream:
                stream.write(json.dumps(event) + "\\n")

        def no_prompt(prompt):
            record("prompt")
            raise AssertionError("non-interactive claim prompted")

        builtins.input = no_prompt
        # main's input default was bound at import: detect that prompt as well.
        defaults = machines_cli.main.__kwdefaults__
        defaults["input_fn"] = no_prompt

        @contextmanager
        def open_client(**kwargs):
            record("open")
            yield object()

        def describe(self, **kwargs):
            record("describe")
            return {"waiting_policy": {"preferences": raw_preferences()},
                    "offer": {"hourly_usd": 1.5}}

        rows = []
        def claim(self, **kwargs):
            record("claim")
            assert kwargs["consent"].consent == ("paid",)
            assert not sys.stdin.isatty()
            assert sys.stdin.read() == ""  # MCP supplies DEVNULL, no consent input.
            rows.append({"id": "fake-pod", "live": True, "release_at": 1800000600.0})
            return SimpleNamespace(ok=True, raw_result={"result": {
                "payload": {"target_operation": {"pod_id": "fake-pod"}}}})

        AstridClient.open_from_launcher = staticmethod(open_client)
        GenerationFacade.describe = describe
        GenerationFacade.claim = claim
        machines_cli._assert_release_storage_reserve = lambda: record("storage")
        machines_cli._leases = lambda: rows
    '''))
    monkeypatch.setenv("MACHINE_MCP_AUDIT", str(audit))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + os.pathsep + os.environ["PYTHONPATH"])

    def call(arguments):
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "machines_claim", "arguments": arguments}}
        # A real MCP request and real _run/CLI child. DEVNULL on the request
        # driver prevents an inherited terminal from supplying accidental input.
        driver = """
import json
from astrid import mcp_server
state = {"tools": mcp_server._machine_tools(), "prompts": {}}
print(json.dumps(mcp_server.handle(json.loads(__import__('sys').argv[1]), state)))
"""
        done = subprocess.run([sys.executable, "-c", driver, json.dumps(message)],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=15, check=True)
        reply = json.loads(done.stdout)
        assert "error" not in reply, reply
        calls = [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
        return reply["result"], calls

    return call


def test_mcp_claim_without_paid_consent_refuses_before_provider(machine_mcp_process):
    result, calls = machine_mcp_process({"max_usd": 2.0})
    assert result["isError"] is True
    assert result["structuredContent"] == {
        "ok": False,
        "error": "claim requires paid consent; review the policy and pass --consent paid",
    }
    assert len(result["content"]) == 1  # no policy output or prompt
    assert calls == []  # no Runtime, quote, rental, create or prompt attempt


def test_mcp_claim_with_paid_consent_reaches_fake_claim(machine_mcp_process):
    result, calls = machine_mcp_process({"max_usd": 2.0, "consent": ["paid"]})
    assert result["isError"] is False
    assert result["structuredContent"]["machine"]["id"] == "fake-pod"
    assert calls == ["open", "storage", "describe", "storage", "claim"]
    assert "consent" in mcp_server._machine_tools()["machines_claim"]["definition"]["inputSchema"]["properties"]


def test_non_tty_claim_refuses_even_with_injected_input(monkeypatch, capsys):
    import io

    from astrid.core import machines_cli

    calls = []

    def forbidden(*args, **kwargs):
        calls.append("unexpected call")
        raise AssertionError("non-interactive claim reached a quote, claim or prompt")

    monkeypatch.setattr(sys, "stdin", io.StringIO())
    monkeypatch.setattr(machines_cli, "_assert_release_storage_reserve", lambda: None)
    assert machines_cli.main(["claim", "--max-usd", "2"],
                             describe=forbidden, claim=forbidden, input_fn=forbidden) == 2
    assert calls == []
    captured = capsys.readouterr()
    assert captured.out == "claim requires paid consent; review the policy and pass --consent paid\n"
    assert captured.err == ""
