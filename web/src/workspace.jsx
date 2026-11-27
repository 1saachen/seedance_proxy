import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { customerApi, requestErrorMessage } from './api.js';
import { ModelPlayground } from './model-playground.jsx';
import { SpendTrend } from './spend-trend.jsx';
import { formatDate, formatNumber, Status } from './workbench-format.jsx';
import { Wordmark } from './wordmark.jsx';

const Playground = lazy(() => import('./playground.jsx').then(({ Playground }) => ({ default: Playground })));
const TaskRecords = lazy(() => import('./task-records.jsx').then(({ TaskRecords }) => ({ default: TaskRecords })));

const navigation = [
  {
    label: '使用',
    items: [
      { id: 'overview', label: '概览', mark: '◫', description: '查看当前项目的接入状态和本月用量。' },
      { id: 'api', label: 'API 接入', mark: '⌘', description: '管理业务 API Key，接入火山方舟模型。' },
      { id: 'models', label: '模型体验', mark: '◇', description: '使用项目 API Key 体验文本回复和图片生成。' },
      { id: 'playground', label: '视频体验', mark: '▷', description: '提交视频生成请求并查看结果。', seedanceOnly: true },
    ],
  },
  {
    label: '记录',
    items: [
      { id: 'tasks', label: '视频任务', mark: '▤', description: '按任务 ID、提交时间和状态查询本项目的视频记录。', seedanceOnly: true },
      { id: 'billing', label: '用量与账单', mark: '≋' },
    ],
  },
];
const sections = navigation.flatMap(({ items }) => items);

function currentDay() {
  const parts = new Intl.DateTimeFormat('en', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts();
  return ['year', 'month', 'day'].map((name) => parts.find((part) => part.type === name).value).join('-');
}

function currentMonth() {
  return currentDay().slice(0, 7);
}

function readRoute() {
  const [id, query] = window.location.hash.slice(1).split('?');
  const section = sections.find((item) => item.id === id) ?? sections[0];
  const params = new URLSearchParams(query);
  const day = section.id === 'billing' ? params.get('day') : null;
  if (/^2\d{3}-\d{2}-\d{2}$/.test(day ?? '')) {
    const date = new Date(`${day}T00:00:00Z`);
    if (!Number.isNaN(date.getTime()) && date.toISOString().slice(0, 10) === day) return { section, day, showUsage: true };
  }
  return { section, day: null, showUsage: section.id === 'billing' && params.get('usage') === '1' };
}

function useRemote(load, onExpired, enabled = true) {
  const [snapshot, setSnapshot] = useState(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!enabled) return;
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
  }, [load, onExpired, revision, enabled]);
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

