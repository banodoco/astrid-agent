"""The console and installer module retain their early timeline start mark."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("route", ["console", "module"])
@pytest.mark.parametrize("probe", ["fallback", "process"])
def test_timeline_start_precedes_launcher_imports(tmp_path, route, probe):
    (tmp_path / "sitecustomize.py").write_text(
        "import builtins, sys, time\n"
        "from astrid import console\n"
        f"console.process_started_at = lambda fallback: fallback {'- 50' if probe == 'process' else ''}\n"
        "_original_import = builtins.__import__\n"
        "_delayed = False\n"
        "def _import(name, *args, **kwargs):\n"
        "    global _delayed\n"
        "    if name.endswith('core.auth') and not _delayed:\n"
        "        _delayed = True\n"
        "        time.sleep(0.2)\n"
        "    module = _original_import(name, *args, **kwargs)\n"
        "    if name == 'astrid.core.gateway' and not hasattr(module, '_capture_wrapped'):\n"
        "        original_main = module.main\n"
        "        def main(*args, **kwargs):\n"
        "            print('GATEWAY_AGE', time.monotonic() - kwargs['started_at'], file=sys.stderr)\n"
        "            return original_main(*args, **kwargs)\n"
        "        module.main = main\n"
        "        module._capture_wrapped = True\n"
        "    return module\n"
        "builtins.__import__ = _import\n"
        "_read = console.cli_started_at\n"
        "def _observed():\n"
        "    start = _read()\n"
        "    print('START_AGE', time.monotonic() - start, file=sys.stderr)\n"
        "    return start\n"
        "console.cli_started_at = _observed\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(root), env.get("PYTHONPATH", "")))
    command = ([str(Path(sys.executable).with_name("astrid"))] if route == "console"
               else [sys.executable, "-m", "astrid.omp_agent"])
    result = subprocess.run(command + ["timelines", "--help"], cwd=root, env=env,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    age = float(result.stderr.split("START_AGE ")[1].splitlines()[0])
    assert age >= (50 if probe == "process" else 0.2)
    assert age < (55 if probe == "process" else 5)
    gateway_age = float(result.stderr.split("GATEWAY_AGE ")[1].splitlines()[0])
    assert 0 <= gateway_age - age < 1
