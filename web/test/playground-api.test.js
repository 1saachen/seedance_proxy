import assert from 'node:assert/strict';
import test from 'node:test';
import { createVideoRun, getVideoTask, httpsVideoUrl, listVideoModels, submitVideoRun } from '../src/playground-api.js';

const key = 'fixture-key';
const taskPath = '/api/v3/contents/generations/tasks';
const taskId = `vid_${'d'.repeat(32)}`;
const groupId = `grp_${'a'.repeat(32)}`;
const uploadId = (number) => `upl_${number.toString().repeat(32)}`;
const assetId = (number) => `ast_${number.toString().repeat(32)}`;
const model = 'doubao-seedance-2.5';
const prompt = 'Fixture prompt';
const makeRun = (media = []) => createVideoRun({ model, prompt, duration: 4, resolution: '480p', media });
const json = (value, status = 200) => Response.json(value, { status });

test('reports the RPM wait without replaying a blocked write or repeating completed upload checks', async (t) => {
  const signal = new AbortController().signal;
  const run = makeRun([{ kind: 'Image', file: new File(['fixture'], 'reference.png') }]);
  const groupPath = '/api/asset-group/create';
  const uploadPath = '/api/asset/upload-file';
  const registerPath = '/api/asset/create';
  const paths = [];
  const blocked = () => Response.json({ detail: { code: 'key_rpm_exceeded' } }, {
    status: 429, headers: { 'Retry-After': '42' },
  });
  const expected = [
    [groupPath, blocked],
    [groupPath, () => json({ group: { id: groupId } }, 201)],
    [uploadPath, () => json({ uploadId: uploadId(1) }, 201)],
    [registerPath, blocked],
    [registerPath, () => json({ asset: { id: assetId(1) } }, 201)],
    [`/api/asset/get?assetId=${assetId(1)}`, () => json({
      asset: { id: assetId(1), status: 'Active', reference: `asset://${assetId(1)}` },
    })],
    [taskPath, () => json({ id: taskId })],
  ];
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    const [expectedPath, response] = expected[paths.length] ?? [];
    assert.equal(path, expectedPath);
    if (path === taskPath) assert.equal(options.headers['Idempotency-Key'], run.requestId);
    paths.push(path);
    return response();
  });
  const submit = () => submitVideoRun(key, run, signal, () => {});
  const rateLimited = (error) => error.code === 'key_rpm_exceeded' && error.retryAfter === 42;
  await assert.rejects(submit, rateLimited);
  assert.deepEqual(paths, [groupPath]);
  await assert.rejects(submit, rateLimited);
  assert.deepEqual(paths, [groupPath, groupPath, uploadPath, registerPath]);
  assert.equal(await submit(), taskId);
  assert.deepEqual(paths, expected.map(([path]) => path));
});

test('discovers live Seedance aliases and references staged images, videos and audio in the video request', async (t) => {
  const controller = new AbortController();
  const steps = [];
  const media = [
    { kind: 'Image', file: new File(['fixture'], 'image.png', { type: 'image/png' }) },
    { kind: 'Video', file: new File(['fixture'], 'video.mp4', { type: 'video/mp4' }) },
    { kind: 'Audio', file: new File(['fixture'], 'audio.mp3', { type: 'audio/mpeg' }) },
  ];
  let uploaded = 0;
  const fetch = t.mock.method(globalThis, 'fetch', async (path, options) => {
    assert.equal(options.credentials, 'omit');
    assert.equal(options.cache, 'no-store');
    assert.equal(options.redirect, 'error');
    assert.equal(options.headers.Authorization, `Bearer ${key}`);
    assert.equal(options.headers['X-CSRF-Token'], undefined);
    if (path === '/v1/models') return json({ data: [{ id: model }, { id: 'unrelated' }, { id: model }] });
    if (path === '/api/asset-group/create') return json({ group: { id: groupId } }, 201);
    if (path === '/api/asset/upload-file') {
      assert.equal(options.headers['Content-Type'], undefined);
      assert.equal(options.body.get('assetType'), media[uploaded].kind);
      assert.equal(options.body.get('file').name, media[uploaded].file.name);
      return json({ uploadId: uploadId(++uploaded) }, 201);
    }
    if (path === '/api/asset/create') {
      assert.deepEqual(JSON.parse(options.body), {
        groupId,
        uploadId: uploadId(uploaded),
        assetType: media[uploaded - 1].kind,
        name: `视频参考 ${uploaded}`,
      });
      return json({ asset: { id: assetId(uploaded) } }, 201);
    }
    if (path === `/api/asset/get?assetId=${assetId(uploaded)}`) {
      return json({ asset: { id: assetId(uploaded), status: 'Active', reference: `asset://${assetId(uploaded)}` } });
    }
    if (path === taskPath) {
      assert.deepEqual(JSON.parse(options.body), {
        model,
        content: [
          { type: 'text', text: prompt },
          { type: 'image_url', image_url: { url: `asset://${assetId(1)}` }, role: 'reference_image' },
          { type: 'video_url', video_url: { url: `asset://${assetId(2)}` }, role: 'reference_video' },
          { type: 'audio_url', audio_url: { url: `asset://${assetId(3)}` }, role: 'reference_audio' },
        ],
        duration: 4,
        resolution: '480p',
      });
      return json({ id: taskId });
    }
    assert.equal(path, `${taskPath}/${taskId}`);
    assert.equal(options.method, 'GET');
    return json({ id: taskId, status: 'succeeded', usage: { completion_tokens: 0 } });
  });

  assert.deepEqual(await listVideoModels(key, controller.signal), [{
    id: model, maxDuration: 30, resolutions: ['480p', '720p', '1080p'],
    audioOnly: true, referenceLimits: { Image: 9, Video: 3, Audio: 3 },
  }]);
  const run = makeRun(media);
  assert.equal(await submitVideoRun(key, run, controller.signal, (...args) => steps.push(args)), taskId);
  assert.equal((await getVideoTask(key, taskId, controller.signal)).usage.completion_tokens, 0);
  assert.deepEqual(steps.map(([step]) => step), [
    'group', 'upload', 'register', 'asset', 'upload', 'register', 'asset', 'upload', 'register', 'asset', 'submit',
  ]);
  const submittedKeys = fetch.mock.calls.map(({ arguments: [, opts] }) => opts.headers['Idempotency-Key']).filter(Boolean);
  assert.equal(new Set(submittedKeys).size, 4);
  assert.equal(submittedKeys.at(-1), run.requestId);
});

