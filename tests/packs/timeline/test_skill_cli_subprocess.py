"""Execute every fenced timeline skill command through the built CLI subprocess."""
from __future__ import annotations

import collections
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from tests.packs.timeline import skill_cli_subprocess_support as support

ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = Path(__file__).with_name("skill_cli_scenarios.yaml")
BOOTSTRAP = "import os\nif os.environ.get('ASTRID_SKILL_CLI_TRANSPORT') == '1':\n    from tests.packs.timeline.skill_cli_subprocess_support import install\n    install()\n"


def _scenario_data() -> list[dict]:
    return yaml.safe_load(SCENARIOS.read_text(encoding="utf-8"))["scenarios"]


def _skill_tokens() -> list[tuple[str, ...]]:
    from tests.verify_skill_cli_commands import fenced_cli_commands

    path = ROOT / "astrid/packs/timeline/docs/SKILL.md"
    return [tuple(tokens) for _line, tokens in fenced_cli_commands(path)]


def _scenario_tokens(scenario: dict) -> collections.Counter[tuple[str, ...]]:
    commands = [*scenario.get("setup_commands", []), *scenario["commands"]]
    parsed = []
    for raw in commands:
        tokens = tuple(shlex.split(str(raw)))
        assert tokens[:3] == ("python3", "-m", "astrid"), (scenario["id"], raw)
        parsed.append(tokens[3:])
    return collections.Counter(parsed)


