import { useCallback, useEffect, useRef, useState } from 'react';
import { customerApi, requestErrorMessage } from './api.js';
import { Wordmark } from './wordmark.jsx';

const sections = [
  { id: 'overview', label: '概览', mark: '◫', description: '查看当前项目的接入状态和本月用量。' },
  { id: 'api', label: 'API 接入', mark: '⌘', description: '管理业务 API Key，接入 Seedance 视频生成。' },
  { id: 'billing', label: '用量与计价', mark: '≋', description: '按月查看当前项目的任务用量和单次金额。' },
];

const statusNames = {
  active: '有效',
  disabled: '已撤销',
  revoked: '已撤销',
  queued: '排队中',
  running: '运行中',
  pending: '待处理',
  submitting: '提交中',
  succeeded: '成功',
  completed: '成功',
  failed: '失败',
  canceled: '已取消',
  cancelled: '已取消',
  expired: '已过期',
  unknown: '状态待确认',
};

const dateFormatter = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  dateStyle: 'medium',
  timeStyle: 'short',
});
const numberFormatter = new Intl.NumberFormat('zh-CN');

function formatDate(value) {
  if (value == null) return '—';
  const date = new Date(typeof value === 'number' && value < 10 ** 11 ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? '—' : dateFormatter.format(date);
}

function formatNumber(value) {
  return value == null ? '—' : numberFormatter.format(value);
}

function formatBillingAmount(billing) {
  if (billing?.amount != null) return billing.amount;
  return billing?.status === 'pending' ? '待计价' : '未计费';
}

function currentMonth() {
  const parts = new Intl.DateTimeFormat('en', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
  }).formatToParts();
  const year = parts.find((part) => part.type === 'year').value;
  const month = parts.find((part) => part.type === 'month').value;
  return `${year}-${month}`;
}

function readSection() {
  const id = window.location.hash.slice(1);
  return sections.find((section) => section.id === id) ?? sections[0];
}

function useRemote(load, onExpired) {
  const [snapshot, setSnapshot] = useState(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setSnapshot({ load, revision, data });
      },
      (error) => {
        if (controller.signal.aborted) return;
        if (error.status === 401) onExpired(true);
        else setSnapshot({ load, revision, error });
      },
    );
    return () => controller.abort();
  }, [load, onExpired, revision]);
  const result = snapshot?.load === load && snapshot.revision === revision ? snapshot : {};
  return { ...result, refresh: () => setRevision((value) => value + 1) };
}

function LoadState({ state, children }) {
  if (state.error)
    return (
      <div className="empty">
        <p role="alert">{requestErrorMessage(state.error)}</p>
        <button className="button secondary" onClick={state.refresh}>
          重试
        </button>
      </div>
    );
  if (!state.data)
    return (
      <p className="empty" role="status">
        正在加载…
      </p>
    );
  return children(state.data);
}

function Status({ value }) {
  const tone = ['active', 'succeeded', 'completed'].includes(value)
    ? 'positive'
    : value === 'failed'
      ? 'negative'
      : 'neutral';
  return (
    <span className={`status ${tone}`}>
      {Object.hasOwn(statusNames, value) ? statusNames[value] : value || '未知'}
    </span>
  );
}

function UsageSummary({ summary }) {
  return (
    <dl className="metric-strip">
      <div>
        <dt>调用次数</dt>
        <dd>{formatNumber(summary.calls)}</dd>
      </div>
      <div>
        <dt>成功任务</dt>
        <dd>{formatNumber(summary.successful)}</dd>
      </div>
      <div>
        <dt>已记录输出 Token</dt>
        <dd>{formatNumber(summary.outputTokens)}</dd>
      </div>
    </dl>
  );
}

function Overview({ customer, usage, month }) {
  return (
    <>
      <section className="workspace-banner" aria-labelledby="overview-heading">
        <div>
          <p className="workspace-caption">客户项目</p>
          <h2 id="overview-heading">Seedance 视频生成</h2>
          <p>创建业务 Key，查看当前可用模型，然后提交第一个视频任务。</p>
        </div>
        <a className="button light" href="#api">
          开始 API 接入
        </a>
      </section>
      <section className="panel" aria-labelledby="month-heading">
        <div className="panel-head">
          <div>
            <h2 id="month-heading">本月用量</h2>
            <p>{month} · 中国标准时间</p>
          </div>
          <a href="#billing">查看用量明细</a>
        </div>
        <LoadState state={usage}>{({ summary }) => <UsageSummary summary={summary} />}</LoadState>
      </section>
      <section className="panel" aria-labelledby="scope-heading">
        <div className="panel-head">
          <div>
            <h2 id="scope-heading">账号与项目</h2>
          </div>
        </div>
        <dl className="project-details">
          <div>
            <dt>账号</dt>
            <dd>{customer.email}</dd>
          </div>
          <div>
            <dt>项目 ID</dt>
            <dd>
              <code>{customer.projectId}</code>
            </dd>
          </div>
        </dl>
      </section>
    </>
  );
}

