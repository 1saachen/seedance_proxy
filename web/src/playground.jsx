import { useEffect, useRef, useState } from 'react';
import { customerApi, requestErrorMessage } from './api.js';
import {
  createVideoRun, getVideoTask, httpsVideoUrl, listVideoModels, mediaKinds, submitVideoRun, waitForPoll,
} from './playground-api.js';
import { formatNumber, Status } from './workbench-format.jsx';
import './playground.css';

const keyPattern = /^vap_live_[A-Za-z0-9_-]{43}$/;
const selectableFiles = Object.values(mediaKinds).flatMap(({ extensions }) => extensions.map((ext) => `.${ext}`)).join(',');
const modelLabels = {
  'doubao-seedance-2.5': 'Seedance 2.5',
  'doubao-seedance-2.5-flash': 'Seedance 2.5 Flash',
  'doubao-seedance-2.0': 'Seedance 2.0',
  'doubao-seedance-2.0-fast': 'Seedance 2.0 Fast',
  'doubao-seedance-2.0-mini': 'Seedance 2.0 Mini',
};
const stepNames = {
  group: '正在创建素材组', upload: '正在上传参考素材', register: '正在登记参考素材',
  asset: '正在等待参考素材可用', submit: '正在提交视频任务',
};
const pendingStates = new Set(['queued', 'running', 'submitting']);
const terminalStates = new Set(['succeeded', 'failed', 'cancelled', 'canceled', 'expired', 'submission_failed']);
const stateNotes = {
  queued: '任务已进入队列。',
  running: 'Seedance 正在生成视频。',
  submitting: '服务端正在确认提交。',
  failed: 'Seedance 未能生成视频。',
  cancelled: '任务已取消。',
  canceled: '任务已取消。',
  expired: '任务已过期。',
  submission_failed: 'Seedance 未接受该请求。',
  submission_unknown: '提交结果待确认，可用原业务 Key 查询该任务。',
};

const failureMessages = {
  asset_not_active: '参考素材暂不可用。',
  asset_processing_failed: '参考素材处理失败，请更换文件。',
  asset_provider_rejected: '素材服务拒绝了该请求，请检查参考文件。',
  asset_provider_unavailable: '素材服务暂不可用，请稍后继续。',
  asset_submission_unknown: '素材服务尚未确认登记，请稍后继续核对。',
  asset_timeout: '参考素材仍在处理中，可继续查询。',
  key_concurrency_exceeded: '当前业务 Key 已达到共享并发上限，请等已有生成结束后继续。',
  key_rpm_exceeded: '当前业务 Key 请求过于频繁，请稍后继续。',
  provider_unavailable: '视频服务暂不可用，请稍后继续。',
  upload_expired: '文件暂存已过期，请重新开始并上传。',
  upload_in_progress: '文件暂存仍在处理中，请稍后继续。',
  upload_registration_pending: '素材的登记结果尚未确认，请稍后继续核对。',
  upload_registration_unavailable: '服务端暂未启用本地参考素材登记。',
  upload_storage_failed: '文件暂存尚未完成，请稍后继续核对。',
  video_daily_limit: '当前项目已达到今日提交上限，请稍后继续。',
  video_status_unavailable: '视频状态暂不可查询，请稍后刷新。',
  timeout: '请求超时，请稍后继续。',
};

function businessError(error) {
  if (error.code === 'key_rpm_exceeded' && error.retryAfter) {
    return `当前业务 Key 已达到每分钟请求上限，请在 ${error.retryAfter} 秒后继续。`;
  }
  if (Object.hasOwn(failureMessages, error.code)) return failureMessages[error.code];
  switch (error.status) {
    case 401: return '业务 Key 已失效，请检查或更换 Key。';
    case 403: return '当前业务 Key 无法执行此操作。';
    case 404: return '该记录不可访问，请核对业务 Key。';
    case 413: return '参考文件超过大小限制。';
    case 415: return '素材格式或实际内容与所选类型不符。';
    case 400:
    case 422: return '请检查生成参数和参考素材。';
    default: return '暂时无法完成此次请求，请稍后继续。';
  }
}

