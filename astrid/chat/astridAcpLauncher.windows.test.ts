import { test } from 'bun:test';
import assert from 'node:assert/strict';
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { resolveAstridAcpSystemPromptFile, resolveAstridAgentContext } from './astridAcpLauncher.ts';

test.skipIf(process.platform !== 'win32')('native Windows ACP profile and context lookups can launch a .cmd Astrid shim', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'Astrid & ACP-'));
  const profileDirectory = mkdtempSync(join(tmpdir(), 'astrid-profile-'));
  const profile = join(profileDirectory, 'astrid-profile.md');
  const shim = join(directory, 'astrid.cmd');
  const failingShim = join(directory, 'failing-astrid.cmd');
  writeFileSync(profile, '# Astrid profile\n', 'utf8');
  writeFileSync(shim, [
    '@echo off',
    'if "%~1"=="--agent-profile-file" (',
    '  echo ' + profile,
    '  exit /b 0',
    ')',
    'if "%~1"=="--agent-context" (',
    '  echo context-sentinel',
    '  exit /b 0',
    ')',
    'exit /b 2',
    '',
  ].join('\r\n'), 'utf8');
  writeFileSync(failingShim, '@echo off\r\necho credential-secret-private 1>&2\r\nexit /b 42\r\n', 'utf8');
  try {
    assert.equal(resolveAstridAcpSystemPromptFile(shim), profile);
    assert.equal(await resolveAstridAgentContext(shim), 'context-sentinel');
    assert.throws(() => resolveAstridAcpSystemPromptFile(failingShim), error => {
      assert.match((error as Error).message, /exit=42/);
      assert.doesNotMatch((error as Error).message, /credential-secret-private/);
      return true;
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
    rmSync(profileDirectory, { recursive: true, force: true });
  }
});

test.skipIf(process.platform === 'win32')('profile lookup reports safe status without returning child stderr', () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-profile-failure-'));
  const command = join(directory, 'astrid');
  writeFileSync(command, `#!${process.execPath}\nprocess.stderr.write('credential-secret-private diagnostic'); process.exit(42);\n`, { mode: 0o755 });
  chmodSync(command, 0o755);
  try {
    assert.throws(() => resolveAstridAcpSystemPromptFile(command), error => {
      assert.match((error as Error).message, /exit=42/);
      assert.doesNotMatch((error as Error).message, /credential-secret-private/);
      return true;
    });
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
