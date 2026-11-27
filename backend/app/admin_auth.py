import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, SecretStr

from .accounts import DUMMY_PASSWORD_HASH, _hash_password, _verify_password


SESSION_PATH = "/api/admin"
SESSION_COOKIE = "seedance_admin_session"
SESSION_SECONDS = 8 * 60 * 60
LOGIN_WINDOW_SECONDS = 15 * 60
ADDRESS_LIMIT = 30
USERNAME_LIMIT = 12
USERNAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9._@-]{2,63}", re.ASCII)
SESSION_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)

router = APIRouter(prefix=SESSION_PATH, tags=["Administration"])


@dataclass(frozen=True)
class AdminPrincipal:
    id: str
    username: str


class AdminCredentials(BaseModel):
    username: str = Field(max_length=128)
    password: SecretStr


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS admin_accounts (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS admin_sessions (
            token_hash TEXT PRIMARY KEY,
            admin_id TEXT NOT NULL REFERENCES admin_accounts(id),
            expires_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS admin_sessions_expiry ON admin_sessions(expires_at);
        CREATE TABLE IF NOT EXISTS admin_login_windows (
            scope_hash TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            attempts INTEGER NOT NULL,
            PRIMARY KEY (scope_hash, window_start)
        );
        CREATE INDEX IF NOT EXISTS admin_login_windows_start ON admin_login_windows(window_start);
        CREATE TABLE IF NOT EXISTS admin_audit (
            id INTEGER PRIMARY KEY,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            target_type TEXT NOT NULL,
            target_id TEXT,
            details_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """)


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message}, headers=headers)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _username(raw: str) -> str:
    if not raw.isascii():
        raise ValueError("Username must use ASCII characters")
    username = raw.strip().lower()
    if not USERNAME_PATTERN.fullmatch(username):
        raise ValueError(
            "Username must be 3–64 characters, start with a letter or digit, "
            "and use only letters, digits, periods, underscores, @, or hyphens"
        )
    return username


def _password_bytes(password: SecretStr) -> bytes:
    value = password.get_secret_value()
    if not 12 <= len(value) <= 128:
        raise ValueError("Password must be 12–128 characters")
    try:
        return value.encode("utf-8")
    except UnicodeError as error:
        raise ValueError("Password contains invalid characters") from error


def _write_audit(
    connection: sqlite3.Connection,
    actor: str,
    action: str,
    target_type: str,
    target_id: str | None,
    details: dict,
) -> None:
    connection.execute(
        """
        INSERT INTO admin_audit (actor, action, target_type, target_id, details_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (actor, action, target_type, target_id, json.dumps(details, ensure_ascii=False), _timestamp()),
    )


def record_audit(
    connection: sqlite3.Connection,
    admin: AdminPrincipal,
    action: str,
    target_type: str,
    target_id: str | None,
    details: dict,
) -> None:
    """Record explicitly selected, non-secret fields in the caller's transaction."""
    _write_audit(connection, admin.username, action, target_type, target_id, details)


def create_admin(connection: sqlite3.Connection, raw_username: str, password: SecretStr) -> AdminPrincipal:
    username = _username(raw_username)
    password_hash = _hash_password(_password_bytes(password))
    admin = AdminPrincipal(id=f"adm_{uuid4().hex}", username=username)
    created = connection.execute(
        """
        INSERT INTO admin_accounts (id, username, password_hash, created_at)
        VALUES (?, ?, ?, ?) ON CONFLICT (username) DO NOTHING
        """,
        (admin.id, admin.username, password_hash, _timestamp()),
    )
    if not created.rowcount:
        raise ValueError("Administrator username already exists")
    _write_audit(connection, "Local CLI", "admin.created", "admin", admin.id, {"username": admin.username})
    return admin


def _consume_login_attempt(request: Request, username: str | None) -> int | None:
    now = int(time.time())
    window_start = now - now % LOGIN_WINDOW_SECONDS
    address = request.client.host if request.client else "unknown"
    scopes = [(f"address:{address}", ADDRESS_LIMIT)]
    if username is not None:
        scopes.append((f"username:{username}", USERNAME_LIMIT))

    with request.app.state.db.connect() as connection:
        connection.execute("DELETE FROM admin_login_windows WHERE window_start < ?", (window_start,))
        for scope, limit in scopes:
            attempts = connection.execute(
                """
                INSERT INTO admin_login_windows (scope_hash, window_start, attempts)
                VALUES (?, ?, 1)
                ON CONFLICT (scope_hash, window_start) DO UPDATE SET attempts = attempts + 1
                WHERE attempts < ?
                RETURNING attempts
                """,
                (_digest(scope), window_start, limit + 1),
            ).fetchone()
            if attempts is not None and attempts["attempts"] <= limit:
                continue
            if attempts is not None:
                _write_audit(connection, "Anonymous", "auth.login_throttled", "admin", None, {})
            return window_start + LOGIN_WINDOW_SECONDS - now
    return None


def _session_token(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE, "")
    return token if SESSION_PATTERN.fullmatch(token) else ""


def _csrf_token(token: str) -> str:
    return hmac.new(token.encode("ascii"), b"admin-request-csrf-v1", "sha256").hexdigest()


def _session_payload(admin: AdminPrincipal, token: str) -> dict[str, str]:
    return {"username": admin.username, "csrfToken": _csrf_token(token)}


def require_admin(request: Request) -> AdminPrincipal:
    token = _session_token(request)
    if not token:
        raise _error(401, "admin_login_required", "请先登录管理端")
    with request.app.state.db.connect() as connection:
        account = connection.execute(
            """
            SELECT a.id, a.username
            FROM admin_sessions s JOIN admin_accounts a ON a.id = s.admin_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (_digest(token), int(time.time())),
        ).fetchone()
    if account is None:
        raise _error(401, "admin_login_required", "请先登录管理端")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        supplied = request.headers.get("X-CSRF-Token", "")
        if not supplied.isascii() or not hmac.compare_digest(supplied, _csrf_token(token)):
            raise _error(403, "invalid_csrf_token", "会话校验失败，请刷新后重试")
    return AdminPrincipal(id=account["id"], username=account["username"])


CurrentAdmin = Annotated[AdminPrincipal, Depends(require_admin)]


def _require_login_json(request: Request) -> None:
    # Require a non-simple content type so a different origin cannot initiate a browser login.
    content_type = request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise _error(415, "admin_json_required", "登录请求须使用 JSON")


@router.post("/login", dependencies=[Depends(_require_login_json)])
def login(credentials: AdminCredentials, request: Request, response: Response) -> dict[str, str]:
    try:
        username = _username(credentials.username)
    except ValueError:
        username = None
    retry_after = _consume_login_attempt(request, username)
    if retry_after is not None:
        raise _error(429, "auth_rate_limited", "请求过于频繁，请稍后再试", {"Retry-After": str(retry_after)})

    try:
        password = _password_bytes(credentials.password)
    except ValueError:
        password = None
    with request.app.state.db.connect() as connection:
        account = connection.execute(
            "SELECT id, username, password_hash FROM admin_accounts WHERE username = ?", (username,),
        ).fetchone()
    if password is not None:
        password_valid = _verify_password(password, account["password_hash"] if account else DUMMY_PASSWORD_HASH)
    else:
        password_valid = False
    if account is None or not password_valid:
        with request.app.state.db.connect() as connection:
            _write_audit(connection, "Anonymous", "auth.login_failed", "admin", account["id"] if account else None, {})
        raise _error(401, "invalid_credentials", "用户名或密码不正确")

    admin = AdminPrincipal(id=account["id"], username=account["username"])
    token = secrets.token_urlsafe(32)
    previous = _session_token(request)
    now = int(time.time())
    with request.app.state.db.connect() as connection:
        connection.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now,))
        if previous:
            connection.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (_digest(previous),))
        connection.execute(
            "INSERT INTO admin_sessions (token_hash, admin_id, expires_at) VALUES (?, ?, ?)",
            (_digest(token), admin.id, now + SESSION_SECONDS),
        )
        record_audit(connection, admin, "auth.login", "admin", admin.id, {})
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_SECONDS, path=SESSION_PATH,
        secure=request.app.state.settings.cookie_secure, httponly=True, samesite="strict",
    )
    return _session_payload(admin, token)


@router.get("/me")
def current_admin(request: Request, admin: CurrentAdmin) -> dict[str, str]:
    return _session_payload(admin, _session_token(request))


@router.post("/logout", status_code=204)
def logout(request: Request, admin: CurrentAdmin) -> Response:
    with request.app.state.db.connect() as connection:
        connection.execute("DELETE FROM admin_sessions WHERE token_hash = ?", (_digest(_session_token(request)),))
        record_audit(connection, admin, "auth.logout", "admin", admin.id, {})
    response = Response(status_code=204)
    response.delete_cookie(
        SESSION_COOKIE, path=SESSION_PATH, secure=request.app.state.settings.cookie_secure,
        httponly=True, samesite="strict",
    )
    return response


@router.get("/audit")
def audit_log(
    request: Request,
    _: CurrentAdmin,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 50,
) -> dict:
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN")
        total = connection.execute("SELECT COUNT(*) FROM admin_audit").fetchone()[0]
        rows = connection.execute(
            """
            SELECT id, actor, action, target_type, target_id, details_json, created_at
            FROM admin_audit ORDER BY id DESC LIMIT ? OFFSET ?
            """,
            (page_size, (page - 1) * page_size),
        ).fetchall()
    items = [
        {
            "id": row["id"], "actor": row["actor"], "action": row["action"],
            "targetType": row["target_type"], "targetId": row["target_id"],
            "details": json.loads(row["details_json"]), "createdAt": row["created_at"],
        }
        for row in rows
    ]
    return {"items": items, "total": total, "page": page, "pageSize": page_size}