async function keyFingerprint(key) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(key));
  return Array.from(new Uint8Array(digest)).join(',');
}

function useVideoModels(active, videoKey) {
  const [snapshot, setSnapshot] = useState(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!active || !videoKey) {
      setSnapshot(null);
      return;
    }
    const controller = new AbortController();
    listVideoModels(videoKey, controller.signal).then(
      (models) => {
        if (!controller.signal.aborted) setSnapshot({ key: videoKey, revision, models });
      },
      (error) => {
        if (!controller.signal.aborted) setSnapshot({ key: videoKey, revision, error });
      },
    );
    return () => controller.abort();
  }, [active, videoKey, revision]);
  const current = snapshot?.key === videoKey && snapshot.revision === revision ? snapshot : null;
  return { ...current, reload: () => setRevision((value) => value + 1) };
}

function VideoKey({ active, csrfToken, onExpired, videoKey, setVideoKey, onKeysChanged, working }) {
  const [draft, setDraft] = useState('');
  const [activity, setActivity] = useState('');
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [copyAvailable, setCopyAvailable] = useState(false);
  const operation = useRef(null);
  const input = useRef(null);
  const verifiedControl = useRef(null);
  const focusNext = useRef(null);
  const createdDraft = useRef(false);

  useEffect(() => () => operation.current?.abort(), [active]);
  useEffect(() => {
    if (!active) return;
    if (videoKey && focusNext.current === 'verified') verifiedControl.current?.focus();
    if (!videoKey && focusNext.current === 'entry') input.current?.focus();
    focusNext.current = null;
  }, [active, videoKey]);
  const canCopy = copyAvailable && Boolean(videoKey);

  async function connect(create) {
    if (!active || operation.current || working) return;
    let key = draft.trim();
    if (!create && !keyPattern.test(key)) {
      setError('请输入完整的业务 API Key。');
      input.current?.focus();
      return;
    }
    const controller = new AbortController();
    let verifying = !create;
    operation.current = controller;
    setActivity(create ? 'create' : 'verify');
    setError('');
    setNotice('');
    try {
      if (create) {
        const result = await customerApi.createKey('视频体验', csrfToken, controller.signal);
        onKeysChanged();
        controller.signal.throwIfAborted();
        key = result.secret;
        if (!keyPattern.test(key)) throw new Error('Invalid key response');
        setDraft(key);
        createdDraft.current = true;
      }
      verifying = true;
      await customerApi.verifyPlaygroundKey(key, controller.signal);
      controller.signal.throwIfAborted();
      focusNext.current = 'verified';
      setCopyAvailable(createdDraft.current);
      createdDraft.current = false;
      setVideoKey(key);
      setDraft('');
    } catch (caught) {
      if (controller.signal.aborted) return;
      if (caught.status === 401) onExpired(true);
      else if (caught.status === 403 && verifying) setError('请使用当前项目有效的业务 Key。');
      else setError(requestErrorMessage(caught));
    } finally {
      if (operation.current === controller) {
        operation.current = null;
        setActivity('');
      }
    }
  }

  async function copyKey() {
    try {
      await navigator.clipboard.writeText(videoKey);
      setNotice('新 Key 已复制。');
    } catch {
      setNotice('无法访问剪贴板，请允许剪贴板权限后重试。');
    }
  }

  return (
    <section className="panel playground-key" aria-labelledby="playground-key-heading">
      <div>
        <h2 id="playground-key-heading">业务 API Key</h2>
        <p id="playground-key-note">{videoKey
          ? canCopy ? '已验证新 Key。请在离开工作台前复制，以便之后查询任务。' : '已验证当前项目的业务 Key。'
          : '使用当前项目的 Key，或为视频体验创建一个。'}</p>
      </div>
      {videoKey ? (
        <div className="playground-key-actions">
          {canCopy && <button type="button" className="button secondary" onClick={copyKey}>复制新 Key</button>}
          <button ref={verifiedControl} type="button" className="button secondary" disabled={working} onClick={() => {
            setCopyAvailable(false);
            setNotice('');
            setError('');
            focusNext.current = 'entry';
            setVideoKey('');
          }}>更换 Key</button>
        </div>
      ) : (
        <form className="playground-key-actions" onSubmit={(event) => { event.preventDefault(); void connect(false); }}>
          <input
            ref={input} type="password" aria-labelledby="playground-key-heading" aria-describedby="playground-key-note"
            autoComplete="off" spellCheck={false} value={draft} placeholder="vap_live_…" disabled={Boolean(activity)}
            onChange={(event) => { createdDraft.current = false; setDraft(event.target.value); setError(''); }}
          />
          <button className="button primary" type="submit" disabled={!draft.trim() || Boolean(activity)}>
            {activity === 'verify' ? '正在验证…' : '验证 Key'}
          </button>
          <button className="button secondary" type="button" disabled={Boolean(activity)} onClick={() => connect(true)}>
            {activity === 'create' ? '正在创建…' : '创建体验 Key'}
          </button>
        </form>
      )}
      {error && <p className="message error playground-key-message" role="alert">{error}</p>}
      {notice && <p className="notice playground-key-message" role="status">{notice}</p>}
    </section>
  );
}

