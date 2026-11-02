from __future__ import annotations

from pathlib import Path

import yaml

from astrid.core.hivemind.executors.contribute.run import build_parser


ROOT = Path(__file__).resolve().parents[3]


def test_contribute_manifest_values_and_input_flags_parse():
    manifest = yaml.safe_load((ROOT / "astrid/core/hivemind/pack.yaml").read_text())
    action = manifest["actions"]["contribute"]
    parser = build_parser()

    type_input = next(item for item in action["inputs"] if item["name"] == "type")
    for operation in type_input["enum"]:
        parser.parse_args(["--type", operation])

    mappings = action["invocation"]["command"]["input_args"]
    for mapping in mappings:
        assert mapping["flag"] in parser._option_string_actions
        argv = ["--type", "submit-resource", mapping["flag"]]
        if mapping["input"] != "dry_run":
            argv.append("value")
        parser.parse_args(argv)
