import assert from 'node:assert/strict';
import { writeFileSync, existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, it } from 'bun:test';
import { chatRegistrationPath, chatRegistration, writeChatRegistration, removeChatRegistration } from './chat-registration.ts';
const temporaryDirectories: string[] = [];
afterEach(async () => { await Promise.all(temporaryDirectories.splice(0).map(directory => rm(directory, { recursive: true, force: true }))); });

describe('agent chat registration', () => {
  it('sits next to discovery.json as chat.json', () => {
    assert.equal(chatRegistrationPath('/d/discovery.json'), join('/d', 'chat.json'));
  });

  it('is the exact endpoint and token the Runtime door reads', () => {
    assert.deepEqual(chatRegistration(17335, 'token-value'), { endpoint: 'http://127.0.0.1:17335', token: 'token-value' });
  });

  it('refuses a bad bridge port or an empty credential', () => {
    for (const port of ['0', '70000', 'x']) {
      assert.throws(() => chatRegistration(port, 'token-value'), /chat bridge port/, port);
    }
    assert.throws(() => chatRegistration('17335', ''), /needs a credential/);
  });

  it('writes the registration owner-only, parseable, with no temporary file left behind', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'astrid-chat-registration-test-'));
    temporaryDirectories.push(directory);
    const path = join(directory, 'chat.json');
    const registration = chatRegistration(17335, 'token-value');
    writeChatRegistration(path, registration);
    assert.equal(statSync(path).mode & 0o777, 0o600);
    assert.deepEqual(JSON.parse(readFileSync(path, 'utf8')), registration);
    assert.deepEqual(readdirSync(directory), ['chat.json']);
  });

  it('removes only a registration the predicate owns, and tolerates missing or unparsable files', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'astrid-chat-remove-test-'));
    temporaryDirectories.push(directory);
    const path = join(directory, 'chat.json');
    writeChatRegistration(path, chatRegistration(17335, 'token-value'));
    removeChatRegistration(path, (registration) => registration.token === 'someone-else');
    assert.equal(existsSync(path), true, 'a registration another service owns is left alone');
    removeChatRegistration(path, (registration) => registration.token === 'token-value');
    assert.equal(existsSync(path), false);
    assert.doesNotThrow(() => removeChatRegistration(path, () => true));
    writeFileSync(path, '{not json');
    assert.doesNotThrow(() => removeChatRegistration(path, () => true));
    assert.equal(existsSync(path), true, 'an unparsable file is not ours to remove');
  });
});

