export class ModelApiError extends Error {
  constructor(status, code = null) {
    super(`Model API: HTTP ${status}`);
    this.status = status;
    this.code = code;
  }
}

async function request(path, key, { body, idempotencyKey, signal } = {}) {
  const headers = { Accept: 'application/json', Authorization: `Bearer ${key}` };
  if (body) headers['Content-Type'] = 'application/json';
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
  const response = await fetch('/v1' + path, {
    method: body ? 'POST' : 'GET',
    headers,
    credentials: 'omit',
    cache: 'no-store',
    redirect: 'error',
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });
  if (!response.ok) {
    const payload = response.status === 429 ? await response.json().catch(() => null) : null;
    const code = payload?.detail?.code;
    throw new ModelApiError(response.status, typeof code === 'string' ? code : null);
  }
  return response.json();
}

export const modelApi = {
  list: (key, signal) => request('/models', key, { signal }),
  chat: (key, body, signal) => request('/chat/completions', key, { body, signal }),
  image: (key, body, idempotencyKey, signal) => request('/images/generations', key, { body, idempotencyKey, signal }),
};

export function httpsImageUrl(value) {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
  } catch {
    return null;
  }
}