function KeyReveal({ secret, onClose }) {
  const [message, setMessage] = useState('');
  const title = useRef(null);
  useEffect(() => {
    title.current.focus();
  }, []);
  async function copy() {
    try {
      await navigator.clipboard.writeText(secret);
      setMessage('已复制 API Key。');
    } catch {
      setMessage('剪贴板不可用，请选中并手动复制。');
    }
  }
  return (
    <div className="secret-panel">
      <h3 tabIndex={-1} ref={title}>
        复制新建的 API Key
      </h3>
      <p>请将它保存在服务器的密钥管理工具中。关闭后只能看到前缀。</p>
      <code className="secret-value">{secret}</code>
      <div className="row-actions">
        <button className="button primary" onClick={copy}>
          复制 API Key
        </button>
        <button className="button secondary" onClick={onClose}>
          关闭
        </button>
      </div>
      {message && <p role="status">{message}</p>}
    </div>
  );
}

function RevokeDialog({ target, busy, error, onClose, onConfirm }) {
  const dialog = useRef(null);
  useEffect(() => {
    dialog.current.showModal();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="revoke-dialog"
      aria-labelledby="revoke-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <h3 id="revoke-title">撤销 API Key</h3>
      <p>撤销「{target.name}」后，使用这枚 Key 的应用将无法继续调用接口。</p>
      {error && (
        <p className="message error" role="alert">
          {error}
        </p>
      )}
      <div className="row-actions">
        <button className="button secondary" onClick={onClose} disabled={busy}>
          取消
        </button>
        <button className="button danger" onClick={onConfirm} disabled={busy}>
          {busy ? '正在撤销…' : '撤销 Key'}
        </button>
      </div>
    </dialog>
  );
}

function KeyPanel({ state, csrfToken, onExpired }) {
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [secret, setSecret] = useState('');
  const [target, setTarget] = useState(null);
  const [notice, setNotice] = useState('');

  async function create(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const name = new FormData(form).get('name').trim();
    if (!name) {
      setError('请输入 API Key 名称。');
      return;
    }
    setError('');
    setNotice('');
    setBusy(true);
    try {
      const result = await customerApi.createKey(name, csrfToken);
      form.reset();
      setSecret(result.secret);
      state.refresh();
    } catch (problem) {
      if (problem.status === 401) onExpired(true);
      else setError(requestErrorMessage(problem));
    } finally {
      setBusy(false);
    }
  }

  async function revoke() {
    setError('');
    setNotice('');
    setBusy(true);
    try {
      await customerApi.revokeKey(target.id, csrfToken);
      setTarget(null);
      setNotice('API Key 已撤销。');
      state.refresh();
    } catch (problem) {
      if (problem.status === 401) onExpired(true);
      else setError(requestErrorMessage(problem));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel" aria-labelledby="keys-heading">
      <div className="panel-head">
        <div>
          <h2 id="keys-heading">API Key</h2>
          <p>用业务 Key 发起模型请求，每枚 Key 归属当前项目。</p>
        </div>
      </div>
      <form className="key-form" onSubmit={create}>
        <label htmlFor="key-name">Key 名称</label>
        <div>
          <input
            id="key-name"
            name="name"
            placeholder="例如：视频生成服务"
            maxLength={100}
            required
            disabled={busy || Boolean(secret)}
          />
          <button className="button primary" disabled={busy || Boolean(secret)}>
            {busy && !target ? '正在创建…' : '创建 Key'}
          </button>
        </div>
      </form>
      {error && !target && (
        <p className="message error panel-message" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="notice panel-message" role="status">
          {notice}
        </p>
      )}
      {secret && <KeyReveal secret={secret} onClose={() => setSecret('')} />}
      <LoadState state={state}>
        {({ keys }) =>
          keys.length === 0 ? (
            <p className="empty">暂无 API Key。创建一枚 Key 后即可开始接入。</p>
          ) : (
            <div className="table-scroll" tabIndex={0} role="region" aria-label="API Key 列表">
              <table>
                <thead>
                  <tr>
                    <th scope="col">名称</th>
                    <th scope="col">前缀</th>
                    <th scope="col">状态</th>
                    <th scope="col">创建时间</th>
                    <th scope="col">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {keys.map((key) => (
                    <tr key={key.id}>
                      <th scope="row">{key.name}</th>
                      <td>
                        <code>{key.prefix}</code>
                      </td>
                      <td>
                        <Status value={key.status} />
                      </td>
                      <td>{formatDate(key.createdAt)}</td>
                      <td>
                        {key.status === 'active' && (
                          <button
                            className="text-danger"
                            disabled={busy}
                            aria-label={`撤销 ${key.name}`}
                            onClick={() => {
                              setError('');
                              setTarget(key);
                            }}
                          >
                            撤销
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </LoadState>
      {target && (
        <RevokeDialog
          target={target}
          error={error}
          busy={busy}
          onClose={() => {
            setError('');
            setTarget(null);
          }}
          onConfirm={revoke}
        />
      )}
    </section>
  );
}

function SetupGuide() {
  const origin = window.location.origin;
  const example = [
    `BASE_URL='${origin}'`,
    'curl "$BASE_URL/v1/models" -H "Authorization: Bearer $API_KEY"',
    '',
    'curl "$BASE_URL/api/v3/contents/generations/tasks" \\',
    '  -H "Authorization: Bearer $API_KEY" \\',
    "  -H 'Content-Type: application/json' \\",
    '  -H "Idempotency-Key: $REQUEST_ID" \\',
    `  -d '{"model":"doubao-seedance-2.0","content":[{"type":"text","text":"雨后的海滨街道"}],"duration":5}'`,
    '',
    'curl "$BASE_URL/api/v3/contents/generations/tasks/$TASK_ID" \\',
    '  -H "Authorization: Bearer $API_KEY"',
  ].join('\n');
  const uploadExample = [
    'curl "$BASE_URL/api/asset-group/create" \\',
    '  -H "Authorization: Bearer $API_KEY" \\',
    "  -H 'Content-Type: application/json' -d '{\"name\":\"reference-demo\"}'",
    '',
    'curl "$BASE_URL/api/asset/upload-file" \\',
    '  -H "Authorization: Bearer $API_KEY" \\',
    '  -H "Idempotency-Key: $UPLOAD_REQUEST_ID" \\',
    '  -F "file=@./reference.png;type=image/png" -F "assetType=Image"',
    '',
    'curl "$BASE_URL/api/asset/create" \\',
    '  -H "Authorization: Bearer $API_KEY" \\',
    "  -H 'Content-Type: application/json' \\",
    '  -d "{\\"groupId\\":\\"$GROUP_ID\\",\\"uploadId\\":\\"$UPLOAD_ID\\",\\"name\\":\\"reference\\",\\"assetType\\":\\"Image\\"}"',
    '',
    'curl "$BASE_URL/api/asset/get?assetId=$ASSET_ID" \\',
    '  -H "Authorization: Bearer $API_KEY"',
  ].join('\n');
  return (
    <section className="panel" aria-labelledby="integration-heading">
      <div className="panel-head">
        <div>
          <h2 id="integration-heading">Seedance 快速接入</h2>
          <p>
            用本站业务 Key 设置 <code>API_KEY</code>，并为每次任务设置独立的 <code>REQUEST_ID</code>。
          </p>
        </div>
        <a href="/#quickstart">查看接入指南</a>
      </div>
      <pre className="console-code" tabIndex={0}>
        <code>{example}</code>
      </pre>
      <p className="guide-note">
        用创建响应里的任务 ID 设置 <code>TASK_ID</code>。实际可用模型以模型列表为准。
      </p>
      <section aria-labelledby="asset-guide-heading">
        <h3 id="asset-guide-heading">用本地图片作为参考</h3>
        <p className="guide-note">
          用同一枚业务 Key 逐条运行命令。将创建素材组返回的 <code>group.id</code> 设为 <code>GROUP_ID</code>，
          并为新文件设置独立的 <code>UPLOAD_REQUEST_ID</code>。
        </p>
        <p className="guide-note">
          用上传返回的 <code>uploadId</code> 设置 <code>UPLOAD_ID</code>，再用登记返回的 <code>asset.id</code>{' '}
          设置 <code>ASSET_ID</code>。
        </p>
        <pre className="console-code" tabIndex={0}>
          <code>{uploadExample}</code>
        </pre>
        <p className="guide-note">
          状态为 <code>Processing</code> 时稍后重查。变为 <code>Active</code> 后，将返回的 <code>asset.reference</code>{' '}
          填入下面的 <code>url</code>，把该对象加到生成请求的 <code>content</code> 中。
        </p>
        <pre className="console-code" tabIndex={0}>
          <code>{'{"type":"image_url","image_url":{"url":"asset://<素材 ID>"},"role":"reference_image"}'}</code>
        </pre>
        <p className="guide-note">
          如果素材已有公网 HTTPS 链接，可在登记请求中填写 <code>url</code> 并省略 <code>uploadId</code>。
        </p>
      </section>
    </section>
  );
}

function Billing({ month, setMonth, maxMonth, usage }) {
  return (
    <>
      <section className="panel" aria-labelledby="usage-heading">
        <div className="panel-head">
          <div>
            <h2 id="usage-heading">用量明细</h2>
            <p>
              按北京时间的任务创建月份归集，最多显示最近 100
              条任务。缺少供应商用量的记录显示为「—」。视频调用受项目每日上限约束。
            </p>
          </div>
          <label className="month-filter">
            月份
            <input
              type="month"
              value={month}
              min="2000-01"
              max={maxMonth}
              onChange={(event) => {
                if (event.target.validity.valid && event.target.value) setMonth(event.target.value);
              }}
            />
          </label>
        </div>
        <LoadState state={usage}>
          {({ summary, items }) => (
            <>
              <UsageSummary summary={summary} />
              {items.length === 0 ? (
                <p className="empty">这个月还没有调用记录。</p>
              ) : (
                <div className="table-scroll" tabIndex={0} role="region" aria-label="用量明细表">
                  <table>
                    <thead>
                      <tr>
                        <th scope="col">任务 ID</th>
                        <th scope="col">模型</th>
                        <th scope="col">状态</th>
                        <th scope="col">输出 Token</th>
                        <th scope="col">单次金额（元）</th>
                        <th scope="col">创建时间</th>
                        <th scope="col">任务结束时间</th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((item) => (
                        <tr key={item.id}>
                          <th scope="row">
                            <code>{item.id}</code>
                          </th>
                          <td>{item.model}</td>
                          <td>
                            <Status value={item.status} />
                          </td>
                          <td>{formatNumber(item.outputTokens)}</td>
                          <td>{formatBillingAmount(item.billing)}</td>
                          <td>{formatDate(item.createdAt)}</td>
                          <td>{formatDate(item.settledAt)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </>
          )}
        </LoadState>
      </section>
      <section className="panel" aria-labelledby="billing-heading">
        <div className="panel-head">
          <div>
            <h2 id="billing-heading">账单进度</h2>
            <p>月度账单、在线支付和发票将在后续开放。</p>
          </div>
        </div>
      </section>
    </>
  );
}

export function Workspace({ session, onSessionEnded }) {
  const [section, setSection] = useState(readSection);
  const [month, setMonth] = useState(currentMonth);
  const [loggingOut, setLoggingOut] = useState(false);
  const [error, setError] = useState('');
  const maxMonth = currentMonth();
  const displayMonth = section.id === 'billing' ? month : maxMonth;
  const loadUsage = useCallback((signal) => customerApi.usage(displayMonth, signal), [displayMonth]);
  const usage = useRemote(loadUsage, onSessionEnded);
  const keys = useRemote(customerApi.keys, onSessionEnded);

  useEffect(() => {
    const onHashChange = () => setSection(readSection());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  async function logout() {
    setError('');
    setLoggingOut(true);
    try {
      await customerApi.logout(session.csrfToken);
      onSessionEnded(false);
    } catch (problem) {
      if (problem.status === 401) onSessionEnded(true);
      else setError(requestErrorMessage(problem));
    } finally {
      setLoggingOut(false);
    }
  }

  return (
    <div className="workspace">
      <a
        className="skip-link"
        href="#workspace-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById('workspace-content').focus();
        }}
      >
        跳到正文
      </a>
      <header className="workspace-header">
        <Wordmark />
        <div className="account-menu">
          <span>{session.customer.email}</span>
          <button className="button secondary" onClick={logout} disabled={loggingOut}>
            {loggingOut ? '正在退出…' : '退出登录'}
          </button>
        </div>
      </header>
      <aside className="workspace-sidebar">
        <nav aria-label="客户工作台">
          {sections.map((item) => (
            <a key={item.id} href={`#${item.id}`} aria-current={section.id === item.id ? 'page' : undefined}>
              <span className="nav-mark" aria-hidden="true">
                {item.mark}
              </span>
              {item.label}
            </a>
          ))}
        </nav>
        <a className="sidebar-docs" href="/#quickstart">
          开发者文档
        </a>
      </aside>
      <main id="workspace-content" className="workspace-main" tabIndex={-1}>
        <div className="page-heading">
          <h1>{section.label}</h1>
          <p>{section.description}</p>
        </div>
        {error && (
          <p className="message error" role="alert">
            {error}
          </p>
        )}
        {section.id === 'overview' && <Overview usage={usage} month={maxMonth} customer={session.customer} />}
        {section.id === 'api' && (
          <>
            <KeyPanel state={keys} csrfToken={session.csrfToken} onExpired={onSessionEnded} />
            <SetupGuide />
          </>
        )}
        {section.id === 'billing' && (
          <Billing month={month} setMonth={setMonth} maxMonth={maxMonth} usage={usage} />
        )}
      </main>
    </div>
  );
}
