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
const after = { project_id: 'project', timeline_id: 'timeline',
  parent_revision: { payload: { config: { tracks: [{ id: 'V1', label: config.draftLabel }] } } } };
const payload = { after, ['__proto__']: 'opaque saved key' };
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


def test_durable_draft_recognizes_released_document_patch_track_rename():
    program = r"""
import assert from 'node:assert/strict';
import { findDraft, sha256 } from './scripts/journey_required_update_browser.mjs';

const config = { projectId: 'project', timelineId: 'timeline', draftLabel: 'Full draft' };
const path = '/v1/projects/project/timelines/timeline/commit';
const fields = { op: 'set', ref: 'parent', before: { state: 'present', value: 'Saved' }, value: config.draftLabel };
const tracks = [{ id: 'V1', label: config.draftLabel }];
const after = { project_id: 'project', timeline_id: 'timeline', parent_revision: { payload: { config: { tracks } } } };
const row = {
  scope: { projectId: 'project', timelineId: 'timeline', realmId: 'realm', actorId: 'actor' },
  ownerId: 'owner',
  intents: [{ id: 'draft-intent', payload: { kind: 'local-save', after } }],
  pending: { intentId: 'draft-intent', request: { method: 'POST', path, key: 'original-key' } },
};
function setChange(...path) {
  row.pending.request.body = JSON.stringify({
    commands: [{ op: 'doc.patch', select: null, args: { changes: [{ ...fields, path }] } }],
  });
}
const page = { evaluate: async () => JSON.stringify([row]) };
for (const prefix of [[], ['config']]) {
  setChange(...prefix, 'tracks', 'id:V1', 'label');
  const observed = await findDraft(page, config);
  assert.ok(observed, 'the shipped document patch must bind the full V1 label');
  assert.equal(observed.draft.requestSha256, sha256(row.pending.request.body));
}
setChange('config', 'tracks', 'id:V2', 'label');
assert.equal(await findDraft(page, config), undefined, 'another track cannot stand in for the draft');
setChange('config', 'tracks', 'id:V1', 'label');
tracks[0].label = 'partial';
assert.equal(await findDraft(page, config), undefined, 'the persisted after-image must carry the same complete label');
"""
    subprocess.run(
        ["node", "--input-type=module", "-"], input=program, text=True, cwd=ROOT, check=True, timeout=15,
    )
