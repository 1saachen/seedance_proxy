import { useCallback, useState } from 'react';
import { adminApi } from './admin-api.js';
import {
  beijingTime, DataTable, Dialog, LoadState, Message, Pager, useAdminMutation, useAdminQuery, yuan,
} from './admin-ui.jsx';
import { formatDate } from './workbench-format.jsx';

const auditActions = {
  'admin.created': '创建管理员',
  'auth.login': '管理员登录',
  'auth.login_failed': '管理员登录失败',
  'auth.login_throttled': '管理员登录限流',
  'auth.logout': '管理员退出',
  'customer.create': '开通客户',
  'billing.statement.confirm': '确认月账单',
  'billing.terms.create': '新增计费条款',
  'billing.price.create': '新增模型价目',
};

const auditTargets = {
  admin: '管理员',
  project: '客户项目',
  statement: '月账单',
  price: '模型价目',
};

const auditFields = {
  customerId: '客户 ID',
  projectId: '项目 ID',
  month: '账期',
  payableAmount: '应付金额',
  termsVersion: '条款版本',
  effectiveAt: '生效时间',
  enabled: '计费启用',
  payableBps: '应付比例',
  upstreamModel: '上游模型',
  resolution: '输出分辨率',
  containsVideoInput: '包含视频输入',
  unitPrice: '单价',
};

function auditValue(key, value) {
  if (key === 'effectiveAt') return formatDate(value);
  if (key === 'payableBps') return value / 100 + '%';
  if (key === 'payableAmount' || key === 'unitPrice') return yuan(value);
  if (typeof value === 'boolean') return value ? '是' : '否';
  return typeof value === 'object' ? JSON.stringify(value) : String(value ?? '—');
}

export function Audit({ onExpired }) {
  const [page, setPage] = useState(1);
  const load = useCallback((signal) => adminApi.audit(page, signal), [page]);
  const resource = useAdminQuery(load, onExpired);
  return (
    <>
      <div className="admin-page-heading"><h1>操作审计</h1></div>
      <section className="admin-panel admin-panel-body" aria-label="审计事件">
        <LoadState resource={resource}>{({ items, total, page, pageSize }) => (
          <>
            <DataTable label="操作审计记录" tableClass="admin-audit-table" headings={['时间（北京时间）', '操作者', '操作', '对象', '详情']}
              empty="暂时没有操作审计记录。">
              {items.map((entry) => {
                const fields = Object.entries(entry.details || {});
                return (
                  <tr key={entry.id}>
                    <td>{formatDate(entry.createdAt)}</td><td>{entry.actor || '—'}</td>
                    <td><strong>{auditActions[entry.action] || entry.action}</strong></td>
                    <td>
                      {auditTargets[entry.targetType] || entry.targetType || '—'}
                      {entry.targetId && <small><code>{entry.targetId}</code></small>}
                    </td>
                    <td>{fields.length ? (
                      <details className="admin-audit-details">
                        <summary>查看详情</summary>
                        <dl>{fields.map(([key, value]) => (
                          <div key={key}><dt>{auditFields[key] || key}</dt><dd>{auditValue(key, value)}</dd></div>
                        ))}</dl>
                      </details>
                    ) : '—'}</td>
                  </tr>
                );
              })}
            </DataTable>
            <Pager page={page} pageSize={pageSize} total={total} onChange={setPage} />
          </>
        )}</LoadState>
      </section>
    </>
  );
}

function PriceForm({ models, csrfToken, onExpired, onClose, onSaved }) {
  const mutation = useAdminMutation(onExpired);
  const [modelId, setModelId] = useState(models[0].id);
  const model = models.find((entry) => entry.id === modelId);

  async function save(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const item = await mutation.run(() => adminApi.createPrice({
      upstreamModel: modelId,
      resolution: form.get('resolution'),
      containsVideoInput: form.get('videoInput') === 'yes',
      effectiveAt: beijingTime(form.get('effectiveAt')),
      unitPrice: form.get('unitPrice'),
      source: form.get('source').trim(),
    }, csrfToken));
    if (item) onSaved();
  }

  return (
    <Dialog title="新增模型价目" onClose={onClose} busy={mutation.busy}>
      <form className="admin-form" onSubmit={save}>
        <label className="admin-field">模型
          <select value={modelId} onChange={(event) => setModelId(event.target.value)}>
            {models.map((entry) => <option key={entry.id} value={entry.id}>{entry.label}</option>)}
          </select>
        </label>
        <div className="admin-form-pair">
          <label className="admin-field">输出分辨率
            <select name="resolution" key={modelId}>
              {model.resolutions.map((resolution) => <option key={resolution}>{resolution}</option>)}
            </select>
          </label>
          <label className="admin-field">视频输入
            <select name="videoInput">
              <option value="no">无视频输入</option><option value="yes">有视频输入</option>
            </select>
          </label>
        </div>
        <label className="admin-field">单价（元 / 百万 Token）
          <input name="unitPrice" inputMode="decimal" pattern="[0-9]{1,7}(\.[0-9]{1,6})?"
            required maxLength={14} />
        </label>
        <label className="admin-field">生效时间（北京时间）
          <input name="effectiveAt" type="datetime-local" required step="1" />
        </label>
        <label className="admin-field">价目来源
          <input name="source" required maxLength={200} />
        </label>
        <Message error={mutation.error} />
        <div className="admin-form-actions">
          <button type="button" className="button secondary" disabled={mutation.busy} onClick={onClose}>取消</button>
          <button className="button primary" disabled={mutation.busy}>{mutation.busy ? '正在保存…' : '保存价目'}</button>
        </div>
      </form>
    </Dialog>
  );
}

export function Prices({ onExpired, csrfToken }) {
  const [page, setPage] = useState(1);
  const [editing, setEditing] = useState(false);
  const [notice, setNotice] = useState('');
  const load = useCallback((signal) => adminApi.prices(page, signal), [page]);
  const resource = useAdminQuery(load, onExpired);
  const models = resource.data?.models || [];

  return (
    <>
      <div className="admin-page-heading">
        <h1>模型价目</h1>
        <button type="button" className="button primary" disabled={!models.length} onClick={() => setEditing(true)}>
          新增模型价目
        </button>
      </div>
      <Message notice={notice} />
      <section className="admin-panel admin-panel-body" aria-label="模型价目版本">
        <LoadState resource={resource}>{({ items, total, page, pageSize }) => (
          <>
            <DataTable label="模型价目版本" headings={['模型 / 版本', '输出分辨率', '视频输入', '元 / 百万 Token', '生效时间', '价目来源']}
              empty="还没有模型价目。">
              {items.map((item) => (
                <tr key={item.id}>
                  <td>
                    {models.find((model) => model.id === item.upstreamModel)?.label || item.upstreamModel}
                    <small>价目版本 {item.id}</small>
                  </td>
                  <td>{item.resolution}</td><td>{item.containsVideoInput ? '有' : '无'}</td>
                  <td className="admin-amount">{yuan(item.unitPrice)}</td>
                  <td>{formatDate(item.effectiveAt)}</td><td>{item.source}</td>
                </tr>
              ))}
            </DataTable>
            <Pager page={page} pageSize={pageSize} total={total} onChange={setPage} />
          </>
        )}</LoadState>
      </section>
      {editing && <PriceForm models={models} csrfToken={csrfToken} onExpired={onExpired}
        onClose={() => setEditing(false)} onSaved={() => {
          setEditing(false);
          setNotice('模型价目已保存。');
          setPage(1);
          resource.reload();
        }} />}
    </>
  );
}