def _bootstrap_env(root: Path, state_path: Path, *, fast_watch: bool = False) -> dict[str, str]:
    boot = root / "bootstrap"
    boot.mkdir(parents=True, exist_ok=True)
    (boot / "sitecustomize.py").write_text(BOOTSTRAP, encoding="utf-8")
    env = os.environ.copy()
    env.update({
        "ASTRID_SKILL_CLI_TRANSPORT": "1",
        "ASTRID_SKILL_CLI_STATE": str(state_path),
        "ASTRID_SKILL_CLI_CALLS": str(root / "fake-transport.jsonl"),
        "BANODOCO_LOCAL_DATA_ROOT": str(root / "Astrid-data"),
        "ASTRID_HOME": str(root / "astrid-home"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "PYTHONPYCACHEPREFIX": str(root / "pycache"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((str(boot), str(ROOT), env.get("PYTHONPATH", ""))),
        # If the sitecustomize bridge ever fails, the SDK rejects this partial
        # eval context before launcher discovery and cannot fall through live.
        "ASTRID_TIMELINE_EVAL_ENDPOINT": "local-fake-only",
    })
    if fast_watch:
        env["ASTRID_SKILL_CLI_FAST_WATCH"] = "1"
    return env


def _run_command(raw: str, env: dict[str, str], *, fixture: str,
                 scenario_id: str, ordinal: int, log_root: Path) -> dict:
    tokens = shlex.split(str(raw))
    command = [sys.executable, *tokens[1:]]
    started = time.perf_counter()
    completed = subprocess.run(command, cwd=ROOT, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=30, check=False)
    runtime_s = time.perf_counter() - started
    log = ("$ " + shlex.join(command) + "\n" + completed.stdout +
           ("\n[stderr]\n" + completed.stderr if completed.stderr else ""))
    log_path = log_root / f"{scenario_id}-{ordinal:02d}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log, encoding="utf-8")
    return {
        "command": shlex.join(command), "cwd": str(ROOT), "runtime_s": round(runtime_s, 6),
        "exit": completed.returncode, "fixture": fixture,
        "log": str(log_path), "log_sha256": hashlib.sha256(log.encode()).hexdigest(),
        "stdout": completed.stdout, "stderr": completed.stderr,
    }


def _prepare_agent_sync_fixture(root: Path, monkeypatch) -> tuple[Path, str]:
    """Create accepted R1 and a later same-content R2 via the existing Runtime fixture."""
    from astrid.core.timeline import scene_answers
    from astrid.sdk.timeline import saved_publish, store
    from astrid.sdk.timeline.checkout import Checkout
    from tests.sdk.test_timeline_agent_sync import SharedRuntime

    monkeypatch.setenv("BANODOCO_LOCAL_DATA_ROOT", str(root / "Astrid-data"))
    monkeypatch.setattr(Checkout, "SAY_SAVED", False)
    monkeypatch.setattr(Checkout, "_native_outline_integrity", lambda *_args, **_kwargs: None)

    class SceneAnswers:
        def query_scene(self, *, revision_digest, scene, frames):
            return [dict(frame=frame, coverage="COVERED", content="CONTENT", findings=[], layers=[])
                    for frame in frames]

    monkeypatch.setattr(scene_answers, "_client", lambda: SceneAnswers())
    runtime = SharedRuntime()
    client = runtime.facade()
    monkeypatch.setattr(store, "resolve_ids", lambda *_args, **_kwargs: ("p", "t", runtime.current))
    monkeypatch.setattr(store, "fetch_bundle", lambda *_args, revision_id=None, **_kwargs:
                        saved_publish._exact(client._transport, {"project_id": "p", "timeline_id": "t"},
                                             revision_id or runtime.current))
    draft = Checkout.draft("p", "t", name="working", client=client)
    from astrid.sdk.timeline.commands import run as run_command

    run_command(draft, {"op": "clip.set", "select": "c1.rocket", "args": {"x": 90}})
    runtime.principal = "agent:alice"

    def republish_same_revision() -> None:
        previous, previous_pub = runtime.current, runtime.publication()
        runtime.parents["R2"] = {**runtime.parents[previous], "revision_id": "R2"}
        runtime.event("R2", "pub-R2", previous, previous_pub)

    runtime.after_accept = republish_same_revision
    accepted = draft.publish("Alice x", client=client)
    assert accepted["accepted_revision_id"] == "accepted-1"
    assert runtime.current == "R2"

    bundles = {rid: saved_publish._exact(client._transport, {"project_id": "p", "timeline_id": "t"}, rid)
               for rid in runtime.parents}
    history = [dict(row, project_id="p", timeline_id="t") for row in runtime.history[0]]
    state_path = root / "runtime-state.json"
    fixture_identity = "test_timeline_agent_sync.SharedRuntime: accepted R1=accepted-1, observed R2=R2"
    digest = support.write_state(state_path, head=runtime.current, bundles=bundles,
                                 closures={rid: runtime.closure(rid) for rid in runtime.parents},
                                 history=history, fixture_identity=fixture_identity)
    return state_path, f"{fixture_identity}; state_sha256={digest}"


@pytest.mark.parametrize("scenario", _scenario_data(), ids=lambda scenario: scenario["id"])
def test_skill_cli_recipe_scenarios_run_built_entrypoint(scenario, tmp_path, monkeypatch) -> None:
    scenario_root = tmp_path / scenario["id"]
    state_path = scenario_root / "runtime-state.json"
    if scenario["fixture"].startswith("two-agents/"):
        state_path, fixture_identity = _prepare_agent_sync_fixture(scenario_root, monkeypatch)
    else:
        state_hash = support.write_simple_state(
            state_path, publish_then_r2=scenario.get("publish_then_r2", False))
        fixture_identity = (f"tiny_publishable/demo-main; publish_then_r2={scenario.get('publish_then_r2', False)}; "
                            f"state_sha256={state_hash}")

    env = _bootstrap_env(scenario_root, state_path, fast_watch=scenario.get("fast_watch_clock", False))
    logs = scenario_root / "logs"
    results: list[dict] = []
    for ordinal, raw in enumerate([*scenario.get("setup_commands", []), *scenario["commands"]], start=1):
        result = _run_command(raw, env, fixture=fixture_identity, scenario_id=scenario["id"],
                               ordinal=ordinal, log_root=logs)
        assert result["exit"] == 0, f"{scenario['id']} failed: {result['command']}\n{result['stdout']}\n{result['stderr']}"
        results.append(result)

    # The sync recipe demonstrates that a saved accepted receipt and the
    # public observed head remain distinct across status and refresh.
    if scenario["id"] == "timeline-skill-sync":
        watch = json.loads(results[0]["stdout"])
        assert watch["data"]["accepted_revision"] == "accepted-1"
        assert watch["data"]["observed_head"] == "R2"
        refreshed = json.loads(results[1]["stdout"])
        assert refreshed["data"]["status"] == "refreshed"
        assert refreshed["data"]["observed_head"] == "R2"

    calls = scenario_root / "fake-transport.jsonl"
    assert calls.is_file() and calls.read_text(encoding="utf-8").strip(), scenario["id"]
    if scenario["id"] == "timeline-skill-edit-loop":
        assert "plan: view contact" in results[4]["stdout"]
        publish = json.loads(results[5]["stdout"])
        accepted_revision = publish["data"]["accepted_revision_id"]
        assert accepted_revision == "accepted-1"
        before_refresh, refreshed, after_refresh = (json.loads(results[index]["stdout"])
                                                    for index in (6, 7, 8))
        assert (before_refresh["data"]["accepted_revision"],
                before_refresh["data"]["observed_head"]) == (accepted_revision, "R2")
        assert refreshed["data"]["status"] == "refreshed"
        assert refreshed["data"]["observed_head"] == "R2"
        assert (after_refresh["data"]["base_revision"],
                after_refresh["data"]["accepted_revision"],
                after_refresh["data"]["observed_head"]) == ("R2", accepted_revision, "R2")
        transport_rows = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
        accepted_calls = [row for row in transport_rows if row["route"] == "commit_timeline"]
        assert len(accepted_calls) == 1
        assert accepted_calls[0]["accepted_revision_id"] == accepted_revision
        assert accepted_calls[0]["observed_head"] == "R2"
        assert any(row["route"] == "get_project_parent_composition_revision" and row["revision"] == "R2"
                   for row in transport_rows)
        # Each watch and refresh uses the existing public revision-only
        # metadata read; it must not invent publication or update metadata.
        head_reads = [row for row in transport_rows if row["route"] == "head_canonical"]
        assert len(head_reads) == 3
    if scenario["id"] == "timeline-skill-history-revert":
        assert "journal" in results[3]["stdout"].lower()
        assert any('"route": "restore_parent_composition_revision"' in line
                   for line in calls.read_text(encoding="utf-8").splitlines())

    manifest_path = tmp_path / "skill-cli-evidence.json"
    prior = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else []
    prior.extend({key: value for key, value in item.items() if key not in {"stdout", "stderr"}}
                 for item in results)
    manifest_path.write_text(json.dumps(prior, indent=2, sort_keys=True), encoding="utf-8")


def test_every_fenced_skill_command_has_a_subprocess_scenario() -> None:
    doc_commands = collections.Counter(_skill_tokens())
    scenario_commands: collections.Counter[tuple[str, ...]] = collections.Counter()
    for scenario in _scenario_data():
        scenario_commands.update(_scenario_tokens(scenario))
    missing = doc_commands - scenario_commands
    assert not missing, f"fenced commands without a built-CLI scenario: {list(missing.elements())}"
