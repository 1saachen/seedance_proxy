const tasksPath = '/api/v3/contents/generations/tasks';
const taskIdPattern = /^vid_[A-Za-z0-9_-]{1,128}$/;
const groupIdPattern = /^grp_[0-9a-f]{32}$/;
const uploadIdPattern = /^upl_[0-9a-f]{32}$/;
const assetIdPattern = /^ast_[0-9a-f]{32}$/;

export const mediaKinds = {
  Image: { label: '图片', extensions: ['jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp', 'tif', 'tiff'], limit: 9, mib: 30 },
  Video: { label: '视频', extensions: ['mp4', 'mov'], limit: 3, mib: 200 },
  Audio: { label: '音频', extensions: ['wav', 'mp3'], limit: 3, mib: 15 },
};

export function httpsVideoUrl(value) {
  if (typeof value !== 'string' || !/^https:\/\/[^/@?#]+(?:[/?#]|$)/i.test(value) || /[\s\\]/.test(value)) return null;
  try {
    const url = new URL(value);
    return url.hostname && !url.username && !url.password && !url.hash ? value : null;
  } catch {
    return null;
  }
}

class VideoApiError extends Error {
  constructor(code, status, detail) {
    super('Video API request failed');
    this.code = code;
    this.status = status;
    this.taskId = matchesId(detail?.task_id, taskIdPattern) ? detail.task_id : null;
    this.uploadId = matchesId(detail?.uploadId, uploadIdPattern) ? detail.uploadId : null;
  }
}

async function request(key, path, { body, form, idempotencyKey, signal } = {}) {
  const headers = { Accept: 'application/json', Authorization: `Bearer ${key}` };
  if (body) headers['Content-Type'] = 'application/json';
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
  const timer = AbortSignal.timeout(form ? 180_000 : 45_000);
  const requestSignal = signal ? AbortSignal.any([signal, timer]) : timer;
  let response;
  let payload;
  try {
    response = await fetch(path, {
      method: body || form ? 'POST' : 'GET',
      headers,
      credentials: 'omit',
      redirect: 'error',
      cache: 'no-store',
      body: body ? JSON.stringify(body) : form,
      signal: requestSignal,
    });
    payload = await response.json().catch(() => null);
  } catch {
    signal?.throwIfAborted();
    throw new VideoApiError(timer.aborted ? 'timeout' : 'network');
  }
  signal?.throwIfAborted();
  if (timer.aborted) throw new VideoApiError('timeout');
  if (!response.ok) {
    const detail = payload?.detail;
    const code = typeof detail?.code === 'string' ? detail.code : 'request_rejected';
    throw new VideoApiError(code, response.status, detail);
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new VideoApiError('invalid_response');
  return payload;
}

function matchesId(value, pattern) {
  return typeof value === 'string' && value === value.trim() && pattern.test(value);
}

function readId(value, pattern) {
  if (!matchesId(value, pattern)) throw new VideoApiError('invalid_response');
  return value;
}

export async function listVideoModels(key, signal) {
  const result = await request(key, '/v1/models', { signal });
  if (!Array.isArray(result.data)) throw new VideoApiError('invalid_response');
  return Array.from(new Set(result.data.map((item) => item?.id).filter(
    (id) => typeof id === 'string' && /^doubao-seedance-[A-Za-z0-9.-]{1,62}$/.test(id),
  )));
}

export async function getVideoTask(key, id, signal) {
  readId(id, taskIdPattern);
  const result = await request(key, `${tasksPath}/${encodeURIComponent(id)}`, { signal });
  if (result.id !== id || typeof result.status !== 'string') throw new VideoApiError('invalid_response');
  return result;
}

export function createVideoRun({ model, prompt, duration, resolution, media }) {
  const requestId = crypto.randomUUID();
  return {
    model, prompt, duration, resolution, requestId,
    groupName: `视频体验 ${requestId}`,
    media: media.map(({ file, kind }) => ({ file, kind, uploadKey: crypto.randomUUID() })),
  };
}

export function waitForPoll(interval, signal) {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    function onAbort() {
      clearTimeout(timer);
      reject(signal.reason);
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort);
      resolve();
    }, interval);
    signal.addEventListener('abort', onAbort, { once: true });
  });
}

