import { useCallback, useEffect, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { adminApi, adminErrorMessage } from './admin-api.js';
import { Projects } from './admin-projects.jsx';
import { Audit, Prices } from './admin-records.jsx';
import { Message, useAdminMutation } from './admin-ui.jsx';
import { Wordmark } from './wordmark.jsx';

function SignIn({ onAuthenticated, notice }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  async function submit(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const fields = new FormData(form);
    setError('');
    setBusy(true);
    try {
      const session = await adminApi.login({
        username: fields.get('username').trim(),
        password: fields.get('password'),
      });
      form.reset();
      onAuthenticated(session);
    } catch (problem) {
      setError(problem.status === 401 ? '管理员账号或密码不正确。' : adminErrorMessage(problem));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="admin-login-panel" aria-labelledby="admin-title">
      <h1 id="admin-title">管理员登录</h1>
      <Message notice={notice} />
      <form className="admin-form" onSubmit={submit}>
        <label className="admin-field">管理员账号
          <input name="username" required autoComplete="username" spellCheck={false} maxLength={64} />
        </label>
        <label className="admin-field">管理员密码
          <input name="password" type="password" required autoComplete="current-password" maxLength={128} />
        </label>
        <Message error={error} />
        <button className="button primary" disabled={busy}>{busy ? '正在登录…' : '登录'}</button>
      </form>
    </section>
  );
}

function Header({ session, onSessionEnded }) {
  const expire = useCallback(() => onSessionEnded(true), [onSessionEnded]);
  const logout = useAdminMutation(expire);

  async function signOut() {
    if (await logout.run(() => adminApi.logout(session.csrfToken)) !== undefined) {
      onSessionEnded(false);
    }
  }

  return (
    <header className="admin-header">
      <div className="admin-header-inner">
        <div className="admin-brand"><Wordmark /><span>运营管理</span></div>
        {session && (
          <div className="admin-account">
            <span title={session.username}>{session.username}</span>
            <button className="button secondary" onClick={signOut} disabled={logout.busy}>
              {logout.busy ? '正在退出…' : '退出登录'}
            </button>
          </div>
        )}
      </div>
      {logout.error && <div className="admin-header-inner"><Message error={logout.error} /></div>}
    </header>
  );
}

function Workspace({ session, onExpired }) {
  const [section, setSection] = useState('projects');
  const props = { onExpired, csrfToken: session.csrfToken };
  return (
    <div className="admin-workspace">
      <nav className="admin-navigation" aria-label="管理功能">
        {[
          ['projects', '客户项目'],
          ['prices', '模型价目'],
          ['audit', '操作审计'],
        ].map(([id, text]) => (
          <button key={id} type="button" aria-current={id === section ? 'page' : undefined}
            onClick={() => setSection(id)}>{text}</button>
        ))}
      </nav>
      <main id="admin-content" className="admin-content" tabIndex={-1}>
        {section === 'projects' && <Projects {...props} />}
        {section === 'prices' && <Prices {...props} />}
        {section === 'audit' && <Audit onExpired={onExpired} />}
      </main>
    </div>
  );
}

function App() {
  const [session, setSession] = useState(undefined);
  const [notice, setNotice] = useState('');
  const [connectionError, setConnectionError] = useState('');
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    adminApi.me(controller.signal).then((current) => {
      if (!controller.signal.aborted) setSession(current);
    }, (problem) => {
      if (controller.signal.aborted) return;
      if (problem.status === 401) setSession(null);
      else setConnectionError(adminErrorMessage(problem));
    });
    return () => controller.abort();
  }, [attempt]);

  const onSessionEnded = useCallback((expired) => {
    setNotice(expired ? '管理会话已过期，请重新登录。' : '已退出管理员登录。');
    setSession(null);
  }, []);
  const onExpired = useCallback(() => onSessionEnded(true), [onSessionEnded]);

  return (
    <>
      <a className="skip-link" href="#admin-content">跳到主要内容</a>
      <Header session={session} onSessionEnded={onSessionEnded} />
      {session ? <Workspace session={session} onExpired={onExpired} /> : (
        <main id="admin-content" className="admin-login" tabIndex={-1}>
          {connectionError ? (
            <section className="admin-login-panel">
              <h1>管理端暂时不可用</h1>
              <Message error={connectionError} />
              <button className="button secondary" onClick={() => {
                setConnectionError('');
                setAttempt((value) => value + 1);
              }}>重新连接</button>
            </section>
          ) : session === undefined ? (
            <p className="admin-state" role="status">正在验证管理会话…</p>
          ) : (
            <SignIn notice={notice} onAuthenticated={(next) => { setNotice(''); setSession(next); }} />
          )}
        </main>
      )}
    </>
  );
}

createRoot(document.getElementById('root')).render(<App />);
