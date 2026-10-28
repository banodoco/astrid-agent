"""``astrid action run``: run one public action once in an ephemeral local Runtime.

    astrid action run h3_av.transform --request request.json --asset-map asset-map.json \\
        --placement local --out OUT

The verb creates a throw-away workspace under a private temporary directory, starts a
Runtime and pack host for it, invokes the public action through the SDK
(``AstridClient.invoke_result``), and stops the Runtime again.  The action writes its
outputs, manifest, receipt and operation journal under ``--out``; nothing durable is
kept outside it.  The exit status is 0 only when the action completed.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

PROFILE = "astrid"
REALM_ID = "action-run"
PROJECT_SLUG = "action-run"
RECEIPT_NAME = "08-final-receipt.json"

# Public actions this verb knows how to feed.  Each maps CLI file flags to action input ports.
ACTIONS: Mapping[str, Mapping[str, str]] = {
    "h3_av.transform": {"request": "request", "asset_map": "asset_map"},
}
PLACEMENTS = ("local",)
LOCAL_ENGINE = "vibecomfy-h3"
_LIFECYCLE_TIMEOUT = 300.0


class ActionRunError(RuntimeError):
    """The ephemeral Runtime or the action could not be run to completion."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # noqa: D401 - argparse hook
        self.exit(2, f"astrid action: {message}; use --help\n")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="astrid action", description="Run a public action in an ephemeral local Runtime.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    run = commands.add_parser(
        "run", help="start a throw-away Runtime, run one public action, stop the Runtime",
        description=(
            "Start an ephemeral local Astrid Runtime and workspace, run the public action with the "
            "given request and asset map, write outputs, manifest, receipt and journal under --out, "
            "then stop the Runtime. Exit 0 only when the action completes."
        ),
    )
    run.add_argument("action_id", metavar="ACTION", choices=sorted(ACTIONS), help="public action id")
    run.add_argument("--request", type=Path, required=True, help="action request JSON")
    run.add_argument("--asset-map", type=Path, required=True, help="JSON map of request asset ids to local files")
    run.add_argument("--placement", choices=PLACEMENTS, required=True, help="where the engine runs (local only)")
    run.add_argument("--out", type=Path, required=True, help="directory that receives the action's outputs")
    run.add_argument("--timeout", type=float, default=3600.0, metavar="SECONDS",
                     help="time limit for the action (default: 3600)")
    return parser


def _source_manifest(path: Path) -> Path:
    """Write the Runtime source profile for the code that is running this command."""
    import astrid
    from importlib.util import find_spec

    spec = find_spec("astrid_runtime")
    if spec is None or not spec.origin:
        raise ActionRunError("the Astrid Runtime is not installed; run the Astrid install line first")
    path.write_text(json.dumps({
        "profile": PROFILE,
        "source_checkout": str(Path(astrid.__file__).resolve().parents[1]),
        "runtime_checkout": str(Path(spec.origin).resolve().parents[1]),
        "runtime_environment": sys.prefix,
    }), encoding="utf-8")
    return path


def _runtime(*args: str, env: Mapping[str, str]) -> str:
    done = subprocess.run([sys.executable, "-m", "astrid_runtime", *args], env=dict(env), capture_output=True,
                          text=True, timeout=_LIFECYCLE_TIMEOUT, check=False)
    if done.returncode:
        told = " | ".join(text.strip()[-600:] for text in (done.stderr, done.stdout) if text.strip())
        raise ActionRunError(f"astrid_runtime {args[0]} failed (exit {done.returncode}): {told or 'no output'}")
    return done.stdout


