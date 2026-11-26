import { useEffect, useRef, useState } from 'react';
import { customerApi, requestErrorMessage } from './api.js';
import { VideoOutput } from './playground.jsx';
import { getVideoTask, httpsVideoUrl } from './playground-api.js';
import { formatDate, formatNumber, Status, statusNames } from './workbench-format.jsx';
import './task-records.css';

const emptyFilters = { taskId: '', status: '', fromDate: '', toDate: '' };
const pageSize = 25;
const taskIdPattern = /^vid_[A-Za-z0-9_-]{1,128}$/;
const taskStatuses = [
  'submitting', 'queued', 'running', 'succeeded', 'failed', 'canceled', 'expired', 'submission_failed', 'submission_unknown',
];
const errorLabels = {
  video_generation_failed: 'Seedance 未能生成视频。',
  video_submission_failed: 'Seedance 拒绝了本次提交。',
  video_submission_unknown: '尚未确认 Seedance 是否收到任务。',
};

function TaskFilters({ draft, applied, onDraft, onApply }) {
  const [error, setError] = useState('');
  const canClear = Object.values(draft).some(Boolean) || Object.values(applied).some(Boolean);

  function edit(event) {
    const { name, value } = event.currentTarget;
    onDraft({ ...draft, [name]: value });
    setError('');
  }

  function search(event) {
    event.preventDefault();
    const taskId = draft.taskId.trim();
    if (taskId && !taskIdPattern.test(taskId)) {
      setError('请输入以 vid_ 开头的完整任务 ID。');
      return;
    }
    if (draft.fromDate && draft.toDate && draft.fromDate > draft.toDate) {
      setError('结束日期必须等于或晚于开始日期。');
      return;
    }
    const filters = { ...draft, taskId };
    onDraft(filters);
    onApply(filters);
  }

  function clear() {
    setError('');
    onDraft(emptyFilters);
    onApply(emptyFilters);
  }

  return (
    <form className="task-record-filters" onSubmit={search}>
      <label>
        任务 ID
        <input
          name="taskId"
          placeholder="填写完整任务 ID"
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
          maxLength={132}
          value={draft.taskId}
          onChange={edit}
        />
      </label>
      <label>
        <span id="task-record-status-label">状态</span>
        <select name="status" aria-labelledby="task-record-status-label" value={draft.status} onChange={edit}>
          <option value="">全部状态</option>
          {taskStatuses.map((value) => <option key={value} value={value}>{statusNames[value]}</option>)}
        </select>
      </label>
      <label>
        开始日期
        <input type="date" name="fromDate" value={draft.fromDate} onChange={edit} />
      </label>
      <label>
        结束日期
        <input type="date" name="toDate" value={draft.toDate} onChange={edit} />
      </label>
      <div className="task-filter-actions">
        <button className="button primary" type="submit">筛选</button>
        <button className="button secondary" type="button" disabled={!canClear} onClick={clear}>清除</button>
      </div>
      {error && <p className="message error task-filter-error" role="alert">{error}</p>}
    </form>
  );
}

