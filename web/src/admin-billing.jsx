import { useCallback, useState } from 'react';
import { adminApi } from './admin-api.js';
import {
  beijingMonth, beijingTime, DataTable, Dialog, LoadState, Message, MonthField, Pager, useAdminMutation, useAdminQuery, yuan,
} from './admin-ui.jsx';
import { formatDate, formatNumber, Status } from './workbench-format.jsx';

const pendingReasons = {
  missing_usage: '缺少实际用量',
  missing_pricing_dimensions: '缺少计价条件',
  missing_price: '缺少适用价目',
  missing_settlement: '缺少任务结束时间',
};

function billingStatus(billing) {
  if (billing?.status === 'priced') return '已计价';
  if (billing?.status === 'pending') return pendingReasons[billing.pendingReason] || '待计价';
  if (billing?.status === 'unbilled') return '未启用计费';
  return '—';
}

function Metrics({ items }) {
  return (
    <dl className="admin-metrics">
      {items.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}
    </dl>
  );
}

export function Usage({ projectId, onExpired }) {
  const [month, setMonth] = useState(beijingMonth);
  const [page, setPage] = useState(1);
  const load = useCallback((signal) => adminApi.usage(projectId, month, page, signal), [projectId, month, page]);
  const resource = useAdminQuery(load, onExpired);
  return (
    <>
      <div className="admin-toolbar">
        <div><h3>用量核查</h3><p>按任务创建时间统计，北京时间。</p></div>
        <MonthField label="用量月份" month={month} onChange={(next) => { setMonth(next); setPage(1); }} />
      </div>
      <LoadState resource={resource}>{({ items, summary, page, pageSize }) => (
        <>
          <Metrics items={[
            ['提交任务', formatNumber(summary.calls)],
            ['成功任务', formatNumber(summary.successful)],
            ['输出 Token', formatNumber(summary.outputTokens)],
            ['已计价', formatNumber(summary.pricedCalls)],
            ['待计价', formatNumber(summary.pendingCalls)],
          ]} />
          <DataTable label="项目用量明细" headings={['任务 ID / 模型', '创建时间', '状态', '输出 Token', '计价状态', '折后金额']}
            empty="本月暂无任务记录。">
            {items.map((item) => (
              <tr key={item.id}>
                <td><code>{item.id}</code><small>{item.model}</small></td>
                <td>{formatDate(item.createdAt)}</td><td><Status value={item.status} /></td>
                <td>{formatNumber(item.outputTokens)}</td>
                <td className={item.billing?.status === 'pending' ? 'admin-attention' : undefined}>
                  {billingStatus(item.billing)}
                </td>
                <td className="admin-amount">{yuan(item.billing?.amount)}</td>
              </tr>
            ))}
          </DataTable>
          <Pager page={page} pageSize={pageSize} total={summary.calls} onChange={setPage} />
        </>
      )}</LoadState>
    </>
  );
}

function ConfirmStatement({ project, statement, csrfToken, onExpired, onClose, onRefresh, onConfirmed }) {
  const mutation = useAdminMutation(onExpired);
  const stale = mutation.code === 'statement_amount_changed';

  async function confirm(event) {
    event.preventDefault();
    const frozen = await mutation.run(() =>
      adminApi.confirmStatement(project.id, statement.month, statement.summary.payableAmount, csrfToken),
    );
    if (frozen) onConfirmed(frozen);
  }

  return (
    <Dialog title="确认并冻结月账单" onClose={onClose} busy={mutation.busy}>
      <form className="admin-form" onSubmit={confirm}>
        <p>确认后，本期金额与明细将冻结，客户可在工作台查看并导出。</p>
        <dl className="admin-confirm-facts">
          <div><dt>客户</dt><dd>{project.email ?? project.id}</dd></div>
          <div><dt>账期</dt><dd>{statement.month}</dd></div>
          <div><dt>预估应付</dt><dd>{yuan(statement.summary.payableAmount)}</dd></div>
        </dl>
        {statement.summary.pendingCalls > 0 && (
          <p className="admin-attention">本期仍有 {statement.summary.pendingCalls} 笔待计价，补齐后将计入后续开放账期。</p>
        )}
        <Message error={mutation.error} />
        <div className="admin-form-actions">
          <button type="button" className="button secondary" autoFocus disabled={mutation.busy} onClick={onClose}>取消</button>
          {stale ? (
            <button type="button" className="button primary" onClick={onRefresh}>刷新账单</button>
          ) : (
            <button className="button primary" disabled={mutation.busy}>
              {mutation.busy ? '正在确认…' : '确认并冻结'}
            </button>
          )}
        </div>
      </form>
    </Dialog>
  );
}

