"""Public H3 commands, backed by the existing request and transform seams."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="astrid h3", description="Plan and create H3 audiovisual work.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, description in (
        ("plan", "Validate and plan a request without running it"),
        ("run", "Run a request through the H3 transform"),
        ("compose", "Run and compose a request through the H3 transform"),
        ("edit", "Write a validated request spec"),
    ):
        sub = commands.add_parser(name, help=description, description=description)
        sub.add_argument("--spec", type=Path, help="existing v1/v2 JSON or YAML request")
        sub.add_argument("--prompt", help="generation prompt, or replacement prompt when editing a spec")
        sub.add_argument("--duration", type=float, help="output duration in seconds")
        sub.add_argument("--reference", help="image asset identifier for a new generation spec")
        sub.add_argument("--out", type=Path, required=True,
                         help="spec file to write" if name == "edit" else "output directory")
        if name != "edit":
            sub.add_argument("--asset-map", type=Path, help="JSON map of asset identifiers to local files")
    return parser


def _document(args: argparse.Namespace, parser: argparse.ArgumentParser) -> dict[str, Any]:
    from .shared.request import _object, load_request_document

    if args.spec:
        if args.reference is not None:
            parser.error("--reference cannot be combined with --spec")
        if args.command != "edit" and (args.prompt is not None or args.duration is not None):
            parser.error("--spec cannot be combined with --prompt or --duration")
        raw = load_request_document(args.spec)
        if args.command == "edit":
            if args.prompt is not None:
                if raw.get("version") == 2:
                    raw["prompt"] = args.prompt
                else:
                    raw["content"] = _object(raw.get("content", {}), "content") or {}
                    raw["content"]["prompt"] = args.prompt
            if args.duration is not None:
                if raw.get("version") == 2:
                    raw["duration"] = args.duration
                else:
                    raw["output"] = _object(raw.get("output", {}), "output") or {}
                    raw["output"]["duration"] = args.duration
        return raw
    if args.prompt is None or args.duration is None or args.reference is None:
        parser.error("provide --spec or --prompt, --duration and --reference")
    return {"version": 2, "prompt": args.prompt, "duration": args.duration,
            "media": [{"id": "reference", "asset": args.reference, "role": "reference", "modality": "image"}],
            "settings": {}}


def _asset_map(path: Path | None) -> dict[str, str]:
    from .shared.request import H3RequestError

    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise H3RequestError(f"cannot read asset map: {exc}", field="asset_map") from exc
    if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
        raise H3RequestError("asset map must be a JSON object of strings", field="asset_map")
    return value


def _ops(argv: list[str]) -> int:
    # Operator entrypoints retain their existing parsers and behavior.
    if not argv or argv[0] in {"-h", "--help"}:
        print("Usage: astrid h3 ops {action,transform,prepare,compile,compose,verify} ...")
        return 0
    name, rest = argv[0], argv[1:]
    if name == "action":
        from astrid.core.action_cli import main
        return main(rest)
    if name not in {"transform", "prepare", "compile", "compose", "verify"}:
        print(f"astrid h3 ops: unknown command {name!r}", file=sys.stderr)
        return 2
    from importlib import import_module
    return import_module(f"astrid.packs.h3_av.actions.{name}.run").main(rest)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "ops":
        return _ops(raw_args[1:])
    parser = _parser()
    args = parser.parse_args(raw_args)
    from astrid.core.contracts.errors import AstridError, render_astrid_error
    from .shared.request import H3RequestError, normalize_request
    from .shared.spec import canonical_spec

    try:
        document = _document(args, parser)
        request = normalize_request(document)
        spec = canonical_spec(request)
        if args.command == "edit":
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(json.dumps({"spec_path": str(args.out.resolve())}))
            return 0
        assets = _asset_map(args.asset_map)
        if args.command == "plan":
            from .shared.planner import plan_request
            plan = plan_request(request, asset_map=assets, out_dir=args.out)
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        unavailable = sorted({op.op for op in spec.ops if not spec.capabilities[op.op]["available"]})
        if unavailable:
            raise H3RequestError(
                "H3 capabilities unavailable: " + ", ".join(unavailable),
                code="h3.capability.unavailable", field="ops",
                fix="Use a capability with recorded GPU proof",
            )
        # The existing action runner owns Runtime lifecycle and invokes B7 transform.
        from astrid.core.action_cli import main as action_main
        with tempfile.TemporaryDirectory(prefix="astrid-h3-") as temp:
            temp_path = Path(temp)
            request_path = temp_path / "request.json"
            asset_path = temp_path / "assets.json"
            request_path.write_text(json.dumps(document), encoding="utf-8")
            asset_path.write_text(json.dumps(assets), encoding="utf-8")
            return action_main(["run", "h3_av.transform", "--request", str(request_path),
                                "--asset-map", str(asset_path), "--placement", "local", "--out", str(args.out)])
    except AstridError as exc:
        return render_astrid_error(exc)
    except OSError as exc:
        return render_astrid_error(H3RequestError(str(exc)))
