import { useCallback, useState } from 'react';
import { adminApi } from './admin-api.js';
import { ProjectTerms, Statements, Usage } from './admin-billing.jsx';
import { DataTable, Dialog, LoadState, Message, Pager, useAdminMutation, useAdminQuery } from './admin-ui.jsx';
import { formatDate, Status } from './workbench-format.jsx';

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

function ProjectKeys({ projectId, onExpired }) {
  const load = useCallback((signal) => adminApi.keys(projectId, signal), [projectId]);
  const resource = useAdminQuery(load, onExpired);
  return (
    <LoadState resource={resource}>{({ keys }) => (
      <DataTable label="项目业务 Key" headings={['名称', '前缀', '状态', '创建时间']} empty="该项目还未创建业务 Key。">
        {keys.map((key) => (
          <tr key={key.id}>
            <td>{key.name}</td><td><code>{key.prefix}</code></td><td><Status value={key.status} /></td>
            <td>{formatDate(key.createdAt)}</td>
          </tr>
        ))}
      </DataTable>
    )}</LoadState>
  );
}

function ProjectDetail({ project, onExpired, csrfToken }) {
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
        ].map(([id, text]) => (
          <button key={id} type="button" aria-pressed={section === id} onClick={() => setSection(id)}>{text}</button>
        ))}
      </div>
      <div className="admin-panel-body">
        {section === 'usage' && <Usage projectId={project.id} onExpired={onExpired} />}
        {section === 'statements' && <Statements project={project} onExpired={onExpired} csrfToken={csrfToken} />}
        {section === 'terms' && <ProjectTerms projectId={project.id} onExpired={onExpired} csrfToken={csrfToken} />}
        {section === 'keys' && <ProjectKeys projectId={project.id} onExpired={onExpired} />}
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
        {selected ? <ProjectDetail key={selected.id} project={selected} onExpired={onExpired} csrfToken={csrfToken} /> : (
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
