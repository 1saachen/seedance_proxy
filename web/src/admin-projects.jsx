import { useCallback, useState } from 'react';
import { adminApi } from './admin-api.js';
import { ProjectTerms, Statements, Usage } from './admin-billing.jsx';
import { DataTable, Dialog, LoadState, Message, Pager, useAdminMutation, useAdminQuery } from './admin-ui.jsx';
import { formatDate, formatNumber, Status } from './workbench-format.jsx';

function CreateCustomer({ csrfToken, onClose, onCreated, onExpired }) {
  const mutation = useAdminMutation(onExpired);

  async function submit(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    const item = await mutation.run(() => adminApi.createProject({
      email: fields.get('email').trim(),
      password: fields.get('password'),
    }, csrfToken));
    if (item) {
      form.reset();
      onCreated(item);
    }
  }

  return (
    <Dialog title="开通客户" onClose={onClose} busy={mutation.busy}>
      <form className="admin-form" onSubmit={submit}>
        <label className="admin-field">客户邮箱
          <input name="email" type="email" required maxLength={254} autoComplete="off" autoFocus />
        </label>
        <label className="admin-field">初始密码
          <input name="password" type="password" required minLength={8} maxLength={128}
            autoComplete="new-password" aria-describedby="admin-new-customer-hint" />
        </label>
        <small id="admin-new-customer-hint">使用 8 至 128 个字符。开通后，客户可通过该邮箱登录工作台。</small>
        <Message error={mutation.error} />
        <div className="admin-form-actions">
          <button type="button" className="button secondary" disabled={mutation.busy} onClick={onClose}>取消</button>
          <button className="button primary" disabled={mutation.busy}>
            {mutation.busy ? '正在开通…' : '确认开通'}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function KeyLimitField({ label, name, limit, max }) {
  return (
    <label className="admin-field">
      {label}
      <input name={name} type="number" min={1} max={max} step={1}
        defaultValue={limit ?? ''} aria-describedby={'key-limit-hint-' + name} />
      <small id={'key-limit-hint-' + name}>填写 1 至 {formatNumber(max)} 的整数，留空表示不限。</small>
    </label>
  );
}

function KeyLimitsDialog({ projectId, apiKey, csrfToken, onExpired, onClose, onSaved }) {
  const mutation = useAdminMutation(onExpired);

  async function save(event) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const concurrency = fields.get('concurrencyLimit');
    const rpm = fields.get('rpmLimit');
    const updated = await mutation.run(() => adminApi.updateKeyLimits(projectId, apiKey.id, {
      concurrencyLimit: concurrency === '' ? null : Number(concurrency),
      rpmLimit: rpm === '' ? null : Number(rpm),
    }, csrfToken));
    if (updated) onSaved(updated);
  }

  return (
    <Dialog title="调整业务 Key 限额" onClose={onClose} busy={mutation.busy}>
      <form className="admin-form" onSubmit={save}>
        <p className="admin-key-name">{apiKey.name} <code>{apiKey.prefix}</code></p>
        <KeyLimitField label="并发任务上限" name="concurrencyLimit" limit={apiKey.concurrencyLimit} max={1000} />
        <KeyLimitField label="每分钟请求上限（RPM）" name="rpmLimit" limit={apiKey.rpmLimit} max={60000} />
        <Message error={mutation.error} />
        <div className="admin-form-actions">
          <button type="button" className="button secondary" disabled={mutation.busy} onClick={onClose}>取消</button>
          <button className="button primary" disabled={mutation.busy}>{mutation.busy ? '正在保存…' : '保存限额'}</button>
        </div>
      </form>
    </Dialog>
  );
}

function keyUsage(count, limit) {
  return formatNumber(count) + ' / ' + (limit == null ? '不限' : formatNumber(limit));
}

function ProjectKeys({ projectId, onExpired, csrfToken }) {
  const load = useCallback((signal) => adminApi.keys(projectId, signal), [projectId]);
  const resource = useAdminQuery(load, onExpired);
  const refresh = useAdminMutation(onExpired);
  const [snapshot, setSnapshot] = useState(null);
  const [editing, setEditing] = useState(null);
  const [notice, setNotice] = useState('');

  async function refreshKeys() {
    setNotice('');
    const result = await refresh.run(() => adminApi.keys(projectId));
    if (result) {
      setSnapshot(result.keys);
      setNotice('业务 Key 用量已刷新。');
    }
  }

  function saved(updated) {
    setSnapshot((previous) =>
      (previous ?? resource.data.keys).map((item) => item.id === updated.id ? updated : item),
    );
    setEditing(null);
    setNotice('业务 Key 限额已保存。');
  }

  return (
    <>
      <div className="admin-toolbar">
        <div>
          <h3>业务 Key</h3>
          <p>并发统计排队、运行及提交待确认等视频任务。RPM 统计近 60 秒内通过 Key 限流检查的业务 API 请求。</p>
        </div>
        <button type="button" className="button secondary" disabled={!resource.data || refresh.busy} onClick={refreshKeys}>
          {refresh.busy ? '正在刷新…' : '刷新用量'}
        </button>
      </div>
      <Message error={refresh.error} notice={notice} />
      <LoadState resource={resource}>{({ keys }) => (
        <DataTable label="项目业务 Key" tableClass="admin-key-table"
          headings={['名称 / 前缀', '状态', '视频并发', '最近 60 秒请求', '创建时间', '操作']}
          empty="该项目还未创建业务 Key。">
          {(snapshot ?? keys).map((key) => (
            <tr key={key.id}>
              <td>{key.name}<small><code>{key.prefix}</code></small></td>
              <td><Status value={key.status} /></td>
              <td className="admin-key-usage">{keyUsage(key.activeTasks, key.concurrencyLimit)}</td>
              <td className="admin-key-usage">{keyUsage(key.requestsLastMinute, key.rpmLimit)}</td>
              <td>{formatDate(key.createdAt)}</td>
              <td>{key.status === 'active' ? (
                <button type="button" className="button secondary" disabled={refresh.busy}
                  onClick={() => setEditing(key)} aria-label={'调整限额：' + key.name}>调整限额</button>
              ) : '—'}</td>
            </tr>
          ))}
        </DataTable>
      )}</LoadState>
      {editing && <KeyLimitsDialog projectId={projectId} apiKey={editing} csrfToken={csrfToken}
        onExpired={onExpired} onClose={() => setEditing(null)} onSaved={saved} />}
    </>
  );
}

function ProjectModelAccess({ project, csrfToken, onExpired, onSaved }) {
  const mutation = useAdminMutation(onExpired);
  const [notice, setNotice] = useState('');
  const enabled = project.seedanceEnabled === true;

  async function save() {
    setNotice('');
    const result = await mutation.run(() => adminApi.updateModelAccess(project.id, !enabled, csrfToken));
    if (result) {
      onSaved(project.id, result.seedanceEnabled);
      setNotice(result.seedanceEnabled ? 'Seedance 已授权。' : 'Seedance 授权已撤销。');
    }
  }

  return (
    <>
      <div className="admin-toolbar">
        <div>
          <h3>Seedance 视频模型</h3>
          <p>{enabled ? '已授权' : '未授权'}。授权包含视频 API、体验入口、素材管理和视频开发者文档。</p>
          <p>适用于此客户项目下的所有业务 Key。</p>
        </div>
        <button type="button" className={`button ${enabled ? 'secondary' : 'primary'}`} onClick={save}
          disabled={mutation.busy}>{mutation.busy ? '正在保存…' : enabled ? '撤销授权' : '授权 Seedance'}</button>
      </div>
      <Message error={mutation.error} notice={notice} />
    </>
  );
}

function ProjectDetail({ project, onExpired, csrfToken, onModelAccessSaved }) {
  const [section, setSection] = useState('usage');
  return (
    <section className="admin-panel admin-project-detail" aria-labelledby="admin-project-email">
      <div className="admin-project-heading">
        <h2 id="admin-project-email">{project.email ?? project.id}</h2>
        <dl className="admin-project-facts">
          <div><dt>项目 ID</dt><dd><code>{project.id}</code></dd></div>
          <div><dt>开通时间</dt><dd>{formatDate(project.createdAt)}</dd></div>
          <div><dt>业务 Key</dt><dd>{project.activeKeys} 枚有效，{project.revokedKeys} 枚已撤销</dd></div>
        </dl>
      </div>
      <div className="admin-project-tabs" aria-label="项目功能">
        {[
          ['usage', '用量核查'],
          ['statements', '月账单'],
          ['terms', '计费条款'],
          ['keys', '业务 Key'],
          ['models', '模型授权'],
        ].map(([id, text]) => (
          <button key={id} type="button" aria-pressed={section === id} onClick={() => setSection(id)}>{text}</button>
        ))}
      </div>
      <div className="admin-panel-body">
        {section === 'usage' && <Usage projectId={project.id} onExpired={onExpired} />}
        {section === 'statements' && <Statements project={project} onExpired={onExpired} csrfToken={csrfToken} />}
        {section === 'terms' && <ProjectTerms projectId={project.id} onExpired={onExpired} csrfToken={csrfToken} />}
        {section === 'keys' && <ProjectKeys projectId={project.id} onExpired={onExpired} csrfToken={csrfToken} />}
        {section === 'models' && <ProjectModelAccess project={project} onExpired={onExpired} csrfToken={csrfToken}
          onSaved={onModelAccessSaved} />}
      </div>
    </section>
  );
}

export function Projects({ onExpired, csrfToken }) {
  const [query, setQuery] = useState({ q: '', page: 1 });
  const [search, setSearch] = useState('');
  const [selected, setSelected] = useState(null);
  const [creating, setCreating] = useState(false);
  const [notice, setNotice] = useState('');
  const load = useCallback((signal) => adminApi.projects(query.q, query.page, signal), [query]);
  const resource = useAdminQuery(load, onExpired);

  function searchProjects(event) {
    event.preventDefault();
    setQuery({ q: search.trim(), page: 1 });
  }

  function opened(project) {
    setCreating(false);
    setSelected(project);
    setSearch(project.email);
    setQuery({ q: project.email, page: 1 });
    setNotice('客户项目已开通。');
  }

  function updateSelectedAccess(projectId, seedanceEnabled) {
    setSelected((current) => current?.id === projectId ? { ...current, seedanceEnabled } : current);
    resource.reload();
  }

  return (
    <>
      <div className="admin-page-heading">
        <h1>客户项目</h1>
        <button type="button" className="button primary" onClick={() => setCreating(true)}>开通客户</button>
      </div>
      <Message notice={notice} />
      <div className="admin-project-grid">
        <section className="admin-panel admin-project-index" aria-labelledby="admin-projects-label">
          <div className="admin-index-heading">
            <h2 id="admin-projects-label">项目列表</h2>
            <form className="admin-search" onSubmit={searchProjects}>
              <label htmlFor="admin-project-search" className="admin-sr-only">按客户邮箱或项目 ID 搜索</label>
              <input id="admin-project-search" type="search" placeholder="客户邮箱或项目 ID" maxLength={254}
                value={search} onChange={(event) => setSearch(event.target.value)} />
              <button className="button secondary">搜索</button>
            </form>
          </div>
          <LoadState resource={resource}>{({ items, page, pageSize, total }) => (
            <>
              {items.length ? (
                <ul className="admin-project-list">
                  {items.map((project) => (
                    <li key={project.id}>
                      <button type="button" aria-pressed={selected?.id === project.id} onClick={() => setSelected(project)}>
                        <span>{project.email ?? project.id}</span><small>{project.activeKeys} 枚有效 Key</small>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : <p className="admin-state">{query.q ? '没有符合条件的客户项目。' : '还没有客户项目。'}</p>}
              <Pager page={page} pageSize={pageSize} total={total}
                onChange={(next) => setQuery({ q: query.q, page: next })} />
            </>
          )}</LoadState>
        </section>
        {selected ? <ProjectDetail key={selected.id} project={selected} onExpired={onExpired} csrfToken={csrfToken}
          onModelAccessSaved={updateSelectedAccess} /> : (
          <div className="admin-panel admin-project-empty">
            <p>选择一个客户项目，查看用量和月账单。</p>
          </div>
        )}
      </div>
      {creating && <CreateCustomer csrfToken={csrfToken} onExpired={onExpired} onCreated={opened}
        onClose={() => setCreating(false)} />}
    </>
  );
}
