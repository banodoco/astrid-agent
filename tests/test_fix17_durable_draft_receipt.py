"""Check that the browser journey binds the complete draft and its saved wire request."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_durable_draft_binds_wire_label_and_unchanged_intent():
    program = r"""
import assert from 'node:assert/strict';
import { findDraft, sha256 } from './scripts/journey_required_update_browser.mjs';

const config = { projectId: 'project', timelineId: 'timeline', draftLabel: 'A complete draft' };
const path = '/v1/projects/project/timelines/timeline/commit';
const payload = { label: config.draftLabel, ['__proto__']: 'opaque saved key' };
const row = {
  scope: { projectId: 'project', timelineId: 'timeline', realmId: 'realm', actorId: 'actor' },
  ownerId: 'owner',
  intents: [{ id: 'draft-intent', payload }],
  pending: { intentId: 'draft-intent', request: { method: 'POST', path, key: 'original-key' } },
};
function setWireLabel(label) {
  row.pending.request.body = JSON.stringify({
    commands: [{ op: 'track.set', select: 'parent/track:V1', args: { values: { label }, clear: [] } }],
  });
}
const page = { evaluate: async () => JSON.stringify([row]) };
setWireLabel(config.draftLabel);
const observed = await findDraft(page, config);
assert.ok(observed, 'the scoped persisted wire request must match the entire draft');
assert.equal(observed.draft.payloadSha256, sha256(JSON.stringify(payload)));
assert.equal(observed.draft.requestSha256, sha256(row.pending.request.body));
assert.equal(observed.draft.keySha256, sha256(row.pending.request.key));
assert.equal((await findDraft(page, { ...config, expectedDraft: observed.draft })).draft.intentId, 'draft-intent');

setWireLabel('A complete');
assert.equal(await findDraft(page, config), undefined, 'an earlier partial save cannot stand in for the draft');
setWireLabel(config.draftLabel);
row.pending.request.key = 'different-key';
assert.equal(await findDraft(page, { ...config, expectedDraft: observed.draft }), undefined);
"""
    subprocess.run(
        ["node", "--input-type=module", "-"], input=program, text=True, cwd=ROOT, check=True, timeout=15,
    )
