import { expect } from '@playwright/test';
// WIP: stage this file in the App's tests/e2e/local directory before invoking
// Playwright. It is not a journey receipt or a substitute for test 16.
import { test, openApp, expectConnected, openSeededTimeline, currentHead, editFirstTrackName, holdCommandRequests, commandOutboxes } from './support/app.ts';
import { skipWhen } from '../support/declaredSkip.ts';

test.beforeEach(() => skipWhen('direct-journey-harness'));
test('App: draft first, then second writer; conflict keeps the draft', async ({ page, harness }) => {
  await openApp(page, harness);
  await expectConnected(page);
  await openSeededTimeline(page, harness);
  const path = `/v1/projects/${harness.seed.projectId}/timelines/${harness.seed.timelineId}`;
  const initial = await harness.runtimeJson('GET', path);
  expect(initial.status).toBe(200);
  const head = initial.body as Record<string, unknown>;
  expect(typeof head.head_revision_id).toBe('string');
  expect(typeof head.head_publication_id).toBe('string');
  const retired = await harness.runtimeJson('POST', `${path}/composition-revisions`, {});
  expect(retired.status).toBe(410);
  expect(retired.body).toMatchObject({ code: 'retired_route' });
  const held = await holdCommandRequests(page);
  try {
    await editFirstTrackName(page, 'Draft from the first writer');
    await expect.poll(() => held.requests.length, { timeout: 20_000 }).toBe(1);
    const request = held.requests[0];
    expect(request.postDataJSON()).toMatchObject({
      base_revision_id: head.head_revision_id,
      base_publication_id: head.head_publication_id,
      commands: [{ op: 'track.set', select: 'parent/track:V1', args: { values: { label: 'Draft from the first writer' }, clear: [] } }],
    });
    const outbox = (await commandOutboxes(page, harness.seed.timelineId)).find(
      (owner) => owner.pending?.request.key === request.headers()['idempotency-key'],
    );
    expect(outbox).toBeTruthy();
    expect(outbox?.pending?.request.body).toBe(request.postData());
    expect(outbox?.pending?.outcome).toBeUndefined();
    await expect(page.getByRole('textbox', { name: 'Track name' }).first()).toHaveValue('Draft from the first writer');
    expect(await currentHead(harness)).toBe(head.head_revision_id);
    const second = await harness.runtimeJson('POST', `${path}/commit`, {
      api_version: 1, project_id: harness.seed.projectId, timeline_id: harness.seed.timelineId,
      base_revision_id: head.head_revision_id, base_publication_id: head.head_publication_id,
      commands: [{ schema_version: 1, op: 'track.set', version: '1', select: 'parent/track:V1', args: { values: { label: 'Second writer' }, clear: [] } }], source: { via: 'app' },
    });
    expect(second.status).toBe(200);
    const revisionId = second.body?.data?.accepted_revision_id;
    const publicationId = second.body?.receipt?.receipt_id;
    expect(typeof revisionId).toBe('string');
    expect(typeof publicationId).toBe('string');
    const response = page.waitForResponse((reply) => reply.request() === request, { timeout: 20_000 });
    await held.release(true);
    expect((await response).status()).toBe(409);
    const conflict = page.getByRole('alertdialog', { name: 'This timeline changed somewhere else' });
    await expect(conflict).toBeVisible({ timeout: 20_000 });
    await expect(conflict.getByText('Keep your changes, or switch to the latest version.')).toBeVisible();
    await expect(conflict.getByRole('button', { name: 'Keep my changes' })).toBeVisible();
    await expect(conflict.getByRole('button', { name: 'Use latest version' })).toBeVisible();
    await expect(page.getByRole('textbox', { name: 'Track name', includeHidden: true }).first()).toHaveValue('Draft from the first writer');
    expect(await currentHead(harness)).toBe(revisionId);
  } finally {
    await held.release(false);
  }
});
