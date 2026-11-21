"""Regression for Pinokio's async install action result contract."""

import os

from scripts import pinokio_row_driver as driver


def test_launcher_install_action_runs_async_run_object_and_custom_steps(tmp_path):
    """Pinokio install.js exports an async {run: [...]} action, not an array."""

    launcher = tmp_path / "pinokio"
    (launcher / "pinokio").mkdir(parents=True)
    (launcher / "pinokio" / "lib.js").write_text("""
const fs = require('node:fs');
const isWindows = process.platform === 'win32';
const event = (name) => ({ method: async () => fs.appendFileSync(process.env.EVENTS, name + '\\n') });
module.exports = {
  ensureDoorPortFree: async () => fs.appendFileSync(process.env.EVENTS, 'door\\n'),
  ensureProgramDirectory: () => fs.appendFileSync(process.env.EVENTS, 'directory\\n'),
  customStep: method => ({ method }),
  shellStep: message => ({ method: 'shell.run', params: {
    shell: isWindows ? 'powershell.exe' : '/bin/bash', path: process.env.FIX17_STEP_DIR,
    message, env: { STEP_VALUE: 'from-step' },
  } }),
  bootstrapScript: () => isWindows
    ? 'Add-Content -LiteralPath $env:EVENTS -Value $env:STEP_VALUE'
    : `printf '%s\\n' "$STEP_VALUE" >> "$EVENTS"`,
  saveModeStep: () => event('save'),
};
""", encoding="utf-8")
    # Contract fixture mirrors the pinned launcher action shape and step ordering.
    (launcher / "pinokio" / "install.js").write_text("""'use strict';
const app = require('./lib');
module.exports = async () => ({ run: [
  app.customStep(app.ensureDoorPortFree),
  app.customStep(async () => app.ensureProgramDirectory()),
  app.shellStep(app.bootstrapScript('install')),
  app.saveModeStep('backend'),
] });
""", encoding="utf-8")
    events = tmp_path / "events.txt"
    row = driver.Row(job_id="pinokio-linux", os_name="linux", run_root=tmp_path / "run",
                     pinokio=launcher, agent=tmp_path / "agent", app_checkout=tmp_path / "app",
                     candidate_sha="a" * 40)
    row.scratch.mkdir(parents=True)
    row.home.mkdir(parents=True)
    row.env.update(os.environ, EVENTS=str(events), FIX17_STEP_DIR=str(tmp_path))
    row.launcher_install_action("b" * 40)
    assert events.read_text().splitlines() == ["door", "directory", "from-step", "save"]
