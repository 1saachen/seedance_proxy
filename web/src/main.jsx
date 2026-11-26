import { useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { customerApi, requestErrorMessage } from './api.js';
import { Wordmark } from './wordmark.jsx';
import { Workspace } from './workspace.jsx';

function AuthFrame({ children }) {
  return (
    <div className="auth-page">
      <header className="public-header page-width">
        <Wordmark />
        <a href="/docs/">开发者文档</a>
      </header>
      <main className="auth-main">
        {children}
        <aside className="auth-access" aria-labelledby="auth-access-title">
          <h2 id="auth-access-title">接入 Seedance API</h2>
          <p>登录后，在自己的项目里创建 API Key、测试视频生成和查看用量。</p>
          <div className="auth-endpoint">
            <span>模型发现</span>
            <code><span>GET</span> /v1/models</code>
          </div>
          <a href="/docs/#quickstart">查看接入说明 <span aria-hidden="true">↗</span></a>
        </aside>
      </main>
    </div>
  );
}

function AuthForm({ onAuthenticated, notice }) {
  const registering = new URLSearchParams(window.location.search).get('mode') === 'register';
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setError('');
    setBusy(true);
    try {
      const payload = { email: form.get('email').trim(), password: form.get('password') };
      onAuthenticated(await (registering ? customerApi.register(payload) : customerApi.login(payload)));
    } catch (problem) {
      if (problem.status === 401) setError('邮箱或密码不正确。');
      else if (registering && problem.status === 403) setError('当前未开放自助注册。');
      else if (registering && problem.status === 409) setError('该邮箱无法注册，请尝试登录。');
      else setError(requestErrorMessage(problem));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AuthFrame>
      <section className="auth-card" aria-labelledby="auth-title">
        <h1 id="auth-title">{registering ? '创建客户账号' : '登录客户工作台'}</h1>
        <p>
          {registering ? '使用邮箱创建账号后，即可访问客户工作台。' : '使用你的项目账号继续。'}
        </p>
        {notice && (
          <p className="notice" role="status">
            {notice}
          </p>
        )}
        <form className="auth-form" onSubmit={submit}>
          <label>
            邮箱
            <input name="email" type="email" required autoComplete="username" maxLength={254} />
          </label>
          <label>
            密码
            <input
              name="password"
              type="password"
              required
              autoComplete={registering ? 'new-password' : 'current-password'}
              minLength={registering ? 8 : undefined}
              maxLength={128}
              aria-describedby={registering ? 'password-hint' : undefined}
            />
          </label>
          {registering && <small id="password-hint">使用至少 8 个字符。</small>}
          {error && (
            <p className="message error" role="alert">
              {error}
            </p>
          )}
          <button className="button primary" disabled={busy}>
            {busy ? (registering ? '正在创建…' : '正在登录…') : registering ? '创建账号' : '登录'}
          </button>
        </form>
        <a className="auth-switch" href={registering ? '/console/' : '/console/?mode=register'}>
          {registering ? '已有账号？登录' : '还没有账号？注册'}
        </a>
      </section>
    </AuthFrame>
  );
}

function App() {
  const [session, setSession] = useState(undefined);
  const [notice, setNotice] = useState('');
  const [connectionError, setConnectionError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    customerApi.me(controller.signal).then(setSession, (error) => {
      if (controller.signal.aborted) return;
      if (error.status === 401) setSession(null);
      else setConnectionError(requestErrorMessage(error));
    });
    return () => controller.abort();
  }, [attempt]);

  function onAuthenticated(nextSession) {
    window.history.replaceState(null, '', `/console/${window.location.hash}`);
    setNotice('');
    setSession(nextSession);
  }

  const onSessionEnded = useCallback((expired) => {
    setNotice(expired ? '会话已过期，请重新登录。' : '你已退出登录。');
    setSession(null);
  }, []);

  if (connectionError) {
    return (
      <AuthFrame>
        <section className="auth-card">
          <h1>工作台暂时不可用</h1>
          <p className="message error" role="alert">
            {connectionError}
          </p>
          <button
            className="button secondary"
            onClick={() => {
              setConnectionError('');
              setAttempt((value) => value + 1);
            }}
          >
            重新连接
          </button>
        </section>
      </AuthFrame>
    );
  }
  if (session === undefined)
    return (
      <AuthFrame><p className="auth-card auth-loading" role="status">正在验证会话…</p></AuthFrame>
    );
  if (!session) return <AuthForm onAuthenticated={onAuthenticated} notice={notice} />;
  return <Workspace key={session.customer.id} session={session} onSessionEnded={onSessionEnded} />;
}

createRoot(document.getElementById('root')).render(<App />);