test('discovers capabilities for the three additional video models and ignores models outside the catalog', async (t) => {
  const capabilities20 = {
    max_duration: 15, resolutions: ['480p', '720p'], audio_only: false,
    max_reference_images: 9, max_reference_videos: 3, max_reference_audios: 3,
  };
  const capabilitiesFlash = {
    ...capabilities20, max_duration: 30, audio_only: true, frame_ratio_adaptive: true,
    max_reference_images: 10, max_reference_videos: 5, max_reference_audios: 5,
  };
  const flash = 'doubao-seedance-2.5-flash';
  const fast = 'doubao-seedance-2.0-fast';
  const mini = 'doubao-seedance-2.0-mini';
  t.mock.method(globalThis, 'fetch', async () => json({ data: [
    { id: 'unrelated-model' },
    { id: 'doubao-seedance-unknown' },
    { id: flash, video: capabilitiesFlash },
    { id: fast, video: capabilities20 },
    { id: mini, video: capabilities20 },
    { id: mini },
  ] }));
  const baseline = {
    maxDuration: 15, resolutions: ['480p', '720p'], audioOnly: false, referenceLimits: { Image: 9, Video: 3, Audio: 3 },
  };
  assert.deepEqual(await listVideoModels(key), [
    { ...baseline, id: flash, maxDuration: 30, audioOnly: true, referenceLimits: { Image: 10, Video: 5, Audio: 5 } },
    { ...baseline, id: fast },
    { ...baseline, id: mini },
  ]);
});

test('rejects a malformed capability response for an advertised video model', async (t) => {
  const video = {
    max_duration: 15, resolutions: ['720p'], audio_only: false,
    max_reference_images: 9, max_reference_videos: 3, max_reference_audios: 3,
  };
  let payload;
  t.mock.method(globalThis, 'fetch', async () => json({ data: [{ id: model, video: payload }] }));
  for (const invalid of [null, { ...video, max_duration: '15' }, { ...video, resolutions: [] },
    { ...video, audio_only: 'false' }, { ...video, max_reference_images: -1 }]) {
    payload = invalid;
    await assert.rejects(listVideoModels(key), { code: 'invalid_response' });
  }
});

test('discovers and submits the 4K resolution using the canonical API value', async (t) => {
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    if (path === '/v1/models') return json({ data: [{ id: model, video: {
      max_duration: 30, resolutions: ['480p', '720p', '1080p', '4k'], audio_only: true,
      max_reference_images: 9, max_reference_videos: 3, max_reference_audios: 3,
    } }] });
    assert.equal(path, taskPath);
    assert.deepEqual(JSON.parse(options.body), {
      model, content: [{ type: 'text', text: prompt }], duration: 4, resolution: '4k',
    });
    return json({ id: taskId });
  });

  const [standard] = await listVideoModels(key);
  assert.deepEqual(standard.resolutions, ['480p', '720p', '1080p', '4k']);
  const run = createVideoRun({ model: standard.id, prompt, duration: 4, resolution: '4k', media: [] });
  assert.equal(await submitVideoRun(key, run, new AbortController().signal, () => {}), taskId);
});

