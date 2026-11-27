const customerPath = '/api/customer';

export class ApiError extends Error {
  constructor(status) {
    super(`Customer API: HTTP ${status}`);
    this.status = status;
  }
}

async function request(path, { method = 'GET', body, csrfToken, bearerKey, signal } = {}) {
  const headers = { Accept: 'application/json' };
  if (body) headers['Content-Type'] = 'application/json';
  if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
  if (bearerKey) headers.Authorization = `Bearer ${bearerKey}`;
  const response = await fetch(`${customerPath}${path}`, {
    method,
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
    ...(bearerKey ? { redirect: 'error' } : {}),
    body: body ? JSON.stringify(body) : undefined,
    signal,
  });
  if (!response.ok) throw new ApiError(response.status);
  if (response.status === 204) return null;
  return response.json();
}

export const customerApi = {
  me: (signal) => request('/me', { signal }),
  login: (body) => request('/login', { method: 'POST', body }),
  register: (body) => request('/register', { method: 'POST', body }),
  logout: (csrfToken) => request('/logout', { method: 'POST', csrfToken }),
  keys: (signal) => request('/keys', { signal }),
  createKey: (name, csrfToken, signal) => request('/keys', { method: 'POST', body: { name }, csrfToken, signal }),
  revokeKey: (id, csrfToken) => request(`/keys/${encodeURIComponent(id)}`, { method: 'DELETE', csrfToken }),
  verifyPlaygroundKey: (key, signal) => request('/playground/key', { bearerKey: key, signal }),
  tasks: ({ taskId, fromDate, toDate, status, page = 1, pageSize = 20 } = {}, signal) => {
    const query = new URLSearchParams({ page, pageSize });
    for (const [name, value] of Object.entries({ taskId, fromDate, toDate, status })) {
      if (value) query.set(name, value);
    }
    return request(`/tasks?${query}`, { signal });
  },
  usage: (month, signal, { day, page, pageSize, includeTrend } = {}) => {
    const query = new URLSearchParams({ month });
    for (const [name, value] of Object.entries({ day, page, pageSize, includeTrend })) {
      if (value != null) query.set(name, value);
    }
    return request(`/usage?${query}`, { signal });
  },
  billing: (month, page, signal) => request(`/billing?${new URLSearchParams({ month, page, pageSize: 50 })}`, { signal }),
  billingExportUrl: (id) =>
    typeof id === 'string' && /^stmt_[0-9a-f]{32}$/.test(id) ? `${customerPath}/billing/${id}/export.csv` : null,
};

export function requestErrorMessage(error) {
  switch (error.status) {
    case 400:
    case 422:
      return '请求参数有误，请检查后重试。';
    case 403:
      return '当前会话无法执行此操作。请刷新页面后重试。';
    case 409:
      return '当前记录发生冲突，请刷新确认状态后重试。';
    case 429:
      return '尝试次数过多，请稍后重试。';
    default:
      return '暂时无法连接服务，请稍后重试。';
  }
}
