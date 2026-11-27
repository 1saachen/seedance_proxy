import { useCallback, useState } from 'react';
import { adminApi } from './admin-api.js';
import {
  beijingTime, DataTable, Dialog, LoadState, Message, Pager, useAdminMutation, useAdminQuery, yuan,
} from './admin-ui.jsx';
import { formatDate, formatNumber } from './workbench-format.jsx';

const auditActions = {
  'admin.created': '创建管理员',
  'auth.login': '管理员登录',
  'auth.login_failed': '管理员登录失败',
  'auth.login_throttled': '管理员登录限流',
  'auth.logout': '管理员退出',
  'customer.create': '开通客户',
  'project.model_access.update': '调整模型授权',
  'key.limits.update': '调整业务 Key 限额',
  'billing.statement.confirm': '确认月账单',
  'billing.terms.create': '新增计费条款',
  'billing.price.create': '新增模型价目',
  'billing.price.import': '导入火山官网价',
};

const auditTargets = {
  admin: '管理员',
  project: '客户项目',
  key: '业务 Key',
  statement: '月账单',
  price: '模型价目',
  price_catalog: '官网价目录',
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
  version: '目录版本',
  createdCount: '导入项数',
  before: '调整前',
  after: '调整后',
};

function auditValue(key, value) {
  if ((key === 'before' || key === 'after') && typeof value?.seedanceEnabled === 'boolean') {
    return value.seedanceEnabled ? 'Seedance 已授权' : 'Seedance 未授权';
  }
  if ((key === 'before' || key === 'after') && value && Object.hasOwn(value, 'concurrencyLimit')) {
    const concurrency = value.concurrencyLimit == null ? '不限' : formatNumber(value.concurrencyLimit);
    const rpm = value.rpmLimit == null ? '不限' : formatNumber(value.rpmLimit);
    return '并发 ' + concurrency + '，RPM ' + rpm;
  }
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

const priceDiff = {
  add: { label: '新增', tone: 'positive' },
  change: { label: '调价', tone: 'neutral' },
  unchanged: { label: '不变', tone: 'neutral' },
  conflict: { label: '冲突', tone: 'negative' },
};

function tomorrowInBeijing() {
  const tomorrow = new Date(Date.now() + 24 * 60 * 60 * 1000);
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(tomorrow);
  const date = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  return `${date.year}-${date.month}-${date.day}T00:00`;
}

function PriceImportPreview({ preview, counts }) {
  return (
    <>
      <div className="admin-price-preview-meta">
        <div>
          <a href={preview.sourceUrl} target="_blank" rel="noreferrer">火山引擎官网计费说明</a>
          <span>核验日期 {preview.verifiedOn} · 目录版本 {preview.version}</span>
        </div>
        <p>生效时间 {formatDate(preview.effectiveAt)}</p>
      </div>
      <dl className="admin-price-preview-counts" aria-label="本批价目差异">
        {Object.entries(priceDiff).map(([type, { label }]) => (
          <div key={type}><dt>{label}</dt><dd>{counts[type]}</dd></div>
        ))}
      </dl>
      {!preview.canApply ? (
        <p className="admin-error" role="alert">所选时间已有不同单价。请选择其他生效时间并重新预览。</p>
      ) : counts.add + counts.change === 0 ? (
        <p className="admin-state">本批价目均已一致。</p>
      ) : null}
      <DataTable label="官网价导入预览" tableClass="admin-price-preview-table"
        headings={['模型 / 档位', '原单价（元）', '官网单价（元）', '差异', '下一版本生效']}
        empty="暂无可预览的官网价。">
        {preview.items.map((row) => (
          <tr key={row.upstreamModel + ':' + row.resolution + ':' + row.containsVideoInput}>
            <td><strong>{row.label}</strong><small>{row.resolution} · {row.containsVideoInput ? '有视频输入' : '无视频输入'}</small></td>
            <td className="admin-amount">{yuan(row.previousUnitPrice)}</td>
            <td className="admin-amount">{yuan(row.unitPrice)}</td>
            <td><span className={`status ${priceDiff[row.status].tone}`}>{priceDiff[row.status].label}</span></td>
            <td>{formatDate(row.nextEffectiveAt)}</td>
          </tr>
        ))}
      </DataTable>
      {preview.items.some((row) => row.nextEffectiveAt) && (
        <p className="admin-price-import-note">已排期的后续价目仍按原时间生效。</p>
      )}
    </>
  );
}

function VolcenginePriceImport({ csrfToken, onExpired, onClose, onApplied }) {
  const [effectiveAt, setEffectiveAt] = useState(tomorrowInBeijing);
  const [preview, setPreview] = useState(null);
  const [reviewed, setReviewed] = useState(false);
  const [operation, setOperation] = useState(null);
  const mutation = useAdminMutation(onExpired);
  const counts = { add: 0, change: 0, unchanged: 0, conflict: 0 };
  for (const row of preview?.items ?? []) counts[row.status] += 1;
  const canApply = preview?.canApply && counts.add + counts.change > 0 && !mutation.error;

  async function previewPrices(event) {
    event.preventDefault();
    setOperation('preview');
    setPreview(null);
    setReviewed(false);
    const result = await mutation.run(() => adminApi.previewVolcenginePrices(beijingTime(effectiveAt)));
    if (result) setPreview(result);
  }

  async function applyPrices() {
    if (!canApply || !reviewed) return;
    setOperation('apply');
    setReviewed(false);
    const result = await mutation.run(() => adminApi.applyVolcenginePrices({
      effectiveAt: preview.effectiveAt,
      previewToken: preview.previewToken,
    }, csrfToken));
    if (result) onApplied(result);
  }

  return (
    <Dialog title="导入火山官网价" className="admin-price-import-dialog" onClose={onClose} busy={mutation.busy}>
      <p className="admin-price-import-note">导入公开基础价，单位为元 / 百万输出 Token。客户折扣仍按项目计费条款计算。</p>
      <form className="admin-price-import-controls" onSubmit={previewPrices}>
        <label className="admin-field">生效时间（北京时间）
          <input name="effectiveAt" type="datetime-local" required step="1" value={effectiveAt} disabled={mutation.busy}
            onChange={(event) => {
              setEffectiveAt(event.target.value);
              setPreview(null);
              setReviewed(false);
              setOperation(null);
            }} />
        </label>
        <button className="button secondary" disabled={mutation.busy}>
          {mutation.busy && operation === 'preview' ? '正在预览…' : preview ? '刷新预览' : '预览价目'}
        </button>
      </form>
      {operation && <Message error={mutation.error} />}
      {preview && <PriceImportPreview preview={preview} counts={counts} />}
      <div className="admin-price-import-footer">
        <label className="admin-price-import-review">
          <input type="checkbox" checked={reviewed} disabled={!canApply || mutation.busy}
            onChange={(event) => setReviewed(event.target.checked)} />
          我已核对价目差异和生效时间
        </label>
        <div className="admin-form-actions">
          <button type="button" className="button secondary" disabled={mutation.busy} onClick={onClose}>取消</button>
          <button type="button" className="button primary" disabled={!canApply || !reviewed || mutation.busy} onClick={applyPrices}>
            {mutation.busy && operation === 'apply' ? '正在导入…' : '确认导入'}
          </button>
        </div>
      </div>
    </Dialog>
  );
}

export function Prices({ onExpired, csrfToken }) {
  const [page, setPage] = useState(1);
  const [dialog, setDialog] = useState(null);
  const [notice, setNotice] = useState('');
  const load = useCallback((signal) => adminApi.prices(page, signal), [page]);
  const resource = useAdminQuery(load, onExpired);
  const models = resource.data?.models || [];

  function refreshPrices(message) {
    setDialog(null);
    setNotice(message);
    setPage(1);
    resource.reload();
  }

  return (
    <>
      <div className="admin-page-heading">
        <h1>模型价目</h1>
        <div className="admin-price-heading-actions">
          <button type="button" className="button secondary" disabled={!models.length} onClick={() => setDialog('manual')}>
            新增模型价目
          </button>
          <button type="button" className="button primary" onClick={() => setDialog('import')}>导入火山官网价</button>
        </div>
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
      {dialog === 'manual' && <PriceForm models={models} csrfToken={csrfToken} onExpired={onExpired}
        onClose={() => setDialog(null)} onSaved={() => refreshPrices('模型价目已保存。')} />}
      {dialog === 'import' && <VolcenginePriceImport csrfToken={csrfToken} onExpired={onExpired}
        onClose={() => setDialog(null)} onApplied={({ createdCount, skippedCount }) => (
          refreshPrices(`已导入 ${createdCount} 项官网价，跳过 ${skippedCount} 项未变化价目。`)
        )} />}
    </>
  );
}
