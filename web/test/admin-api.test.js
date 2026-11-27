import assert from 'node:assert/strict';
import test from 'node:test';
import { AdminApiError, adminApi, adminErrorMessage } from '../src/admin-api.js';

test('admin calls stay separate from customer auth and write once with CSRF', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json({}));
  const csrfToken = 'fixture-csrf';
  const controller = new AbortController();

  await adminApi.me(controller.signal);
  await adminApi.login({ username: 'operator', password: 'fixture-password' });
  await adminApi.createProject({ email: 'qa@example.test', password: 'fixture-password' }, csrfToken);
  await adminApi.confirmStatement('project/fixture', '2026-10', '275.00', csrfToken);

  const requests = fetch.mock.calls.map(({ arguments: args }) => args);
  assert.deepEqual(requests.map(([path, options]) => [path, options.method]), [
    ['/api/admin/me', 'GET'],
    ['/api/admin/login', 'POST'],
    ['/api/admin/projects', 'POST'],
    ['/api/admin/projects/project%2Ffixture/statements/2026-10/confirm', 'POST'],
  ]);
  assert.equal(requests[0][1].signal, controller.signal);
  assert.deepEqual(JSON.parse(requests[3][1].body), { expectedPayableAmount: '275.00' });
  for (const [index, [, options]] of requests.entries()) {
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.redirect, 'error');
    assert.equal(options.headers.Authorization, undefined);
    assert.equal(options.headers['X-CSRF-Token'], index < 2 ? undefined : csrfToken);
  }
});

test('changed statement amount is surfaced without replaying the confirmation', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () =>
    Response.json({ detail: { code: 'statement_amount_changed' } }, { status: 409 }),
  );

  await assert.rejects(adminApi.confirmStatement('project', '2026-10', '275.00', 'fixture-csrf'), (error) => {
    assert.ok(error instanceof AdminApiError);
    assert.equal(error.status, 409);
    assert.equal(error.code, 'statement_amount_changed');
    assert.match(adminErrorMessage(error), /刷新账单并重新核对/);
    return true;
  });
  assert.equal(fetch.mock.callCount(), 1);
});

test('admin Key limit updates send both limits to the scoped path with CSRF', async (t) => {
  const updated = { id: 'k_fixture', concurrencyLimit: null, rpmLimit: 45, activeTasks: 3, requestsLastMinute: 11 };
  const fetch = t.mock.method(globalThis, 'fetch', async () => Response.json(updated));

  assert.deepEqual(await adminApi.updateKeyLimits('p/fixture', 'k/fixture', {
    concurrencyLimit: null, rpmLimit: 45,
  }, 'fixture-csrf'), updated);
  const [[path, options]] = fetch.mock.calls.map(({ arguments: args }) => args);
  assert.equal(path, '/api/admin/projects/p%2Ffixture/keys/k%2Ffixture/limits');
  assert.equal(options.method, 'PUT');
  assert.equal(options.headers['X-CSRF-Token'], 'fixture-csrf');
  assert.deepEqual(JSON.parse(options.body), { concurrencyLimit: null, rpmLimit: 45 });
});

test('official price import previews the Beijing time and applies its server token with CSRF', async (t) => {
  const preview = { effectiveAt: '2026-11-28T16:00:00Z', previewToken: 'fixture-preview-token' };
  const applied = { createdCount: 28, skippedCount: 0 };
  const controller = new AbortController();
  const fetch = t.mock.method(globalThis, 'fetch', async (_path, { method }) =>
    Response.json(method === 'GET' ? preview : applied),
  );

  const selection = await adminApi.previewVolcenginePrices('2026-11-29T00:00+08:00', controller.signal);
  assert.deepEqual(selection, preview);
  assert.deepEqual(await adminApi.applyVolcenginePrices(selection, 'fixture-csrf'), applied);

  const [[previewPath, read], [applyPath, write]] = fetch.mock.calls.map(({ arguments: args }) => args);
  assert.equal(previewPath, '/api/admin/prices/volcengine/preview?effectiveAt=2026-11-29T00%3A00%2B08%3A00');
  assert.equal(read.signal, controller.signal);
  assert.equal(read.headers['X-CSRF-Token'], undefined);
  assert.equal(applyPath, '/api/admin/prices/volcengine/apply');
  assert.equal(write.method, 'POST');
  assert.equal(write.headers['X-CSRF-Token'], 'fixture-csrf');
  assert.deepEqual(JSON.parse(write.body), preview);
});

test('a stale official price preview requires a fresh review and does not replay writes', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () =>
    Response.json({ detail: { code: 'official_price_preview_stale' } }, { status: 409 }),
  );
  await assert.rejects(
    adminApi.applyVolcenginePrices({ effectiveAt: '2026-12-01T00:00:00Z', previewToken: 'stale' }, 'fixture-csrf'),
    (error) => {
      assert.equal(error.status, 409);
      assert.match(adminErrorMessage(error), /刷新预览后重新核对/);
      return true;
    },
  );
  assert.equal(fetch.mock.callCount(), 1);
});
