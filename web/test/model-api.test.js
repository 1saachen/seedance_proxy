import assert from 'node:assert/strict';
import test from 'node:test';
import { customerApi } from '../src/api.js';
import { httpsImageUrl, modelApi } from '../src/model-api.js';

test('public model endpoints use business key auth without sending cookies', async (context) => {
  const expected = [
    ['/v1/models', 'GET', undefined],
    ['/v1/chat/completions', 'POST', { model: 'doubao-seed-2.1-lite', messages: [{ role: 'user', content: 'Hello' }] }],
    ['/v1/images/generations', 'POST', { model: 'doubao-seedream-5.5', prompt: 'Coastal town' }],
  ];
  const signal = new AbortController().signal;
  const fetch = context.mock.method(globalThis, 'fetch', async (path, options) => {
    const [wantedPath, method, body] = expected.shift();
    assert.equal(path, wantedPath);
    assert.equal(options.method, method);
    assert.equal(options.headers.Authorization, 'Bearer fixture-key');
    assert.equal(options.credentials, 'omit');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.redirect, 'error');
    assert.equal(options.signal, signal);
    assert.equal(options.body, body && JSON.stringify(body));
    assert.equal(options.headers['Idempotency-Key'], path === '/v1/images/generations' ? 'request-id' : undefined);
    return Response.json({ data: [] });
  });

  await modelApi.list('fixture-key', signal);
  await modelApi.chat('fixture-key', expected[0][2], signal);
  await modelApi.image('fixture-key', expected[0][2], 'request-id', signal);
  assert.equal(fetch.mock.callCount(), 3);
});

test('model usage uses only the customer cookie and supplied month', async (context) => {
  context.mock.method(globalThis, 'fetch', async (path, options) => {
    assert.equal(path, '/api/customer/model-usage?month=2026-11');
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers.Authorization, undefined);
    return Response.json({ items: [] });
  });

  assert.deepEqual(await customerApi.modelUsage('2026-11'), { items: [] });
});

test('shared concurrency errors preserve the server code without retrying generation', async (context) => {
  const fetch = context.mock.method(globalThis, 'fetch', async () => Response.json({
    detail: { code: 'key_concurrency_exceeded' },
  }, { status: 429 }));

  await assert.rejects(modelApi.image('fixture-key', { prompt: 'Coastal town' }, 'request-id'), {
    status: 429, code: 'key_concurrency_exceeded',
  });
  assert.equal(fetch.mock.callCount(), 1);
});

test('image rendering rejects insecure and credential-bearing URLs', () => {
  assert.equal(httpsImageUrl('https://example.test/image.png'), 'https://example.test/image.png');
  for (const unsafe of ['javascript:alert(1)', 'http://example.test/image.png', 'https://user:pwd@example.test/image.png', '/image.png', null]) {
    assert.equal(httpsImageUrl(unsafe), null);
  }
});
