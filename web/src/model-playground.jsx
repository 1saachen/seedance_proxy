import { useEffect, useRef, useState } from 'react';
import { customerApi, requestErrorMessage } from './api.js';
import { httpsImageUrl, modelApi } from './model-api.js';
import './model-playground.css';

const keyPattern = /^vap_live_[A-Za-z0-9_-]{43}$/;

function displayError(error) {
  if (error.status === 401 || error.status === 403) return '请使用当前项目中有效的业务 Key。';
  if (error.status === 409) return '图片请求尚待确认。请稍后用相同参数重试。';
  if (error.status === 503) return '当前模型暂时不可用。';
  return requestErrorMessage(error);
}

function Answer({ output, busy }) {
  if (busy) return <p className="empty" role="status">正在生成结果…</p>;
  if (!output) return <p className="empty">生成的文本或图片会显示在这里。</p>;
  if (output.type === 'text') return <p className="model-answer" aria-live="polite">{output.content}</p>;
  return output.urls.map((url, index) => (
    <figure className="model-image" key={index}>
      <img src={url} alt={'生成图片 ' + (index + 1)} referrerPolicy="no-referrer" />
      <figcaption><a href={url} target="_blank" rel="noopener noreferrer">在新窗口打开图片</a></figcaption>
    </figure>
  ));
}

export function ModelPlayground({ onExpired }) {
  const [inputKey, setInputKey] = useState('');
  const [activeKey, setActiveKey] = useState('');
  const [models, setModels] = useState([]);
  const [mode, setMode] = useState('text');
  const [selectedModel, setSelectedModel] = useState('');
  const [prompt, setPrompt] = useState('');
  const [imageUrl, setImageUrl] = useState('');
  const [result, setResult] = useState(null);
  const [pending, setPending] = useState(null);
  const [error, setError] = useState('');
  const operation = useRef(null);
  const retry = useRef(null);

  const eligibleModels = models.filter(({ type }) => type === mode);
  const model = eligibleModels.find(({ id }) => id === selectedModel)?.id ?? eligibleModels[0]?.id ?? '';
  const imageMode = mode === 'image';
  const maxPromptLength = imageMode ? 8000 : 32768;

  useEffect(() => () => operation.current?.abort(), []);

  async function action(kind, callback) {
    if (operation.current) return;
    const controller = new AbortController();
    operation.current = controller;
    setError('');
    setPending(kind);
    try {
      await callback(controller.signal);
    } catch (error) {
      if (!controller.signal.aborted) setError(displayError(error));
    } finally {
      operation.current = null;
      if (!controller.signal.aborted) setPending(null);
    }
  }

  function connect(event) {
    event.preventDefault();
    const key = inputKey.trim();
    if (!keyPattern.test(key)) {
      setError('请填写工作台生成的业务 Key。');
      return;
    }
    action('connect', async (signal) => {
      try {
        await customerApi.verifyPlaygroundKey(key, signal);
      } catch (error) {
        if (error.status === 401 && !signal.aborted) onExpired(true);
        throw error;
      }
      const response = await modelApi.list(key, signal);
      const publicModels = response.data.filter((item) => item && typeof item.id === 'string' && ['text', 'image'].includes(item.type));
      setModels(publicModels);
      setActiveKey(key);
      setInputKey('');
    });
  }

  function generate(event) {
    event.preventDefault();
    if (!activeKey || !model || !prompt.trim()) return;
    if (prompt.length > maxPromptLength) {
      setError('请将提示词缩短到 ' + maxPromptLength + ' 个字符以内。');
      return;
    }
    const reference = imageMode && imageUrl.trim() ? httpsImageUrl(imageUrl.trim()) : null;
    if (imageMode && imageUrl.trim() && !reference) {
      setError('请输入公开的 HTTPS 参考图地址。');
      return;
    }
    action('generate', async (signal) => {
      setResult(null);
      if (imageMode) {
        const body = { model, prompt: prompt.trim(), ...(reference ? { image: reference } : {}) };
        const fingerprint = JSON.stringify(body);
        if (retry.current?.fingerprint !== fingerprint) retry.current = { fingerprint, id: crypto.randomUUID() };
        const response = await modelApi.image(activeKey, body, retry.current.id, signal);
        const urls = response.data.map((item) => httpsImageUrl(item?.url)).filter(Boolean);
        if (!urls.length) throw new Error('No image URLs');
        retry.current = null;
        setResult({ type: mode, urls });
      } else {
        const response = await modelApi.chat(activeKey, { model, messages: [{ role: 'user', content: prompt.trim() }] }, signal);
        const content = response.choices?.[0]?.message?.content;
        if (typeof content !== 'string' || !content.trim()) throw new Error('No text content');
        setResult({ type: mode, content });
      }
    });
  }

  function resetKey() {
    setActiveKey('');
    setInputKey('');
    setModels([]);
    setResult(null);
    setError('');
    retry.current = null;
  }

  return (
    <div className="model-playground">
      <section className="panel" aria-labelledby="model-input-heading">
        <div className="panel-head"><h2 id="model-input-heading">发起请求</h2></div>
        <div className="model-panel-body">
          {activeKey ? (
            <>
              <div className="model-key-state">
                <span>已连接项目 API Key</span>
                <button className="button secondary" type="button" onClick={resetKey} disabled={!!pending}>更换 Key</button>
              </div>
              <form className="model-form" onSubmit={generate}>
                <fieldset disabled={!!pending}>
                  <label>
                    能力
                    <select value={mode} onChange={(event) => {
                      setMode(event.target.value);
                      setResult(null);
                      setError('');
                    }}>
                      <option value="text">文本对话</option>
                      <option value="image">图片生成</option>
                    </select>
                  </label>
                  <label>
                    模型
                    <select value={model} onChange={(event) => setSelectedModel(event.target.value)} disabled={!model}>
                      {eligibleModels.map(({ id }) => <option key={id}>{id}</option>)}
                    </select>
                  </label>
                  {!model && <p className="message" role="status">项目暂时没有这类可用模型。</p>}
                  <label>
                    提示词
                    <textarea required rows={5} maxLength={maxPromptLength} value={prompt} onChange={(event) => setPrompt(event.target.value)} />
                  </label>
                  {imageMode && (
                    <label>
                      参考图地址（选填）
                      <input type="url" pattern="https://.+" value={imageUrl} onChange={(event) => setImageUrl(event.target.value)} placeholder="https://" />
                    </label>
                  )}
                  <button className="button primary" type="submit" disabled={!model || !prompt.trim()}>
                    {pending === 'generate' ? '正在生成…' : imageMode ? '生成图片' : '生成文本'}
                  </button>
                </fieldset>
              </form>
            </>
          ) : (
            <form className="model-form" onSubmit={connect}>
              <label>
                业务 API Key
                <input required type="password" autoComplete="off" value={inputKey} onChange={(event) => setInputKey(event.target.value)} disabled={!!pending} />
              </label>
              <button className="button primary" type="submit" disabled={!!pending}>
                {pending === 'connect' ? '正在验证…' : '连接 API Key'}
              </button>
            </form>
          )}
          {error && <p className="message error" role="alert">{error}</p>}
        </div>
      </section>
      <section className="panel" aria-labelledby="model-answer-heading">
        <div className="panel-head"><h2 id="model-answer-heading">结果</h2></div>
        <div className="model-panel-body"><Answer output={result} busy={pending === 'generate'} /></div>
      </section>
    </div>
  );
}