test('resumes a registered local reference after a failed asset status read without duplicating writes', async (t) => {
  const controller = new AbortController();
  const run = makeRun([{ kind: 'Image', file: new File(['fixture'], 'image.png') }]);
  let reads = 0;
  const fetch = t.mock.method(globalThis, 'fetch', async (path) => {
    if (path === '/api/asset-group/create') return json({ group: { id: groupId } });
    if (path === '/api/asset/upload-file') return json({ uploadId: uploadId(1) });
    if (path === '/api/asset/create') return json({ asset: { id: assetId(1) } });
    if (path === `/api/asset/get?assetId=${assetId(1)}`) {
      if (++reads === 1) return json({ detail: { code: 'asset_provider_unavailable' } }, 502);
      return json({ asset: { id: assetId(1), status: 'Active', reference: `asset://${assetId(1)}` } });
    }
    assert.equal(path, taskPath);
    return json({ id: taskId });
  });
  await assert.rejects(submitVideoRun(key, run, controller.signal, () => {}), { status: 502 });
  assert.equal(run.groupId, groupId);
  assert.equal(run.media[0].uploadId, uploadId(1));
  assert.equal(run.media[0].assetId, assetId(1));
  assert.equal(await submitVideoRun(key, run, controller.signal, () => {}), taskId);
  assert.deepEqual(fetch.mock.calls.map(({ arguments: [path] }) => path), [
    '/api/asset-group/create', '/api/asset/upload-file', '/api/asset/create',
    `/api/asset/get?assetId=${assetId(1)}`, `/api/asset/get?assetId=${assetId(1)}`, taskPath,
  ]);
});

test('reconciliation after a lost submission reuses the original body and idempotency key', async (t) => {
  const controller = new AbortController();
  const run = makeRun();
  let attempts = 0;
  const fetch = t.mock.method(globalThis, 'fetch', async (path) => {
    assert.equal(path, taskPath);
    if (++attempts === 1) throw new Error('Fixture lost response');
    if (attempts === 2) return json({ detail: { code: 'invalid_api_key' } }, 401);
    return json({ id: taskId });
  });
  await assert.rejects(submitVideoRun(key, run, controller.signal, () => {}), { code: 'network' });
  assert.equal(fetch.mock.callCount(), 1);
  assert.equal(run.submissionUncertain, true);
  await assert.rejects(submitVideoRun(key, run, controller.signal, () => {}), { status: 401 });
  assert.equal(run.submissionUncertain, true);
  assert.equal(await submitVideoRun(key, run, controller.signal, () => {}), taskId);
  assert.equal(run.submissionUncertain, false);
  const submits = fetch.mock.calls.map(({ arguments: [, options] }) => options);
  for (const options of submits) {
    assert.equal(options.headers['Idempotency-Key'], run.requestId);
    assert.equal(options.body, submits[0].body);
  }
});

test('a known task ID in an uncertain response prevents later POSTs', async (t) => {
  const controller = new AbortController();
  const run = makeRun();
  const fetch = t.mock.method(globalThis, 'fetch', async () => json({
    detail: { code: 'video_submission_unknown', task_id: taskId, message: 'fixture detail' },
  }, 502));
  await assert.rejects(submitVideoRun(key, run, controller.signal, () => {}), (error) => (
    error.status === 502 && error.taskId === taskId && !error.message.includes('fixture detail')
  ));
  assert.equal(run.submissionUncertain, true);
  assert.equal(await submitVideoRun(key, run, controller.signal, () => {}), taskId);
  assert.equal(fetch.mock.callCount(), 1);
});

test('aborting while submitting keeps the request safe to reconcile', async (t) => {
  const controller = new AbortController();
  const run = makeRun();
  const fetch = t.mock.method(globalThis, 'fetch', async (path, { signal }) => {
    assert.equal(path, taskPath);
    return new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true }));
  });
  const submitted = submitVideoRun(key, run, controller.signal, () => {});
  controller.abort();
  await assert.rejects(submitted, { name: 'AbortError' });
  assert.equal(fetch.mock.callCount(), 1);
  assert.equal(run.submissionUncertain, true);
  assert.equal(run.taskId, undefined);
});

test('uses escaped local task paths, reports business errors, and only accepts safe HTTPS result URLs', async (t) => {
  const controller = new AbortController();
  const legacyId = 'vid_older-task';
  const fetch = t.mock.method(globalThis, 'fetch', async () => json({}, 404));
  await assert.rejects(getVideoTask(key, '../../customer/me', controller.signal), { code: 'invalid_response' });
  await assert.rejects(getVideoTask(key, legacyId, controller.signal), { status: 404 });
  assert.equal(fetch.mock.callCount(), 1);
  assert.equal(fetch.mock.calls[0].arguments[0], `${taskPath}/${legacyId}`);
  const url = 'https://example.com/generated.mp4?signature=fixture';
  assert.equal(httpsVideoUrl(url), url);
  for (const invalid of [null, 'https:///example.com', 'http://example.com', '//example.com',
    'javascript:alert(1)', 'https://user@example.com', 'https://@example.com', 'https://example.com/\n']) {
    assert.equal(httpsVideoUrl(invalid), null);
  }
});