function Overview({ customer, keys, month, onExpired }) {
  const activeKeys = keys.data?.keys.filter((key) => key.status === 'active').length;
  const load = useCallback((signal) => customerApi.usage(month, signal, { includeTrend: true, pageSize: 1 }), [month]);
  const usage = useRemote(load, onExpired);
  return (
    <>
      <section className="panel overview-usage" aria-labelledby="month-heading">
        <div className="panel-head">
          <div>
            <h2 id="month-heading">本月用量</h2>
            <p><time dateTime={month}>{month}</time>（北京时间）</p>
          </div>
          <a href="#billing?usage=1">查看调用记录</a>
        </div>
        <LoadState state={usage}>{({ summary }) => <UsageSummary summary={summary} />}</LoadState>
      </section>
      <section className="panel spend-panel" aria-labelledby="spend-heading">
        <div className="panel-head">
          <div>
            <h2 id="spend-heading">近 7 日折后费用</h2>
            <p>按北京时间的任务创建日期归集，包含当日。</p>
          </div>
        </div>
        <LoadState state={usage}>{({ spendTrend }) => <SpendTrend trend={spendTrend} />}</LoadState>
      </section>
      <div className="overview-layout">
        <section className="panel" aria-labelledby="overview-heading">
          <div className="panel-head">
            <div>
              <h2 id="overview-heading">开始接入模型</h2>
              <p>创建密钥后，在工作台体验或从应用发起 API 请求。</p>
            </div>
          </div>
          <ol className="onboarding-steps" role="list">
            <li>
              <div><h3>创建项目 API Key</h3><p>创建业务 Key，供本地工具、脚本或应用调用。</p></div>
              <a className="button primary" href="#api">管理密钥</a>
            </li>
            <li>
              <div><h3>体验文本和图片</h3><p>选择模型，输入提示词并生成文本或图片。</p></div>
              <a className="button secondary" href="#models">进入模型体验</a>
            </li>
            <li>
              <div><h3>查询调用用量</h3><p>按日期、模型或业务 Key 查询项目用量。</p></div>
              <a className="button secondary" href="#billing?usage=1">查看调用记录</a>
            </li>
          </ol>
        </section>
        <section className="panel project-panel" aria-labelledby="scope-heading">
          <div className="panel-head">
            <h2 id="scope-heading">项目详情</h2>
          </div>
          <dl className="project-details">
            <div><dt>账号</dt><dd>{customer.email}</dd></div>
            <div><dt>项目 ID</dt><dd><code>{customer.projectId}</code></dd></div>
            <div><dt>可用密钥</dt><dd>{activeKeys == null ? '—' : formatNumber(activeKeys)}</dd></div>
            <div><dt>API 地址</dt><dd><code>{window.location.origin}</code></dd></div>
          </dl>
        </section>
      </div>
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
      <p>请妥善保存这枚 Key。关闭后只能看到前缀。</p>
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
    <section className="panel key-panel" aria-labelledby="keys-heading">
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
            placeholder="例如：内容生成服务"
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
            <div className="table-scroll key-table" tabIndex={0} role="region" aria-label="API Key 列表">
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
  const example = [
    "BASE_URL='" + window.location.origin + "'",
    'curl "$BASE_URL/v1/models" -H "Authorization: Bearer $API_KEY"',
    '',
    'curl "$BASE_URL/v1/chat/completions" \\',
    '  -H "Authorization: Bearer $API_KEY" \\',
    "  -H 'Content-Type: application/json' \\",
    "  -d '{\"model\":\"doubao-seed-2.1-lite\",\"messages\":[{\"role\":\"user\",\"content\":\"写一句海滨旅行推荐\"}]}'",
    '',
    'curl "$BASE_URL/v1/images/generations" \\',
    '  -H "Authorization: Bearer $API_KEY" \\',
    "  -H 'Content-Type: application/json' \\",
    '  -H "Idempotency-Key: $REQUEST_ID" \\',
    "  -d '{\"model\":\"doubao-seedream-5.5\",\"prompt\":\"雨后海滨小镇的红色电车\"}'",
  ].join('\n');
  return (
    <section className="panel" aria-labelledby="integration-heading">
      <div className="panel-head">
        <div>
          <h2 id="integration-heading">模型 API 快速接入</h2>
          <p>用 API 地址和项目业务 Key，从终端、API 客户端或应用发起请求。</p>
        </div>
        <a href="/docs/#quickstart">开发者文档</a>
      </div>
      <p className="guide-note">
        发送请求时使用 <code>Authorization: Bearer &lt;API_KEY&gt;</code>。运行下列 Bash 命令前，
        先在终端设置 <code>API_KEY</code> 和 <code>REQUEST_ID</code>。
      </p>
      <pre className="console-code" tabIndex={0}><code>{example}</code></pre>
      <p className="guide-note">
        每次新建图片请求时设置不同的 <code>REQUEST_ID</code>，重试沿用原值和原请求体。
      </p>
    </section>
  );
}

function MonthlyStatement({ month, active, onExpired }) {
  const [page, setPage] = useState(1);
  const [open, setOpen] = useState(false);
  const [previous, setPrevious] = useState(null);
  const linesRef = useRef(null);
  const load = useCallback((signal) => customerApi.billing(month, page, signal), [month, page]);
  const statement = useRemote(load, onExpired, active);
  const bill = statement.data ?? previous;

  useEffect(() => {
    if (statement.data) setPrevious(statement.data);
  }, [statement.data]);

  function goToPage(next) {
    setPage(next);
    linesRef.current?.focus();
  }

  if (!bill) return <LoadState state={statement}>{() => null}</LoadState>;

  const { id, status, confirmedAt, summary } = bill;
  const confirmed = status === 'confirmed';
  const csv = confirmed ? customerApi.billingExportUrl(id) : null;
  return (
    <>
      <div className="statement-overview">
        <dl className="statement-totals">
          <div className="statement-payable">
            <dt>{confirmed ? '已确认应付' : '预估应付'}</dt>
            <dd>¥{summary.payableAmount}</dd>
          </div>
          <div>
            <dt>本期计价任务</dt>
            <dd>{formatNumber(summary.usageCalls)}</dd>
          </div>
          {summary.lateCalls > 0 && (
            <div>
              <dt>补计任务</dt>
              <dd>{formatNumber(summary.lateCalls)}</dd>
            </div>
          )}
          {summary.pendingCalls > 0 && (
            <div>
              <dt>{confirmed ? '确认时待计价' : '待计价任务'}</dt>
              <dd>{formatNumber(summary.pendingCalls)}</dd>
            </div>
          )}
        </dl>
        {csv && <a className="button secondary" href={csv} download>下载账单 CSV</a>}
      </div>
      <p className="statement-note">
        {confirmed ? `${formatDate(confirmedAt)} 确认。` : '账单确认后可下载。'}
      </p>
      {!open && statement.error && <LoadState state={statement}>{() => null}</LoadState>}
      <div className="statement-disclosure">
        <button className="usage-disclosure" type="button" aria-expanded={open}
          aria-controls="statement-details-content" onClick={() => setOpen((value) => !value)}>
          {open ? '收起账单明细' : '查看账单明细'} <span className="usage-chevron" aria-hidden="true">⌄</span>
        </button>
      </div>
      <div id="statement-details-content" className="billing-results" ref={linesRef} tabIndex={-1}
        role="group" aria-label="账单明细结果" hidden={!open}>
        {open && <LoadState state={statement}>{(data) => <StatementLines data={data} onPage={goToPage} />}</LoadState>}
      </div>
    </>
  );
}

function StatementLines({ data: { items, pagination }, onPage }) {
  const { page, pageSize, total } = pagination;
  const lastPage = Math.max(1, Math.ceil(total / pageSize));
  return (
    <>
      {items.length === 0 ? (
        <p className="empty">该账期暂无已计价明细。</p>
      ) : (
        <div className="table-scroll statement-details" tabIndex={0} role="region" aria-label="账单明细">
          <table>
            <thead>
              <tr>
                <th scope="col">任务 ID / 模型</th>
                <th scope="col">折后（元）</th>
                <th scope="col">来源账期 / 结束时间</th>
                <th scope="col">输出 Token</th>
                <th scope="col">单价（元／百万 Token）</th>
                <th scope="col">折前（元）</th>
                <th scope="col">应付比例</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.taskId}>
                  <th scope="row">
                    <code>{item.taskId}</code>
                    <small className="statement-line-note">{item.model}</small>
                  </th>
                  <td>{item.amount ?? '—'}</td>
                  <td>
                    {item.sourceMonth}{item.kind === 'late_usage' && ' · 补计'}
                    <small className="statement-line-note">{formatDate(item.settledAt)}</small>
                  </td>
                  <td>{formatNumber(item.outputTokens)}</td>
                  <td>{item.unitPrice ?? '—'}</td>
                  <td>{item.listAmount ?? '—'}</td>
                  <td>{item.payableBps == null ? '—' : `${item.payableBps / 100}%`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {(page > 1 || lastPage > 1) && (
        <nav className="statement-pages" aria-label="账单明细分页">
          <button type="button" className="button secondary" disabled={page <= 1} onClick={() => onPage(Math.min(page - 1, lastPage))}>
            上一页
          </button>
          <span role="status">第 {page} 页 / 共 {lastPage} 页</span>
          <button type="button" className="button secondary" disabled={page >= lastPage} onClick={() => onPage(page + 1)}>
            下一页
          </button>
        </nav>
      )}
    </>
  );
}

const reportKinds = { video: '视频', text: '文本', image: '图片' };

function reportDates(day) {
  if (day) return { from: day, to: day };
  const today = currentDay();
  return { from: today.slice(0, 7) + '-01', to: today };
}

function reportAmount(amount, status) {
  if (amount != null) return '¥' + amount;
  if (status === 'pending') return '待计价';
  return status === 'unbilled' ? '未计费' : '—';
}

function UsageReportResults({ data, onPage }) {
  const { summary, items, total, page, pageSize } = data;
  const lastPage = Math.max(1, Math.ceil(total / pageSize));
  const totals = [
    ['调用次数', formatNumber(summary.calls)],
    ['成功调用', formatNumber(summary.successful)],
    ['输入 Token', formatNumber(summary.promptTokens)],
    ['输出 Token', formatNumber(summary.outputTokens)],
    ['生成图片', formatNumber(summary.generatedImages)],
    ['已计价金额', reportAmount(summary.amount)],
  ];
  return (
    <>
      <dl className="metric-strip usage-report-metrics">
        {totals.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
      </dl>
      {summary.pendingCalls > 0 && (
        <p className="usage-report-note">其中 {formatNumber(summary.pendingCalls)} 笔待计价。</p>
      )}
      {items.length ? (
        <div className="table-scroll usage-report-table" tabIndex={0} role="region" aria-label="调用记录表">
          <table>
            <thead>
              <tr>
                {['模型 / 调用 ID', '业务 Key', '状态', '输入 Token', '输出 Token', '生成图片', '折后金额', '创建时间']
                  .map((heading) => <th scope="col" key={heading}>{heading}</th>)}
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.kind + ':' + item.id}>
                  <th scope="row">
                    {item.model}
                    <small>{reportKinds[item.kind]} · <code>{item.id}</code></small>
                  </th>
                  <td>
                    {item.keyName ?? '—'}
                    <small><code>{item.keyPrefix}</code>{item.keyStatus === 'revoked' && ' · 已撤销'}</small>
                  </td>
                  <td><Status value={item.status} /></td>
                  <td>{formatNumber(item.promptTokens)}</td>
                  <td>{formatNumber(item.outputTokens)}</td>
                  <td>{formatNumber(item.generatedImages)}</td>
                  <td>{reportAmount(item.amount, item.billingStatus)}</td>
                  <td>
                    {formatDate(item.createdAt)}
                    {item.settledAt && <small>结束 {formatDate(item.settledAt)}</small>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : <p className="empty">所选范围暂无调用记录。</p>}
      {(page > 1 || lastPage > 1) && (
        <nav className="statement-pages" aria-label="调用记录分页">
          <button type="button" className="button secondary" disabled={page <= 1}
            onClick={() => onPage(Math.min(page - 1, lastPage))}>上一页</button>
          <span role="status">第 {page} 页 / 共 {lastPage} 页</span>
          <button type="button" className="button secondary" disabled={page >= lastPage}
            onClick={() => onPage(page + 1)}>下一页</button>
        </nav>
      )}
    </>
  );
}

function UsageReport({ day, active, onClearDay, onExpired }) {
  const [draft, setDraft] = useState(() => ({ keyId: '', model: '', ...reportDates(day) }));
  const [applied, setApplied] = useState(draft);
  const [page, setPage] = useState(1);
  const [choices, setChoices] = useState(null);
  const [exportError, setExportError] = useState('');
  const [exporting, setExporting] = useState(false);
  const resultsRef = useRef(null);
  const exportRequest = useRef(null);
  const load = useCallback((signal) => customerApi.usageReport(applied, page, signal), [applied, page]);
  const usage = useRemote(load, onExpired, active);
  const dirty = Object.keys(applied).some((field) => applied[field] !== draft[field]);

  useEffect(() => {
    if (usage.data) setChoices({ keys: usage.data.keys, models: usage.data.models });
  }, [usage.data]);
  useEffect(() => () => exportRequest.current?.abort(), []);

  function change(field, value) {
    setDraft((previous) => ({ ...previous, [field]: value }));
  }

  function filter(event) {
    event.preventDefault();
    setExportError('');
    if (dirty) setApplied(draft);
    setPage(1);
  }

  async function download() {
    if (exportRequest.current) return;
    const controller = new AbortController();
    exportRequest.current = controller;
    setExporting(true);
    setExportError('');
    try {
      const csv = await customerApi.usageReportCsv(applied, controller.signal);
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(csv);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'rich-relay-usage-' + applied.from + '_' + applied.to + '.csv';
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (controller.signal.aborted) return;
      if (error.status === 401) onExpired(true);
      else setExportError(requestErrorMessage(error));
    } finally {
      if (!controller.signal.aborted) {
        exportRequest.current = null;
        setExporting(false);
      }
    }
  }

  return (
    <>
      <form className="usage-report-filters" onSubmit={filter}>
        <label>业务 Key
          <select value={draft.keyId} disabled={!choices} onChange={(event) => change('keyId', event.target.value)}>
            <option value="">全部 Key</option>
            {choices?.keys.map((key) => (
              <option key={key.id} value={key.id}>{key.name} · {key.prefix}{key.status === 'revoked' ? '（已撤销）' : ''}</option>
            ))}
          </select>
        </label>
        <label>模型
          <select value={draft.model} disabled={!choices} onChange={(event) => change('model', event.target.value)}>
            <option value="">全部模型</option>
            {choices?.models.map((model) => <option key={model.id} value={model.id}>{model.label}</option>)}
          </select>
        </label>
        <label>开始日期
          <input type="date" value={draft.from} min="2000-01-01" max={draft.to} required
            onChange={(event) => change('from', event.target.value)} />
        </label>
        <label>结束日期
          <input type="date" value={draft.to} min={draft.from} max={currentDay()} required
            onChange={(event) => change('to', event.target.value)} />
        </label>
        <div className="usage-report-actions">
          <button className="button primary" type="submit">筛选</button>
          {day && <button className="button secondary" type="button" onClick={onClearDay}>回到本月</button>}
          <button className="button secondary" type="button" onClick={download} disabled={dirty || !usage.data || exporting}>
            {exporting ? '正在导出…' : '导出调用记录 CSV'}
          </button>
        </div>
      </form>
      {exportError && <p className="message error panel-message" role="alert">{exportError}</p>}
      <div className="billing-results" ref={resultsRef} tabIndex={-1} role="group" aria-label="调用记录查询结果">
        <LoadState state={usage}>{(data) => <UsageReportResults data={data} onPage={(next) => {
          setPage(next);
          resultsRef.current?.focus();
        }} />}</LoadState>
      </div>
    </>
  );
}

function Billing({ month, setMonth, maxMonth, day, showUsage, onClearDay, onExpired }) {
  const [lastUsageDay, setLastUsageDay] = useState(day);
  const usageBasis = useRef(null);
  const usageDay = showUsage ? day : lastUsageDay;
  useEffect(() => {
    if (showUsage) {
      setLastUsageDay(day);
      usageBasis.current?.focus();
    }
  }, [day, showUsage]);

  return (
    <section className="panel billing-workspace" aria-label="项目用量与账单">
      <nav className="billing-views" aria-label="用量与账单视图">
        <a id="billing-statement-link" href="#billing" aria-current={showUsage ? undefined : 'page'}>月度账单</a>
        <a id="billing-usage-link" href={usageDay ? `#billing?day=${usageDay}` : '#billing?usage=1'}
          aria-current={showUsage ? 'page' : undefined}>
          调用记录
        </a>
      </nav>
      <section className="billing-view" aria-labelledby="billing-statement-link" hidden={showUsage}>
        <div className="panel-head billing-view-head">
          <p>按任务结束时间归入账期（北京时间）。</p>
          <label className="month-filter">
            账期
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
        <MonthlyStatement key={month} month={month} active={!showUsage} onExpired={onExpired} />
      </section>
      <section className="billing-view" aria-labelledby="billing-usage-link" hidden={!showUsage}>
        <div className="panel-head billing-view-head">
          <p id="usage-basis" ref={usageBasis} tabIndex={-1}>按调用创建日期查询（北京时间）。</p>
        </div>
        <UsageReport key={usageDay ?? maxMonth} day={usageDay} active={showUsage}
          onClearDay={onClearDay} onExpired={onExpired} />
      </section>
    </section>
  );
}

export function Workspace({ session, onSessionEnded }) {
  const [route, setRoute] = useState(readRoute);
  const [videoKey, setVideoKey] = useState('');
  const [month, setMonth] = useState(currentMonth);
  const [loggingOut, setLoggingOut] = useState(false);
  const [error, setError] = useState('');
  const maxMonth = currentMonth();
  const keys = useRemote(customerApi.keys, onSessionEnded);
  const seedanceEnabled = session.capabilities?.seedance === true;
  const { section, day, showUsage } = route.section.seedanceOnly && !seedanceEnabled ? { section: sections[0], day: null } : route;

  useEffect(() => {
    const onHashChange = () => setRoute(readRoute());
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  useEffect(() => {
    if (!seedanceEnabled) setVideoKey('');
  }, [seedanceEnabled]);

  function clearBillingDay() {
    if (!day) return;
    window.history.replaceState(window.history.state, '', '#billing?usage=1');
    setRoute(readRoute());
  }

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
        <div className="workspace-brand">
          <Wordmark />
          <span className="workspace-context">客户工作台</span>
        </div>
        <div className="account-menu">
          <span title={session.customer.email}>{session.customer.email}</span>
          <button className="button secondary" onClick={logout} disabled={loggingOut}>
            {loggingOut ? '正在退出…' : '退出登录'}
          </button>
        </div>
      </header>
      <aside className="workspace-sidebar">
        <nav aria-label="客户工作台">
          {navigation.map(({ label, items }) => (
            <div key={label} className="workspace-nav-group">
              <p>{label}</p>
              {items.filter((item) => seedanceEnabled || !item.seedanceOnly).map((item) => (
                <a key={item.id} href={`#${item.id}`} aria-current={section.id === item.id ? 'page' : undefined}>
                  <span className="nav-mark" aria-hidden="true">{item.mark}</span>
                  {item.label}
                </a>
              ))}
            </div>
          ))}
          <a className="sidebar-docs" href="/docs/">
            <span className="nav-mark" aria-hidden="true">↗</span>
            开发者文档
          </a>
          {seedanceEnabled && <a className="sidebar-docs" href="/api/customer/docs/video">视频开发文档</a>}
        </nav>
      </aside>
      <main id="workspace-content" className="workspace-main" tabIndex={-1}>
        <div className="page-heading">
          <h1>{section.label}</h1>
          {section.description && <p>{section.description}</p>}
        </div>
        {error && (
          <p className="message error" role="alert">
            {error}
          </p>
        )}
        {section.id === 'overview' && <Overview keys={keys} month={maxMonth} customer={session.customer} onExpired={onSessionEnded} />}
        {section.id === 'api' && (
          <>
            <KeyPanel state={keys} csrfToken={session.csrfToken} onExpired={onSessionEnded} />
            <SetupGuide />
          </>
        )}
        {section.id === 'models' && <ModelPlayground onExpired={onSessionEnded} />}
        {seedanceEnabled && (
          <Suspense fallback={section.seedanceOnly ? <p role="status">正在加载…</p> : null}>
            <div hidden={section.id !== 'playground'}>
              <Playground
                active={section.id === 'playground'}
                csrfToken={session.csrfToken}
                onExpired={onSessionEnded}
                videoKey={videoKey}
                setVideoKey={setVideoKey}
                onKeysChanged={keys.refresh}
              />
            </div>
            {section.id === 'tasks' && <TaskRecords videoKey={videoKey} onExpired={onSessionEnded} />}
          </Suspense>
        )}
        {section.id === 'billing' && (
          <Billing month={month} setMonth={setMonth} maxMonth={maxMonth} day={day} showUsage={showUsage}
            onClearDay={clearBillingDay} onExpired={onSessionEnded} />
        )}
      </main>
    </div>
  );
}
