import assert from 'node:assert/strict';
import test from 'node:test';
import { clickAndObserveEditorNavigation, promptTitleFromSummary, selectedEditorUrl, waitAt } from './journey_required_update_browser.mjs';
import { clickAndObserveGuardedReload } from './journey_reload_inflight_browser.mjs';

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

function guardedReloadHarness({ navigation, clickError, trusted = true }) {
  const config = { origin: 'https://astrid.haus', projectId: 'p1', timelineId: 't1' };
  const response = (url) => ({ ok: () => true, url: () => url,
    request: () => ({ isNavigationRequest: () => true }), frame: () => 'main-frame' });
  let binding;
  let commit;
  let navigationError;
  const page = {
    mainFrame: () => 'main-frame',
    exposeBinding: async (_name, callback) => { binding = callback; },
    waitForNavigation: () => new Promise((resolve, reject) => {
      commit = resolve;
      navigationError = reject;
    }),
  };
  const button = {
    evaluate: async () => {},
    click: async () => {
      if (trusted) binding({ frame: page.mainFrame() });
      setImmediate(() => {
        if (navigation) commit(response(navigation));
        else navigationError(new Error('navigation timeout'));
      });
      if (clickError) throw clickError;
    },
  };
  return { page, button, config };
}

test('guarded reload accepts pinned commit when click acknowledgement times out after trusted click', async () => {
  const h = guardedReloadHarness({ navigation: 'https://astrid.haus/tools/video-editor?project=p1&timeline=t1', clickError: new Error('ack timeout') });
  const outcome = await clickAndObserveGuardedReload(h.page, h.button, h.config);
  assert.equal(outcome.click_observed, true);
  assert.equal(outcome.response.url(), 'https://astrid.haus/tools/video-editor?project=p1&timeline=t1');
});

test('guarded reload rejects click acknowledgement failure without navigation', async () => {
  const h = guardedReloadHarness({ clickError: new Error('ack timeout') });
  await assert.rejects(clickAndObserveGuardedReload(h.page, h.button, h.config), /ack timeout/);
});

test('guarded reload rejects wrong navigation despite click acknowledgement failure', async () => {
  const h = guardedReloadHarness({ navigation: 'https://astrid.haus/tools/video-editor?project=p2&timeline=t1', clickError: new Error('ack timeout') });
  await assert.rejects(clickAndObserveGuardedReload(h.page, h.button, h.config), /selected editor did not commit/);
});

test('guarded reload requires a trusted click even when the selected document commits', async () => {
  const h = guardedReloadHarness({
    navigation: 'https://astrid.haus/tools/video-editor?project=p1&timeline=t1', trusted: false,
  });
  await assert.rejects(clickAndObserveGuardedReload(h.page, h.button, h.config), /did not observe a trusted Reload click/);
});