export function Statements({ project, onExpired, csrfToken }) {
  const [month, setMonth] = useState(beijingMonth);
  const [page, setPage] = useState(1);
  const [confirming, setConfirming] = useState(null);
  const [notice, setNotice] = useState('');
  const load = useCallback((signal) => adminApi.statement(project.id, month, page, signal), [project.id, month, page]);
  const resource = useAdminQuery(load, onExpired);

  function confirmed(frozen) {
    setConfirming(null);
    setNotice(frozen.month + ' 月账单已确认，冻结应付 ' + yuan(frozen.summary.payableAmount) + '。');
    setPage(1);
    resource.reload();
  }

  return (
    <>
      <div className="admin-toolbar">
        <div><h3>月账单</h3><p>按任务结算月份归集，北京时间。</p></div>
        <MonthField label="账期" month={month} onChange={(next) => { setMonth(next); setPage(1); setNotice(''); }} />
      </div>
      <Message notice={notice} />
      <LoadState resource={resource}>{(statement) => {
        const { summary, status, items, confirmedAt, pagination } = statement;
        const frozen = status === 'confirmed';
        return (
          <>
            <div className="admin-statement-total">
              <div><p>{frozen ? '已确认应付' : '预估应付'}</p><strong>{yuan(summary.payableAmount)}</strong></div>
              <div>
                {frozen ? (
                  <><span className="admin-frozen">已冻结</span><p>确认时间 {formatDate(confirmedAt)}</p></>
                ) : month < beijingMonth() ? (
                  <button type="button" className="button primary" onClick={() => setConfirming(statement)}>确认账单</button>
                ) : <p>账期结束后可以确认</p>}
              </div>
            </div>
            <Metrics items={[
              ['折前金额', yuan(summary.listAmount)],
              ['当期用量', formatNumber(summary.usageCalls)],
              ['迟到补计', formatNumber(summary.lateCalls)],
              ['待计价', formatNumber(summary.pendingCalls)],
            ]} />
            <DataTable label="月账单明细" headings={['任务 ID / 模型', '来源账期', '结算时间', '输出 Token', '折后金额']}
              empty="本账期暂无已计价明细。">
              {items.map((item) => (
                <tr key={item.taskId}>
                  <td><code>{item.taskId}</code><small>{item.model}</small></td>
                  <td>{item.sourceMonth}{item.kind === 'late_usage' && <small>迟到补计</small>}</td>
                  <td>{formatDate(item.settledAt)}</td><td>{formatNumber(item.outputTokens)}</td>
                  <td className="admin-amount">{yuan(item.amount)}</td>
                </tr>
              ))}
            </DataTable>
            <Pager {...pagination} onChange={setPage} />
          </>
        );
      }}</LoadState>
      {confirming && <ConfirmStatement statement={confirming} project={project} onExpired={onExpired}
        csrfToken={csrfToken} onClose={() => setConfirming(null)} onConfirmed={confirmed}
        onRefresh={() => { setConfirming(null); resource.reload(); }} />}
    </>
  );
}

function TermsForm({ projectId, csrfToken, onExpired, onSaved, onClose }) {
  const mutation = useAdminMutation(onExpired);
  const [enabled, setEnabled] = useState(true);

  async function save(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const [whole, fraction = ''] = String(form.get('payable')).split('.');
    const payableBps = enabled ? Number(whole) * 100 + Number(fraction.padEnd(2, '0')) : 10000;
    const terms = await mutation.run(() => adminApi.createTerms(projectId, {
      effectiveAt: beijingTime(form.get('effectiveAt')),
      enabled,
      payableBps,
    }, csrfToken));
    if (terms) onSaved();
  }

  return (
    <Dialog title="新增计费条款" onClose={onClose} busy={mutation.busy}>
      <form className="admin-form" onSubmit={save}>
        <label className="admin-field">计费状态
          <select value={enabled ? 'enabled' : 'disabled'} onChange={(event) => setEnabled(event.target.value === 'enabled')}>
            <option value="enabled">启用计费</option><option value="disabled">停用计费</option>
          </select>
        </label>
        {enabled && <label className="admin-field">应付比例（%）
          <input name="payable" type="text" inputMode="decimal" pattern="[0-9]{1,2}(\.[0-9]{1,2})?|100(\.0{1,2})?"
            required maxLength={6} defaultValue="100" aria-describedby="admin-payable-hint" />
          <small id="admin-payable-hint">例如，应付 80% 即按原价打 8 折。</small>
        </label>}
        <label className="admin-field">生效时间（北京时间）
          <input name="effectiveAt" type="datetime-local" required step="1" />
        </label>
        <Message error={mutation.error} />
        <div className="admin-form-actions">
          <button type="button" className="button secondary" disabled={mutation.busy} onClick={onClose}>取消</button>
          <button className="button primary" disabled={mutation.busy}>{mutation.busy ? '正在保存…' : '保存计费条款'}</button>
        </div>
      </form>
    </Dialog>
  );
}

export function ProjectTerms({ projectId, onExpired, csrfToken }) {
  const [editing, setEditing] = useState(false);
  const [notice, setNotice] = useState('');
  const load = useCallback((signal) => adminApi.terms(projectId, signal), [projectId]);
  const resource = useAdminQuery(load, onExpired);
  return (
    <>
      <div className="admin-toolbar">
        <div><h3>计费条款</h3><p>项目默认未计费，按条款的生效时间应用应付比例。</p></div>
        <button type="button" className="button primary" onClick={() => setEditing(true)}>新增计费条款</button>
      </div>
      <Message notice={notice} />
      <LoadState resource={resource}>{({ items }) => (
        <DataTable label="项目计费条款" headings={['版本', '生效时间', '计费状态', '应付比例']} empty="该项目还未设置计费条款。">
          {items.map((item) => (
            <tr key={item.id}>
              <td>{item.id}</td><td>{formatDate(item.effectiveAt)}</td><td>{item.enabled ? '启用' : '停用'}</td>
              <td>{item.enabled ? item.payableBps / 100 + '%' : '—'}</td>
            </tr>
          ))}
        </DataTable>
      )}</LoadState>
      {editing && <TermsForm projectId={projectId} csrfToken={csrfToken} onExpired={onExpired}
        onClose={() => setEditing(false)} onSaved={() => {
          setEditing(false);
          setNotice('计费条款已保存。');
          resource.reload();
        }} />}
    </>
  );
}