@contextlib.contextmanager
def _environment(values: Mapping[str, str]) -> Iterator[None]:
    prior = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def ephemeral_runtime(root: Path) -> Iterator[Mapping[str, str]]:
    """Create a fresh workspace under ``root``; yield the environment that selects it; stop it after."""
    support, realm = root / "support", root / "realm"
    manifest = _source_manifest(root / "source-profile.json")
    env = dict(os.environ, ASTRID_RUNTIME_DATA_ROOT=str(support), ASTRID_RUNTIME_SOURCE_MANIFEST=str(manifest))
    common = ["--profile", PROFILE, "--data-root", str(support), "--source-manifest", str(manifest)]
    _runtime("workspace", "create", *common, "--realm-root", str(realm), "--realm-id", REALM_ID,
             "--hosted-origin", "off", "--json", env=env)
    try:
        yield env
    finally:
        # Stop whatever the action started, even when it failed; a stop failure must not hide the cause.
        from astrid.sdk.upgrade import UpgradeError, stop_pack_host

        for what, stop in (
            ("pack host", lambda: stop_pack_host(support)),
            ("Runtime", lambda: _runtime("down", *common, "--json", env=env)),
        ):
            try:
                stop()
            except (ActionRunError, UpgradeError, subprocess.SubprocessError, OSError) as exc:
                print(f"astrid action: warning: could not stop the ephemeral {what} cleanly: {exc}", file=sys.stderr)


def _submission_id(inputs: Mapping[str, Path]) -> str:
    digest = hashlib.sha256()
    for name in sorted(inputs):
        digest.update(name.encode() + b"\0" + inputs[name].read_bytes() + b"\0")
    return "action-" + digest.hexdigest()[:32]


def _local_engine_environment() -> Mapping[str, str]:
    """Placement local: tell the pack host to derive its model-root binding from the installed engine.

    No readiness profile exists for an ephemeral host.  The host calls ``local_model_root_binding`` (the
    install receipt's verified model lock plus the engine root) the first time a VibeComfy task needs the
    binding (the client starts that host from this process's environment).  A missing or unverified install
    is refused there, naming ``astrid engines install``, after ``h3_av.transform`` has already refused an
    engine that is not installed and healthy.
    """
    from astrid.core.generation.engine_local_endpoint import LOCAL_ENGINE_HOST_ENV

    return {LOCAL_ENGINE_HOST_ENV: LOCAL_ENGINE}


def run_action(args: argparse.Namespace, *, workspace: Path) -> dict[str, Any]:
    """Run ``args.action_id`` in a fresh Runtime under ``workspace``; return the JSON summary."""
    from astrid.sdk.client import AstridClient

    files = {"request": args.request.resolve(), "asset_map": args.asset_map.resolve()}
    for name, path in files.items():
        if not path.is_file():
            raise ActionRunError(f"--{name.replace('_', '-')} is not a file: {path}")
    out = args.out.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    ports = ACTIONS[args.action_id]
    engine_env = _local_engine_environment() if args.placement == "local" else {}
    with ephemeral_runtime(workspace) as env, _environment(
        {**{key: env[key] for key in ("ASTRID_RUNTIME_DATA_ROOT", "ASTRID_RUNTIME_SOURCE_MANIFEST")}, **engine_env}
    ):
        client = AstridClient.open_from_launcher(client_name="astrid-action", client_version="1")
        project = client.projects.create(slug=PROJECT_SLUG, name="Action run", idempotency_key="action-run-project")
        if not project.ok:
            raise ActionRunError(f"could not create the workspace project: {project.error}")
        project_id = str(project.data["project_id"])
        result = client.invoke_result(
            args.action_id,
            kind="orchestrator",
            python_exec=sys.executable,
            project=project_id,
            out=out,
            inputs={**{ports[name]: str(path) for name, path in files.items()}, "project": project_id},
            idempotency_context={"h3_submission_id": _submission_id(files)},
            wait=True,
            timeout_seconds=args.timeout,
            poll_seconds=0.25,
        )
    receipt = out / RECEIPT_NAME
    summary: dict[str, Any] = {
        "action": args.action_id,
        "ok": bool(result.ok),
        "placement": args.placement,
        "out": str(out),
        "receipt": str(receipt) if receipt.is_file() else None,
        "manifest": result.manifest_path,
        "run_id": result.run_id,
    }
    if not result.ok:
        summary["error"] = result.error
    return summary


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    # AF_UNIX paths are short; keep the throw-away workspace in a short private temp directory.
    workspace = Path(tempfile.mkdtemp(prefix="astrid-action-"))
    try:
        summary = run_action(args, workspace=workspace)
    except ActionRunError as exc:
        print(json.dumps({"action": args.action_id, "ok": False, "error": str(exc)}, indent=2, sort_keys=True))
        return 1
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