function TaskRecordTable({ items, onSelect }) {
  return (
    <div className="table-scroll task-record-table" tabIndex={0} role="region" aria-label="视频任务列表">
      <table role="table">
        <thead role="rowgroup">
          <tr role="row">
            <th role="columnheader" scope="col">任务 / 模型</th>
            <th role="columnheader" scope="col">状态</th>
            <th role="columnheader" scope="col">输出 Token</th>
            <th role="columnheader" scope="col">任务时间</th>
            <th role="columnheader" scope="col">结果</th>
          </tr>
        </thead>
        <tbody role="rowgroup">
          {items.map((task) => {
            const errorCode = task.error?.code;
            return (
              <tr key={task.id} role="row">
                <th scope="row" role="rowheader">
                  <code className="task-record-id">{task.id}</code>
                  <span className="task-record-meta">{task.model}</span>
                </th>
                <td role="cell">
                  <Status value={task.status} />
                  {Object.hasOwn(errorLabels, errorCode) && (
                    <span className="task-record-meta task-record-error">{errorLabels[errorCode]}</span>
                  )}
                </td>
                <td className="task-record-tokens" role="cell" title={task.outputTokens == null ? '未记录' : undefined}>
                  <span aria-hidden="true">输出 Token </span>{formatNumber(task.outputTokens)}
                </td>
                <td className="task-record-times" role="cell">
                  <div><span>创建</span> {formatDate(task.createdAt)}</div>
                  <div><span>结束</span> {formatDate(task.settledAt)}</div>
                </td>
                <td className="task-record-action" role="cell">
                  {task.status === 'succeeded' ? (
                    <button type="button" className="button secondary" onClick={() => onSelect(task.id)} aria-label={`查看视频 ${task.id}`}>
                      查看视频
                    </button>
                  ) : '—'}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function TaskRecordResults({ snapshot, filters, page, onPage, onRetry, onSelect }) {
  if (snapshot?.error) {
    return (
      <div className="empty">
        <p role="alert">{requestErrorMessage(snapshot.error)}</p>
        <button className="button secondary" type="button" onClick={onRetry}>重试</button>
      </div>
    );
  }
  if (!snapshot) return <p className="empty" role="status">正在加载任务…</p>;

  const { items, total, pageSize: size } = snapshot.data;
  const pages = Math.max(1, Math.ceil(total / size));
  const filtered = Object.values(filters).some(Boolean);
  if (!items.length) {
    return (
      <div className="empty">
        <p role="status">{filtered ? '没有符合筛选条件的任务。' : '当前项目还没有视频任务。'}</p>
        {page > 1 && <button className="button secondary" type="button" onClick={() => onPage(1)}>返回第一页</button>}
      </div>
    );
  }

  return (
    <>
      <TaskRecordTable items={items} onSelect={onSelect} />
      <nav className="task-record-pagination" aria-label="任务记录分页">
        <p role="status">共 {formatNumber(total)} 条，第 {page} / {pages} 页</p>
        <div className="row-actions">
          <button className="button secondary" type="button" disabled={page <= 1} onClick={() => onPage(page - 1)}>上一页</button>
          <button className="button secondary" type="button" disabled={page >= pages} onClick={() => onPage(page + 1)}>下一页</button>
        </div>
      </nav>
    </>
  );
}

function resultErrorMessage(error) {
  if (error.status === 404) return '请填写创建此任务所用的 API Key。';
  if (error.status === 401 || error.status === 403) return 'API Key 无效或已撤销。';
  return requestErrorMessage(error);
}

function TaskResultDialog({ taskId, videoKey, onClose, onExpired }) {
  const dialog = useRef(null);
  const heading = useRef(null);
  const request = useRef(null);
  const [keyInput, setKeyInput] = useState(videoKey ?? '');
  const [result, setResult] = useState(null);
  const busy = result?.state === 'loading';

  useEffect(() => {
    const element = dialog.current;
    element.showModal();
    heading.current.focus();
    return () => {
      request.current?.abort();
      element.close();
    };
  }, []);

  function close() {
    request.current?.abort();
    dialog.current.close();
    onClose();
  }

  async function loadResult(event) {
    event.preventDefault();
    const key = keyInput.trim();
    if (!key || request.current) return;
    const controller = new AbortController();
    request.current = controller;
    setResult({ state: 'loading' });

    try {
      try {
        await customerApi.verifyPlaygroundKey(key, controller.signal);
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error.status === 401) onExpired(true);
        else if (error.status === 403) setResult({ state: 'error', message: '请输入当前项目中有效的 API Key。' });
        else setResult({ state: 'error', message: requestErrorMessage(error) });
        return;
      }
      if (controller.signal.aborted) return;
      const task = await getVideoTask(key, taskId, controller.signal);
      if (controller.signal.aborted) return;
      const url = task.status === 'succeeded' ? httpsVideoUrl(task.content?.video_url) : null;
      setResult(url ? { state: 'ready', url } : { state: 'unavailable' });
    } catch (error) {
      if (!controller.signal.aborted) setResult({ state: 'error', message: resultErrorMessage(error) });
    } finally {
      request.current = null;
    }
  }

  return (
    <dialog
      ref={dialog}
      className="task-result-dialog"
      aria-labelledby="task-result-heading"
      onCancel={(event) => {
        event.preventDefault();
        close();
      }}
    >
      <div className="task-result-header">
        <div>
          <h2 id="task-result-heading" ref={heading} tabIndex={-1}>视频结果</h2>
          <code>{taskId}</code>
        </div>
        <button className="button secondary" type="button" onClick={close}>关闭</button>
      </div>
      <form onSubmit={loadResult} className="task-result-form" autoComplete="off">
        <label>
          创建任务的 API Key
          <input
            type="password"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            required
            value={keyInput}
            disabled={busy}
            onChange={(event) => {
              setKeyInput(event.currentTarget.value);
              setResult(null);
            }}
          />
        </label>
        <button className="button primary" type="submit" disabled={busy || !keyInput.trim()}>
          {busy ? '正在查询…' : '获取视频'}
        </button>
      </form>
      {busy && <p className="task-result-note" role="status">正在验证 API Key 并查询视频…</p>}
      {result?.state === 'error' && <p className="message error" role="alert">{result.message}</p>}
      {result?.state === 'unavailable' && <p className="notice" role="status">当前没有可播放的视频链接。链接可能已过期。</p>}
      {result?.state === 'ready' && (
        <div className="task-result-media">
          <VideoOutput url={result.url} taskId={taskId} />
        </div>
      )}
    </dialog>
  );
}

export function TaskRecords({ videoKey, onExpired }) {
  const [draft, setDraft] = useState(emptyFilters);
  const [query, setQuery] = useState({ filters: emptyFilters, page: 1 });
  const [snapshot, setSnapshot] = useState(null);
  const [selected, setSelected] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    customerApi.tasks({ ...query.filters, page: query.page, pageSize }, controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setSnapshot({ query, data });
      },
      (error) => {
        if (controller.signal.aborted) return;
        if (error.status === 401) onExpired(true);
        else setSnapshot({ query, error });
      },
    );
    return () => controller.abort();
  }, [query, onExpired]);

  function retry() {
    setQuery({ ...query });
  }

  return (
    <section className="panel task-records" aria-labelledby="task-records-heading">
      <div className="panel-head">
        <div>
          <h2 id="task-records-heading">项目视频任务</h2>
          <p>按创建日期筛选。日期和时间均使用北京时间。</p>
        </div>
        <button className="button secondary" type="button" onClick={retry}>刷新</button>
      </div>
      <TaskFilters draft={draft} applied={query.filters} onDraft={setDraft} onApply={(filters) => setQuery({ filters, page: 1 })} />
      <TaskRecordResults
        snapshot={snapshot?.query === query ? snapshot : null}
        filters={query.filters}
        page={query.page}
        onPage={(page) => setQuery({ ...query, page })}
        onRetry={retry}
        onSelect={setSelected}
      />
      {selected && <TaskResultDialog taskId={selected} videoKey={videoKey} onExpired={onExpired} onClose={() => setSelected('')} />}
    </section>
  );
}
