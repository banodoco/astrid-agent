"""Focused configuration checks for the journey workflow's inline preflight."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/journey.yml"


def config_script() -> str:
    source = WORKFLOW.read_text(encoding="utf-8")
    marker = "          python3 - <<'PY'\n"
    start = source.index(marker) + len(marker)
    end = source.index("          PY\n", start)
    return textwrap.dedent(source[start:end])


def run_config(
    direct_os: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    output = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False)
    output_path = output.name
    output.close()
    env.update(
        {
            "CANDIDATE": json.dumps(
                {
                    "app": "a" * 40,
                    "agent": "b" * 40,
                    "runtime": "c" * 40,
                    "pinokio": "d" * 40,
                }
            ),
            "DIRECT_OS": direct_os or "",
            "PYTHONPATH": str(ROOT / "scripts"),
            "DRIVER_AGENT_REF": "e" * 40,
            "WORKFLOW_REPOSITORY": "banodoco/astrid-agent",
            "OLD_LAUNCHER_REF": "bbba620e3d89eeff8021417f365e6ea584203105",
            "STOPPED_PATH_AGENT_REF": "b" * 40,
            "M4_4A_EXECUTOR_CONFIGS": '{"linux":{},"macos":{},"windows":{}}',
            "M4_4A_BACKEND_ONLY": "false",
            "M4_5_ENABLED": "false",
            "M4_5_ONLY": "false",
            "M4_5_BOUNDARY": "",
            "M4_5_BOUNDARY_ARGS": "[]",
            "M4_4B_ENABLED": "false",
            "M4_6_ENABLED": "false",
            "M4_6_CANDIDATE": "",
            "GITHUB_OUTPUT": output_path,
        }
    )
    if extra_env:
        env.update(extra_env)
    try:
        result = subprocess.run(
            ["python3", "-c", config_script()],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            output_text = Path(output_path).read_text(encoding="utf-8")
            result.outputs = dict(line.split("=", 1) for line in output_text.splitlines())
        return result
    finally:
        Path(output_path).unlink(missing_ok=True)


class JourneyWorkflowConfigurationTests(unittest.TestCase):
    def test_each_direct_os_selects_exactly_its_direct_row(self) -> None:
        for os_name in ("macos", "linux", "windows"):
            with self.subTest(os=os_name):
                result = run_config(os_name)
                self.assertEqual(result.returncode, 0, result.stderr)
                rows = json.loads(result.outputs["matrix"])["include"]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["job_id"], f"direct-{os_name}")
                self.assertEqual(rows[0]["mode"], "direct")
                self.assertEqual(rows[0]["leg"], "release0-to-release1")
                self.assertEqual(rows[0]["os"], os_name)

    def test_invalid_direct_os_fails_preflight(self) -> None:
        for value in ("Linux", "all", "windows "):
            with self.subTest(value=value):
                result = run_config(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("direct_os must be one of: macos, linux, windows", result.stderr)

    def test_omitted_selector_keeps_all_nine_rows_and_full_aggregate(self) -> None:
        result = run_config()
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.outputs["matrix"])["include"]
        self.assertEqual(len(rows), 9)
        self.assertEqual(
            [row["job_id"] for row in rows if row["mode"] == "direct"],
            ["direct-macos", "direct-linux", "direct-windows"],
        )
        source = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "needs.config.outputs.direct_os == ''",
            re.search(r"- name: Aggregate the exact Release-1 receipt matrix\n(?P<step>.*?)(?=\n      - name:)", source, re.S)["step"],
        )
        self.assertIn("--aggregate-release1", source)

    def test_selector_rejects_conflicting_optional_modes(self) -> None:
        result = run_config("linux", {"M4_4B_ENABLED": "true"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("direct_os cannot be combined", result.stderr)


if __name__ == "__main__":
    unittest.main()
