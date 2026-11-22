import assert from 'node:assert/strict';
import test from 'node:test';
import { clickAndObserveEditorNavigation, promptTitleFromSummary, selectedEditorUrl, waitAt } from './journey_required_update_browser.mjs';

test('required prompt title omits summary controls from actual summary text', () => {
  assert.equal(promptTitleFromSummary('Update Astrid Required Show how'), 'Update Astrid');
});

test('browser readiness timeout identifies the stalled stage and selector', async () => {
  await assert.rejects(
    waitAt('wait-for-retry-ready', '[data-save-bar] button[name="Try again"]', async () => {
      const error = new Error('locator timeout');
      error.name = 'TimeoutError';
      throw error;
    }),
    (error) => error.name === 'TimeoutError'
      && error.stalled.step === 'wait-for-retry-ready'
      && error.stalled.selector === '[data-save-bar] button[name="Try again"]',
  );
});

test('reload arms main-frame commit before a no-wait click and accepts incidental query changes', async () => {
  const config = { origin: 'https://astrid.haus', projectId: 'p1', timelineId: 't1' };
  let armed = false;
  let options;
  const response = {
    ok: () => true,
    url: () => 'https://astrid.haus/tools/video-editor?project=p1&timeline=t1&source=update',
    request: () => ({ isNavigationRequest: () => true }),
    frame: () => 'main-frame',
  };
  const page = {
    mainFrame: () => 'main-frame',
    waitForNavigation: ({ waitUntil }) => {
      assert.equal(waitUntil, 'commit');
      armed = true;
      return new Promise((resolve) => { page.commit = resolve; });
    },
  };
  const button = { click: async (received) => {
    options = received;
    assert.equal(armed, true);
    page.commit(response);
  } };
  const outcome = await clickAndObserveEditorNavigation(page, button, config);
  assert.equal(outcome.response, response);
  assert.equal(options.noWaitAfter, true);
  assert.equal(options.timeout, 30_000);
  assert.equal(selectedEditorUrl(new URL(response.url()), config), true);
});

test('reload rejects a committed navigation to a different selected editor', async () => {
  const config = { origin: 'https://astrid.haus', projectId: 'p1', timelineId: 't1' };
  const response = {
    ok: () => true,
    url: () => 'https://astrid.haus/tools/video-editor?project=p2&timeline=t1',
    request: () => ({ isNavigationRequest: () => true }),
    frame: () => 'main-frame',
  };
  const page = { mainFrame: () => 'main-frame', waitForNavigation: () => Promise.resolve(response) };
  await assert.rejects(clickAndObserveEditorNavigation(page, { click: async () => {} }, config),
    /selected editor did not commit/);
});
