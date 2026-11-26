import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, customerApi } from '../src/api.js';

const json = (value, status = 200) => Response.json(value, { status });

test('customer read requests use same-origin cookies, no browser caching and explicit usage month', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => json({}));
  const controller = new AbortController();
  await customerApi.me(controller.signal);
  await customerApi.keys(controller.signal);
  await customerApi.usage('2026-11', controller.signal);

  assert.deepEqual(
    fetch.mock.calls.map(({ arguments: [path] }) => path),
    ['/api/customer/me', '/api/customer/keys', '/api/customer/usage?month=2026-11'],
  );
  for (const {
    arguments: [, options],
  } of fetch.mock.calls) {
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.signal, controller.signal);
    assert.equal(options.headers['X-CSRF-Token'], undefined);
    assert.equal(options.headers.Authorization, undefined);
  }
});

test('monthly statements are paginated same-origin reads with a scoped CSV path', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => json({ status: 'estimate' }));
  const controller = new AbortController();
  assert.deepEqual(await customerApi.billing('2026-11', 2, controller.signal), { status: 'estimate' });
  const [[path, options]] = fetch.mock.calls.map(({ arguments: args }) => args);
  assert.equal(path, '/api/customer/billing?month=2026-11&page=2&pageSize=50');
  assert.equal(options.credentials, 'same-origin');
  assert.equal(options.cache, 'no-store');
  assert.equal(options.signal, controller.signal);
  assert.equal(options.headers.Authorization, undefined);
  const id = `stmt_${'a'.repeat(32)}`;
  assert.equal(customerApi.billingExportUrl(id), `/api/customer/billing/${id}/export.csv`);
  assert.equal(customerApi.billingExportUrl('../../me'), null);
  assert.equal(customerApi.billingExportUrl(null), null);
});

test('API Key changes and logout send CSRF and never use a bearer secret', async (t) => {
  const credential = 'fixture-only';
  const created = { key: { id: 'key_any' }, secret: 'fixture-secret' };
  const fetch = t.mock.method(globalThis, 'fetch', async (path, options) =>
    path.endsWith('/keys') && options.method === 'POST'
      ? json(created, 201)
      : new Response(null, { status: 204 }),
  );
  assert.deepEqual(await customerApi.createKey('集成脚本', credential), created);
  assert.equal(await customerApi.revokeKey('id/escape', credential), null);
  assert.equal(await customerApi.logout(credential), null);

  const requests = fetch.mock.calls.map(({ arguments: args }) => args);
  assert.deepEqual(
    requests.map(([path, options]) => [path, options.method]),
    [
      ['/api/customer/keys', 'POST'],
      ['/api/customer/keys/id%2Fescape', 'DELETE'],
      ['/api/customer/logout', 'POST'],
    ],
  );
  assert.deepEqual(JSON.parse(requests[0][1].body), { name: '集成脚本' });
  for (const [, options] of requests) {
    assert.equal(options.headers['X-CSRF-Token'], credential);
    assert.equal(options.headers.Authorization, undefined);
  }
});

test('expired sessions preserve status and do not replay mutations', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async () => json({ detail: 'fixture' }, 401));
  await assert.rejects(
    customerApi.createKey('test', 'fixture-only'),
    (error) => error instanceof ApiError && error.status === 401,
  );
  assert.equal(fetch.mock.callCount(), 1);
});

test('task records use only the customer session and key verification stays on the same origin', async (t) => {
  const fetch = t.mock.method(globalThis, 'fetch', async (path) =>
    path.endsWith('/playground/key') ? new Response(null, { status: 204 }) : json({ items: [] }),
  );
  const controller = new AbortController();
  const taskId = `vid_${'c'.repeat(32)}`;
  await customerApi.tasks({ taskId, fromDate: '2026-11-01', status: 'queued', page: 2 }, controller.signal);
  await customerApi.verifyPlaygroundKey('fixture-only', controller.signal);

  const [[list, listOptions], [verify, verifyOptions]] = fetch.mock.calls.map(({ arguments: args }) => args);
  assert.equal(list, `/api/customer/tasks?page=2&pageSize=20&taskId=${taskId}&fromDate=2026-11-01&status=queued`);
  assert.equal(listOptions.credentials, 'same-origin');
  assert.equal(listOptions.cache, 'no-store');
  assert.equal(listOptions.headers.Authorization, undefined);
  assert.equal(listOptions.signal, controller.signal);
  assert.equal(verify, '/api/customer/playground/key');
  assert.equal(verifyOptions.headers.Authorization, 'Bearer fixture-only');
  assert.equal(verifyOptions.credentials, 'same-origin');
  assert.equal(verifyOptions.redirect, 'error');
  assert.equal(verifyOptions.signal, controller.signal);
});
