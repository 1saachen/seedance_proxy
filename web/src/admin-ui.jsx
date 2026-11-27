import { useEffect, useRef, useState } from 'react';
import { adminErrorMessage } from './admin-api.js';

export function useAdminQuery(load, onExpired) {
  const [attempt, setAttempt] = useState(0);
  const [result, setResult] = useState(null);

  useEffect(() => {
    const controller = new AbortController();
    load(controller.signal).then((data) => {
      if (!controller.signal.aborted) setResult({ load, attempt, data });
    }, (error) => {
      if (controller.signal.aborted) return;
      if (error.status === 401) onExpired();
      else setResult({ load, attempt, error: adminErrorMessage(error) });
    });
    return () => controller.abort();
  }, [load, onExpired, attempt]);

  const current = result?.load === load && result.attempt === attempt ? result : null;
  return { ...current, loading: current === null, reload: () => setAttempt((value) => value + 1) };
}

export function useAdminMutation(onExpired) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const inFlight = useRef(false);

  async function run(write) {
    if (inFlight.current) return undefined;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      return await write();
    } catch (problem) {
      if (problem.status === 401) onExpired();
      else setError({ message: adminErrorMessage(problem), code: problem.code });
      return undefined;
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return { busy, error: error?.message, code: error?.code, run };
}

export function LoadState({ resource, children }) {
  if (resource.loading) return <p className="admin-state" role="status">正在加载…</p>;
  if (resource.error)
    return (
      <div className="admin-state">
        <p className="admin-error" role="alert">{resource.error}</p>
        <button type="button" className="button secondary" onClick={resource.reload}>重新加载</button>
      </div>
    );
  return children(resource.data);
}

export function Pager({ page, pageSize, total, onChange }) {
  const lastPage = Math.max(1, Math.ceil(total / pageSize));
  if (lastPage === 1) return null;
  return (
    <div className="admin-pager" aria-label="分页">
      <span>第 {page} / {lastPage} 页，共 {total} 条</span>
      <button type="button" className="button secondary" disabled={page <= 1}
        onClick={() => onChange(page - 1)}>上一页</button>
      <button type="button" className="button secondary" disabled={page >= lastPage}
        onClick={() => onChange(page + 1)}>下一页</button>
    </div>
  );
}

export function DataTable({ label, headings, empty, children, tableClass = '' }) {
  if (!children?.length) return <p className="admin-state">{empty}</p>;
  return (
    <div className="admin-table-scroll" role="region" aria-label={label} tabIndex={0}>
      <table className={`admin-table ${tableClass}`} aria-label={label}>
        <thead><tr>{headings.map((heading) => <th key={heading} scope="col">{heading}</th>)}</tr></thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

export function Dialog({ title, onClose, busy, children }) {
  const dialog = useRef(null);
  useEffect(() => {
    const element = dialog.current;
    element.showModal();
    return () => element.close();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="admin-dialog"
      aria-labelledby="admin-dialog-title"
      onCancel={(event) => {
        event.preventDefault();
        if (!busy) onClose();
      }}
    >
      <h2 id="admin-dialog-title">{title}</h2>
      {children}
    </dialog>
  );
}

export function Message({ error, notice }) {
  if (error) return <p className="admin-error" role="alert">{error}</p>;
  return notice ? <p className="admin-notice" role="status">{notice}</p> : null;
}

export function MonthField({ label, month, onChange }) {
  return (
    <label className="admin-field admin-month">
      {label}
      <input name="month" type="month" min="2000-01" max={beijingMonth()} value={month}
        onChange={(event) => {
          if (/^2\d{3}-(0[1-9]|1[0-2])$/.test(event.target.value)) onChange(event.target.value);
        }} />
    </label>
  );
}

export function beijingMonth() {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit',
  }).formatToParts();
  return parts.find((part) => part.type === 'year').value + '-' + parts.find((part) => part.type === 'month').value;
}

export function beijingTime(value) {
  return /^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?$/.test(value) ? value + '+08:00' : '';
}

export function yuan(value) {
  return value == null ? '—' : '¥' + value;
}