function getMediaProblem(model, media) {
  if (!model || !media.length) return '';
  for (const [kind, limit] of Object.entries(model.referenceLimits)) {
    if (media.filter((item) => item.kind === kind).length > limit) {
      return `当前模型最多支持 ${limit} 个参考${mediaKinds[kind].label}，请移除多余的文件。`;
    }
  }
  if (!model.audioOnly && media.every(({ kind }) => kind === 'Audio')) {
    return '当前模型使用参考音频时，还需要添加参考图片或视频。';
  }
  return '';
}

function chooseResolution(resolutions, preferred) {
  return [preferred, '720p'].find((item) => resolutions.includes(item)) ?? resolutions[0] ?? '';
}

function ReferenceFiles({ selected, setSelected, referenceLimits, mediaProblem }) {
  const [error, setError] = useState('');

  function addFiles(event) {
    const next = [...selected];
    let problem = '';
    for (const file of event.target.files) {
      const extension = file.name.split('.').pop().toLowerCase();
      const match = Object.entries(mediaKinds).find(([, format]) => format.extensions.includes(extension));
      if (!match) {
        problem ||= `暂不支持「${file.name}」的文件格式。`;
        continue;
      }
      const [kind, format] = match;
      const limit = referenceLimits[kind];
      if (next.filter((item) => item.kind === kind).length >= limit) {
        problem ||= `${format.label}最多可添加 ${limit} 个。`;
        continue;
      }
      if (!file.size || file.size > format.mib * 1024 * 1024) {
        problem ||= `${format.label}文件需大于 0，且最多 ${format.mib} MiB。`;
        continue;
      }
      next.push({ file, kind, id: crypto.randomUUID() });
    }
    setSelected(next);
    setError(problem);
    event.target.value = '';
  }

  return (
    <div className="playground-field">
      <label htmlFor="playground-media">参考素材 <small>可选</small></label>
      <input id="playground-media" type="file" accept={selectableFiles} multiple disabled={!referenceLimits}
        aria-describedby={referenceLimits ? 'playground-media-note' : undefined} onChange={addFiles} />
      {referenceLimits && (
        <p className="playground-hint" id="playground-media-note">
          {Object.entries(mediaKinds).map(([kind, { label, mib }]) =>
            `${label}最多 ${referenceLimits[kind]} 个，每个不超过 ${mib} MiB。`).join(' ')}
        </p>
      )}
      {error && <p className="message error" role="alert">{error}</p>}
      {mediaProblem && <p className="message error" role="alert">{mediaProblem}</p>}
      {selected.length > 0 && (
        <ul className="playground-files">
          {selected.map(({ id, file, kind }) => (
            <li key={id}>
              <span className="playground-file-type">{mediaKinds[kind].label}</span>
              <span className="playground-file-name">{file.name}</span>
              <button type="button" className="text-danger" aria-label={`移除 ${file.name}`} onClick={() => {
                setSelected(selected.filter((item) => item.id !== id));
                setError('');
              }}>移除</button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function VideoOutput({ url, taskId, outputFormat }) {
  const [playbackError, setPlaybackError] = useState(false);
  const [downloadNotice, setDownloadNotice] = useState('');
  const download = useRef(null);
  useEffect(() => () => download.current?.abort(), []);

  async function downloadVideo() {
    if (download.current) return;
    const controller = new AbortController();
    download.current = controller;
    setDownloadNotice('正在下载…');
    try {
      const response = await fetch(url, {
        credentials: 'omit', referrerPolicy: 'no-referrer', redirect: 'error', cache: 'no-store',
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(120_000)]),
      });
      if (!response.ok) throw new Error('Video download failed');
      const blob = await response.blob();
      controller.signal.throwIfAborted();
      const source = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = source;
      const isMov = outputFormat === 'mov' || blob.type === 'video/quicktime';
      link.download = `${taskId}.${isMov ? 'mov' : 'mp4'}`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(source), 1_000);
      setDownloadNotice('下载已开始。');
    } catch {
      if (!controller.signal.aborted) setDownloadNotice('请打开视频，通过播放器保存文件。链接可能已过期。');
    } finally {
      download.current = null;
    }
  }

  return (
    <>
      <video src={url} controls playsInline preload="metadata" aria-label="生成视频" onError={() => setPlaybackError(true)} />
      {playbackError && <p className="playground-preview-message" role="alert">暂时无法播放。链接可能已过期，也可能是浏览器不支持视频编码。可尝试下载到本地播放。</p>}
      <div className="playground-preview-actions row-actions">
        <button type="button" className="button primary" onClick={downloadVideo} disabled={downloadNotice === '正在下载…'}>下载视频</button>
        <a className="button secondary" href={url} target="_blank" rel="noreferrer">打开视频</a>
      </div>
      <p className="playground-preview-message" role="status">{downloadNotice || '视频链接可能过期，请及时保存。'}</p>
    </>
  );
}

function CurrentVideo({ active, job, busy, onContinue, onEnd }) {
  const url = job?.status === 'succeeded' ? httpsVideoUrl(job.task?.content?.video_url) : null;
  const inProgress = job?.step && !job.status;
  let caption = '提交任务后在此查看进度和播放生成的视频。';
  if (inProgress) caption = job.step;
  if (job?.status) caption = Object.hasOwn(stateNotes, job.status) ? stateNotes[job.status] : '可刷新查询最新任务状态。';
  if (job?.status === 'succeeded') caption = '任务成功，暂无可播放的视频链接。';

  return (
    <section className="panel playground-result" aria-labelledby="playground-result-heading">
      <div className="panel-head">
        <h2 id="playground-result-heading">生成预览</h2>
        {job?.status && <Status value={job.status} />}
      </div>
      {url && active ? <VideoOutput key={url} url={url} taskId={job.id} outputFormat={job.task?.output_format} /> : (
        <div className="playground-screen">
          <span className="playground-screen-icon" aria-hidden="true">▷</span>
          <p role="status">{caption}</p>
        </div>
      )}
      {job?.id && (
        <dl className="playground-task-facts">
          <div><dt>任务 ID</dt><dd><code>{job.id}</code></dd></div>
          {job.status === 'succeeded' && <div><dt>输出 Token</dt><dd>{formatNumber(job.task?.usage?.completion_tokens)}</dd></div>}
        </dl>
      )}
      {job?.message && <p className={`message ${job.error ? 'error' : 'notice'} playground-result-message`} role={job.error ? 'alert' : 'status'}>{job.message}</p>}
      {job?.continuable && !busy && (
        <div className="playground-preview-actions row-actions">
          <button type="button" className="button primary" onClick={onContinue}>
            {job.id ? '刷新任务状态' : job.uncertain ? '核对本次提交' : '继续本次生成'}
          </button>
          <button type="button" className="button secondary" onClick={onEnd}>结束本次跟踪</button>
        </div>
      )}
    </section>
  );
}

export function Playground({ active = true, csrfToken, onExpired, videoKey, setVideoKey, onKeysChanged }) {
  const [chosenModel, setChosenModel] = useState('');
  const [prompt, setPrompt] = useState('');
  const [media, setMedia] = useState([]);
  const [duration, setDuration] = useState('4');
  const [chosenResolution, setChosenResolution] = useState('480p');
  const [job, setJob] = useState(null);
  const [busy, setBusy] = useState(false);
  const operation = useRef(null);
  const current = useRef(null);
  const catalog = useVideoModels(active, videoKey);
  const models = catalog.models ?? [];
  const model = models.find(({ id }) => id === chosenModel) ?? models[0];
  const resolutions = model?.resolutions ?? [];
  const resolution = chooseResolution(resolutions, chosenResolution);
  const seconds = Number(duration);
  const mediaProblem = getMediaProblem(model, media);
  const ready = Boolean(videoKey && model && prompt.trim() && !mediaProblem &&
    Number.isInteger(seconds) && seconds >= 4 && seconds <= model.maxDuration);
  const locked = busy || Boolean(job?.continuable);

  useEffect(() => () => operation.current?.abort(), [active]);

  async function runVideo(run) {
    if (!run || operation.current || !active) return;
    if (!videoKey) {
      setJob((previous) => ({ ...previous, error: true, message: '请先验证本次生成所用的业务 Key。' }));
      return;
    }
    const controller = new AbortController();
    const { signal } = controller;
    operation.current = controller;
    setBusy(true);
    setJob((previous) => ({ ...previous, error: false, message: '' }));
    try {
      const fingerprint = await keyFingerprint(videoKey);
      signal.throwIfAborted();
      if (run.fingerprint && run.fingerprint !== fingerprint) {
        setJob((previous) => ({ ...previous, error: true, message: '请换回本次生成所用的业务 Key。' }));
        return;
      }
      run.fingerprint = fingerprint;
      await submitVideoRun(videoKey, run, signal, (step, number, count) => {
        const name = stepNames[step];
        setJob({ step: count > 1 ? `${name}（${number}/${count}）` : name });
      });
      setJob({ id: run.taskId, status: run.lastTask?.status ?? 'queued', task: run.lastTask });
      const deadline = Date.now() + 600_000;
      while (true) {
        const task = await getVideoTask(videoKey, run.taskId, signal);
        run.lastTask = task;
        if (task.status !== 'submission_unknown' && task.status !== 'submitting') run.submissionUncertain = false;
        const update = { id: task.id, status: task.status, task };
        setJob(update);
        if (!pendingStates.has(task.status)) {
          if (!terminalStates.has(task.status)) setJob({ ...update, continuable: true, uncertain: run.submissionUncertain });
          return;
        }
        if (Date.now() >= deadline) {
          setJob({ ...update, continuable: true, message: '任务仍在处理，可继续刷新状态。' });
          return;
        }
        await waitForPoll(5_000, signal);
      }
    } catch (error) {
      const definitive = error.code === 'video_submission_failed';
      let message;
      if (signal.aborted) message = '已暂停，返回后可继续本次操作。';
      else if (error.status === 401) message = businessError(error);
      else if (run.submissionUncertain && !run.taskId) message = '提交结果待确认。核对时会沿用原请求，避免重复生成。';
      else if (run.submissionUncertain && run.taskId) message = '提交结果待确认，可查询该任务的状态。';
      else if (!definitive) message = businessError(error);
      setJob((previous) => ({
        id: run.taskId, task: run.lastTask,
        status: run.lastTask?.status ?? (definitive ? 'submission_failed' : run.submissionUncertain ? 'submission_unknown' : run.taskId ? 'queued' : null),
        step: previous?.step, message, error: !signal.aborted && Boolean(message),
        uncertain: run.submissionUncertain, continuable: !definitive,
      }));
    } finally {
      operation.current = null;
      setBusy(false);
    }
  }

  function startVideo(event) {
    event.preventDefault();
    if (!ready || locked || operation.current) return;
    const run = createVideoRun({ model: model.id, prompt: prompt.trim(), duration: seconds, resolution, media });
    current.current = run;
    setJob(null);
    void runVideo(run);
  }

  function endTracking() {
    if ((job?.uncertain || job?.id) && !window.confirm('结束跟踪后，已提交的任务会继续处理。可在「任务记录」中核对。确定结束跟踪？')) return;
    current.current = null;
    setJob(null);
  }

  return (
    <div className="playground">
      <VideoKey {...{ active, csrfToken, onExpired, videoKey, setVideoKey, onKeysChanged, working: busy }} />
      <div className="playground-layout">
        <section className="panel" aria-labelledby="playground-form-heading">
          <div className="panel-head"><h2 id="playground-form-heading">生成设置</h2></div>
          <form className="playground-form" onSubmit={startVideo}>
            <fieldset disabled={locked}>
              <div className="playground-field">
                <label htmlFor="playground-model">模型</label>
                <select id="playground-model" value={model?.id ?? ''} disabled={!models.length} onChange={(event) => {
                  const next = models.find(({ id }) => id === event.target.value);
                  if (!next) return;
                  setChosenModel(next.id);
                  if (seconds > next.maxDuration) setDuration(String(next.maxDuration));
                  setChosenResolution(chooseResolution(next.resolutions, resolution));
                }}>
                  {!models.length && <option value="">{!videoKey ? '请先验证业务 Key' : '选择模型'}</option>}
                  {models.map(({ id }) => <option value={id} key={id}>{modelLabels[id] ?? id}</option>)}
                </select>
                {videoKey && !catalog.error && !models.length && (
                  <p className="playground-hint" role="status">{catalog.models ? '当前没有可用的 Seedance 模型。' : '正在加载模型…'}</p>
                )}
                {catalog.error && (
                  <div className="playground-model-error">
                    <p role="alert">{businessError(catalog.error)}</p>
                    <button type="button" className="button secondary" onClick={catalog.reload}>重新加载模型</button>
                  </div>
                )}
              </div>
              <div className="playground-field">
                <label htmlFor="playground-prompt">提示词</label>
                <textarea id="playground-prompt" required rows={5} maxLength={4000} value={prompt}
                  placeholder="描述画面、主体动作和镜头变化" onChange={(event) => setPrompt(event.target.value)} />
              </div>
              <ReferenceFiles key={model?.id} selected={media} setSelected={setMedia}
                referenceLimits={model?.referenceLimits} mediaProblem={mediaProblem} />
              <div className="playground-field-pair">
                <div className="playground-field">
                  <label htmlFor="playground-duration">时长（秒）</label>
                  <input id="playground-duration" type="number" required min={4} max={model?.maxDuration} value={duration} disabled={!model}
                    onChange={(event) => setDuration(event.target.value)} />
                </div>
                <div className="playground-field">
                  <label htmlFor="playground-resolution">分辨率</label>
                  <select id="playground-resolution" value={resolution} disabled={!model} onChange={(event) => setChosenResolution(event.target.value)}>
                    {resolutions.map((item) => <option key={item} value={item}>{item === '4k' ? '4K' : item}</option>)}
                  </select>
                  {resolution === '4k' && <p className="playground-hint">4K 视频使用 H.265 编码。若浏览器无法预览，可以下载后用本地播放器打开。</p>}
                </div>
              </div>
              <div className="playground-submit">
                <button className="button primary" disabled={!ready} type="submit">生成视频</button>
              </div>
            </fieldset>
          </form>
        </section>
        <CurrentVideo active={active} job={job} busy={busy} onContinue={() => runVideo(current.current)} onEnd={endTracking} />
      </div>
    </div>
  );
}
