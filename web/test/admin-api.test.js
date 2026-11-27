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
