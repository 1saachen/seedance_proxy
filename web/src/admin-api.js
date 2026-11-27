const adminPath = '/api/admin';
const projectPath = (id) => '/projects/' + encodeURIComponent(id);

export class AdminApiError extends Error {
  constructor(status, code) {
    super('Admin API: HTTP ' + status);
    this.status = status;
    this.code = code;
  }
}

async function request(path, { method = 'GET', body, csrfToken, signal } = {}) {
  const headers = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
  const response = await fetch(adminPath + path, {
    method,
    headers,
    credentials: 'same-origin',
    cache: 'no-store',
    redirect: 'error',
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const problem = await response.json().catch(() => null);
    throw new AdminApiError(response.status, problem?.detail?.code);
  }
  return response.status === 204 ? null : response.json();
}

function query(path, params, signal) {
  return request(path + '?' + new URLSearchParams(params), { signal });
}

export const adminApi = {
  me: (signal) => request('/me', { signal }),
  login: (body) => request('/login', { method: 'POST', body }),
  logout: (csrfToken) => request('/logout', { method: 'POST', csrfToken }),
  projects: (q, page, signal) => query('/projects', { q, page, pageSize: 15 }, signal),
  createProject: (body, csrfToken) => request('/projects', { method: 'POST', body, csrfToken }),
  keys: (id, signal) => request(projectPath(id) + '/keys', { signal }),
  usage: (id, month, page, signal) => query(projectPath(id) + '/usage', { month, page, pageSize: 25 }, signal),
  statement: (id, month, page, signal) =>
    query(projectPath(id) + '/statements/' + encodeURIComponent(month), { page, pageSize: 50 }, signal),
  confirmStatement: (id, month, expectedPayableAmount, csrfToken) =>
    request(projectPath(id) + '/statements/' + encodeURIComponent(month) + '/confirm', {
      method: 'POST',
      body: { expectedPayableAmount },
      csrfToken,
    }),
  terms: (id, signal) => request(projectPath(id) + '/billing-terms', { signal }),
  createTerms: (id, body, csrfToken) => request(projectPath(id) + '/billing-terms', { method: 'POST', body, csrfToken }),
  prices: (page, signal) => query('/prices', { page, pageSize: 25 }, signal),
  createPrice: (body, csrfToken) => request('/prices', { method: 'POST', body, csrfToken }),
  audit: (page, signal) => query('/audit', { page, pageSize: 25 }, signal),
};

export function adminErrorMessage(error) {
  switch (error.code) {
    case 'cannot_confirm_open_month':
      return '账期尚未结束。请选择之前的月份。';
    case 'future_month':
      return '该账期尚未开始。';
    case 'statement_amount_changed':
      return '应付金额已经变化，请刷新账单并重新核对。';
    default:
      break;
  }
  switch (error.status) {
    case 400:
    case 422:
      return '请检查填写的内容后重试。';
    case 403:
      return '当前会话无法执行此操作，请重新加载页面后重试。';
    case 404:
      return '找不到这条记录，请刷新列表。';
    case 409:
      return '此记录与现有数据冲突，请刷新并核对后重试。';
    case 429:
      return '尝试次数过多，请稍后重试。';
    default:
      return '请求暂时无法完成，请稍后重试。';
  }
}