async function getOrCreateGroup(key, run, signal) {
  // Group creation has no idempotency key. Check the unique local name before replaying a lost response.
  if (run.groupUnconfirmed) {
    const { groups } = await request(key, '/api/asset-group/list?pageNumber=1&pageSize=100', { signal });
    if (!Array.isArray(groups)) throw new VideoApiError('invalid_response');
    const match = groups.find((group) => group?.name === run.groupName);
    if (match) return readId(match.id, groupIdPattern);
  }
  run.groupUnconfirmed = true;
  const result = await request(key, '/api/asset-group/create', { body: { name: run.groupName }, signal });
  return readId(result.group?.id, groupIdPattern);
}

async function stageUpload(key, item, signal) {
  if (item.uploadId) {
    const existing = await request(key, `/api/asset/upload-file/${item.uploadId}`, { signal });
    if (existing.uploadId !== item.uploadId) throw new VideoApiError('invalid_response');
    if (existing.status === 'staged') return;
    if (existing.status !== 'failed') {
      throw new VideoApiError(existing.status === 'expired' ? 'upload_expired' : 'upload_in_progress');
    }
  }
  const form = new FormData();
  form.append('assetType', item.kind);
  form.append('file', item.file, item.file.name);
  try {
    const result = await request(key, '/api/asset/upload-file', { form, idempotencyKey: item.uploadKey, signal });
    item.uploadId = readId(result.uploadId, uploadIdPattern);
  } catch (error) {
    if (error.uploadId) item.uploadId = error.uploadId;
    throw error;
  }
}

async function waitForAsset(key, assetId, signal) {
  const end = Date.now() + 120_000;
  while (Date.now() < end) {
    const { asset } = await request(key, `/api/asset/get?${new URLSearchParams({ assetId })}`, { signal });
    if (asset?.id !== assetId) throw new VideoApiError('invalid_response');
    if (asset.status === 'Active' && asset.reference === `asset://${assetId}`) return asset.reference;
    if (asset.status === 'Failed') throw new VideoApiError('asset_processing_failed');
    if (asset.status !== 'Processing') throw new VideoApiError('invalid_response');
    await waitForPoll(2_000, signal);
  }
  throw new VideoApiError('asset_timeout');
}

function referenceContent(item) {
  const kind = item.kind.toLowerCase();
  const field = `${kind}_url`;
  return { type: field, [field]: { url: item.reference }, role: `reference_${kind}` };
}

export async function submitVideoRun(key, run, signal, onStep) {
  if (run.taskId) return run.taskId;
  signal.throwIfAborted();
  if (run.media.length && !run.groupId) {
    onStep('group');
    run.groupId = await getOrCreateGroup(key, run, signal);
  }
  for (const [index, item] of run.media.entries()) {
    if (item.reference) continue;
    if (!item.assetId) {
      onStep('upload', index + 1, run.media.length);
      await stageUpload(key, item, signal);
      onStep('register', index + 1, run.media.length);
      const { asset } = await request(key, '/api/asset/create', {
        body: { groupId: run.groupId, uploadId: item.uploadId, assetType: item.kind, name: `视频参考 ${index + 1}` },
        signal,
      });
      item.assetId = readId(asset?.id, assetIdPattern);
    }
    onStep('asset', index + 1, run.media.length);
    item.reference = await waitForAsset(key, item.assetId, signal);
  }
  const body = {
    model: run.model,
    content: [{ type: 'text', text: run.prompt }, ...run.media.map(referenceContent)],
    duration: run.duration,
    resolution: run.resolution,
  };
  onStep('submit');
  signal.throwIfAborted();
  const wasUncertain = run.submissionUncertain;
  run.submissionUncertain = true;
  try {
    const result = await request(key, tasksPath, { body, idempotencyKey: run.requestId, signal });
    run.taskId = readId(result.id, taskIdPattern);
    run.submissionUncertain = false;
    return run.taskId;
  } catch (error) {
    if (error.taskId) run.taskId = error.taskId;
    if (error.code === 'video_submission_failed' || (!wasUncertain && !run.taskId &&
      (error.code === 'provider_unavailable' || [400, 401, 403, 404, 422, 429].includes(error.status)))) {
      run.submissionUncertain = false;
    }
    throw error;
  }
}
