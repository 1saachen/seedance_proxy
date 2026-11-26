import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr


SESSION_COOKIE = "seedance_customer_session"
SESSION_PATH = "/api/customer"
SESSION_SECONDS = 12 * 60 * 60
KEY_PREFIX = "vap_live_"
AUTH_WINDOW_SECONDS = 15 * 60
EMAIL_PATTERN = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+", re.ASCII)
PASSWORD_SCHEME = "scrypt-v1"
DUMMY_PASSWORD_HASH = f"{PASSWORD_SCHEME}${'00' * 16}${'00' * 32}"

router = APIRouter(prefix="/api/customer", tags=["customer"])
business_bearer = HTTPBearer(auto_error=False, scheme_name="Business API Key")


@dataclass(frozen=True)
class CustomerPrincipal:
    id: str
    email: str
    project_id: str


@dataclass(frozen=True)
class BusinessPrincipal:
    project_id: str
    key_id: str


class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: SecretStr


class KeyCreate(BaseModel):
    name: str = Field(max_length=100)


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS customer_accounts (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            project_id TEXT UNIQUE NOT NULL REFERENCES projects(id),
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS customer_sessions (
            token_hash TEXT PRIMARY KEY,
            customer_id TEXT NOT NULL REFERENCES customer_accounts(id),
            expires_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS customer_sessions_expiry
            ON customer_sessions(expires_at);
        CREATE TABLE IF NOT EXISTS business_keys (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            name TEXT NOT NULL,
            prefix TEXT NOT NULL,
            secret_hash TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            revoked_at TEXT
        );
        CREATE INDEX IF NOT EXISTS business_keys_project
            ON business_keys(project_id, created_at);
        CREATE TABLE IF NOT EXISTS auth_rate_windows (
            action TEXT NOT NULL,
            scope_hash TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            attempts INTEGER NOT NULL,
            PRIMARY KEY (action, scope_hash, window_start)
        );
        CREATE INDEX IF NOT EXISTS auth_rate_windows_start
            ON auth_rate_windows(window_start);
        """
    )


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message}, headers=headers)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _email(raw_email: str) -> str:
    email = raw_email.strip().lower()
    if not email.isascii() or not email.isprintable() or not EMAIL_PATTERN.fullmatch(email):
        raise _error(422, "invalid_email", "请输入有效的邮箱地址")
    return email


def _password_bytes(raw_password: SecretStr) -> bytes:
    password = raw_password.get_secret_value()
    if not 8 <= len(password) <= 128:
        raise _error(422, "invalid_password", "密码长度须为 8 至 128 个字符")
    try:
        return password.encode("utf-8")
    except UnicodeError as error:
        raise _error(422, "invalid_password", "密码包含无效字符") from error


def _derive_password(password: bytes, salt: bytes) -> bytes:
    return hashlib.scrypt(password, salt=salt, n=32768, r=8, p=1, dklen=32, maxmem=64 * 1024**2)


def _hash_password(password: bytes) -> str:
    salt = secrets.token_bytes(16)
    return f"{PASSWORD_SCHEME}${salt.hex()}${_derive_password(password, salt).hex()}"


def _verify_password(password: bytes, stored_hash: str) -> bool:
    scheme, salt, expected = stored_hash.split("$")
    return scheme == PASSWORD_SCHEME and hmac.compare_digest(
        _derive_password(password, bytes.fromhex(salt)), bytes.fromhex(expected)
    )


def _consume_auth_attempt(request: Request, action: str, email: str | None = None) -> None:
    address = request.client.host if request.client else "unknown"
    now = int(time.time())
    window_start = now - now % AUTH_WINDOW_SECONDS
    scopes = [(address, 8)] if action == "register" else [(address, 30), (f"{address}\0{email}", 8)]
    with request.app.state.db.connect() as connection:
        connection.execute("DELETE FROM auth_rate_windows WHERE window_start < ?", (window_start,))
        for scope, limit in scopes:
            result = connection.execute(
                """
                INSERT INTO auth_rate_windows (action, scope_hash, window_start, attempts)
                VALUES (?, ?, ?, 1)
                ON CONFLICT (action, scope_hash, window_start) DO UPDATE SET attempts = attempts + 1
                WHERE attempts < ?
                """,
                (action, _digest(scope), window_start, limit),
            )
            if not result.rowcount:
                raise _error(429, "auth_rate_limited", "请求过于频繁，请稍后再试", {
                    "Retry-After": str(window_start + AUTH_WINDOW_SECONDS - now),
                })


def _csrf_token(session: str) -> str:
    # The database needs only the session digest, which cannot derive this token.
    return hmac.new(session.encode("ascii"), b"customer-request-csrf-v1", "sha256").hexdigest()


def _session_token(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE, "")
    if len(token) != 43 or not token.isascii():
        return ""
    return token


def _new_session(connection: sqlite3.Connection, customer_id: str, previous: str) -> str:
    now = int(time.time())
    connection.execute("DELETE FROM customer_sessions WHERE expires_at <= ?", (now,))
    if previous:
        connection.execute("DELETE FROM customer_sessions WHERE token_hash = ?", (_digest(previous),))
    token = secrets.token_urlsafe(32)
    connection.execute(
        "INSERT INTO customer_sessions (token_hash, customer_id, expires_at) VALUES (?, ?, ?)",
        (_digest(token), customer_id, now + SESSION_SECONDS),
    )
    return token


def _customer_payload(customer: CustomerPrincipal, token: str) -> dict:
    return {
        "customer": {"id": customer.id, "email": customer.email, "projectId": customer.project_id},
        "csrfToken": _csrf_token(token),
    }


def _session_response(request: Request, response: Response, customer: CustomerPrincipal, token: str) -> dict:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_SECONDS, path=SESSION_PATH,
        secure=request.app.state.settings.cookie_secure, httponly=True, samesite="strict",
    )
    return _customer_payload(customer, token)


def require_customer(request: Request) -> CustomerPrincipal:
    token = _session_token(request)
    if not token:
        raise _error(401, "customer_login_required", "请先登录客户账户")
    with request.app.state.db.connect() as connection:
        customer = connection.execute(
            """
            SELECT a.id, a.email, a.project_id
            FROM customer_sessions AS s JOIN customer_accounts AS a ON a.id = s.customer_id
            WHERE s.token_hash = ? AND s.expires_at > ?
            """,
            (_digest(token), int(time.time())),
        ).fetchone()
    if customer is None:
        raise _error(401, "customer_login_required", "请先登录客户账户")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        supplied = request.headers.get("X-CSRF-Token", "")
        if not supplied.isascii() or not hmac.compare_digest(supplied, _csrf_token(token)):
            raise _error(403, "invalid_csrf_token", "会话校验失败，请刷新后重试")
    return CustomerPrincipal(**dict(customer))


def _find_business_key(request: Request, credentials: HTTPAuthorizationCredentials | None) -> BusinessPrincipal | None:
    secret = credentials.credentials if credentials and credentials.scheme.lower() == "bearer" else ""
    if not (secret.startswith(KEY_PREFIX) and len(secret) == len(KEY_PREFIX) + 43 and secret.isascii()):
        return None
    with request.app.state.db.connect() as connection:
        key = connection.execute(
            "SELECT id, project_id FROM business_keys WHERE secret_hash = ? AND revoked_at IS NULL",
            (_digest(secret),),
        ).fetchone()
    return BusinessPrincipal(project_id=key["project_id"], key_id=key["id"]) if key is not None else None


def require_business_key(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(business_bearer)],
) -> BusinessPrincipal:
    principal = _find_business_key(request, credentials)
    if principal is None:
        raise _error(401, "invalid_api_key", "请提供有效的业务 API Key", {"WWW-Authenticate": "Bearer"})
    return principal


CurrentCustomer = Annotated[CustomerPrincipal, Depends(require_customer)]


@router.get("/playground/key", status_code=204)
def check_playground_key(
    request: Request,
    customer: CurrentCustomer,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(business_bearer)],
) -> Response:
    key = _find_business_key(request, credentials)
    if key is None or key.project_id != customer.project_id:
        raise _error(403, "invalid_api_key", "请提供当前项目有效的业务 API Key")
    return Response(status_code=204)


@router.post("/register", status_code=201)
def register(credentials: Credentials, request: Request, response: Response) -> dict:
    if not request.app.state.settings.registration_enabled:
        raise _error(403, "registration_disabled", "当前未开放自助注册")
    email = _email(credentials.email)
    password = _password_bytes(credentials.password)
    _consume_auth_attempt(request, "register")
    customer = CustomerPrincipal(id=f"cus_{uuid4().hex}", email=email, project_id=f"prj_{uuid4().hex}")
    password_hash = _hash_password(password)
    created_at = _now()
    with request.app.state.db.connect() as connection:
        connection.execute("INSERT INTO projects (id, created_at) VALUES (?, ?)", (customer.project_id, created_at))
        result = connection.execute(
            """
            INSERT INTO customer_accounts (id, email, project_id, password_hash, created_at)
            VALUES (?, ?, ?, ?, ?) ON CONFLICT (email) DO NOTHING
            """,
            (customer.id, customer.email, customer.project_id, password_hash, created_at),
        )
        if not result.rowcount:
            raise _error(409, "email_unavailable", "无法使用该邮箱注册")
        token = _new_session(connection, customer.id, _session_token(request))
    return _session_response(request, response, customer, token)


@router.post("/login")
def login(credentials: Credentials, request: Request, response: Response) -> dict:
    email = _email(credentials.email)
    password = _password_bytes(credentials.password)
    _consume_auth_attempt(request, "login", email)
    with request.app.state.db.connect() as connection:
        record = connection.execute(
            "SELECT id, email, project_id, password_hash FROM customer_accounts WHERE email = ?", (email,)
        ).fetchone()
    valid_password = _verify_password(password, record["password_hash"] if record else DUMMY_PASSWORD_HASH)
    if not record or not valid_password:
        raise _error(401, "invalid_credentials", "邮箱或密码不正确")
    customer = CustomerPrincipal(id=record["id"], email=record["email"], project_id=record["project_id"])
    with request.app.state.db.connect() as connection:
        token = _new_session(connection, customer.id, _session_token(request))
    return _session_response(request, response, customer, token)


@router.get("/me")
def current_customer(request: Request, customer: CurrentCustomer) -> dict:
    return _customer_payload(customer, _session_token(request))


@router.post("/logout", status_code=204)
def logout(request: Request, _: CurrentCustomer) -> Response:
    with request.app.state.db.connect() as connection:
        connection.execute("DELETE FROM customer_sessions WHERE token_hash = ?", (_digest(_session_token(request)),))
    response = Response(status_code=204)
    response.delete_cookie(
        SESSION_COOKIE, path=SESSION_PATH, secure=request.app.state.settings.cookie_secure,
        httponly=True, samesite="strict",
    )
    return response


def _public_key(row: sqlite3.Row | dict) -> dict:
    return {
        "id": row["id"], "name": row["name"], "prefix": row["prefix"], "createdAt": row["created_at"],
        "status": "revoked" if row["revoked_at"] else "active",
    }


@router.get("/keys")
def list_keys(request: Request, customer: CurrentCustomer) -> dict:
    with request.app.state.db.connect() as connection:
        keys = connection.execute(
            """
            SELECT id, name, prefix, created_at, revoked_at FROM business_keys
            WHERE project_id = ? ORDER BY created_at DESC, id DESC
            """,
            (customer.project_id,),
        ).fetchall()
    return {"keys": [_public_key(key) for key in keys]}


@router.post("/keys", status_code=201)
def create_key(payload: KeyCreate, request: Request, customer: CurrentCustomer) -> dict:
    name = payload.name.strip()
    if not name or not name.isprintable():
        raise _error(422, "invalid_key_name", "请输入有效的密钥名称")
    secret = KEY_PREFIX + secrets.token_urlsafe(32)
    key = {
        "id": f"key_{uuid4().hex}", "name": name, "prefix": secret[:16] + "…",
        "created_at": _now(), "revoked_at": None,
    }
    with request.app.state.db.connect() as connection:
        connection.execute(
            """
            INSERT INTO business_keys (id, project_id, name, prefix, secret_hash, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (key["id"], customer.project_id, name, key["prefix"], _digest(secret), key["created_at"]),
        )
    return {"key": _public_key(key), "secret": secret}


@router.delete("/keys/{key_id}", status_code=204)
def revoke_key(key_id: str, request: Request, customer: CurrentCustomer) -> Response:
    with request.app.state.db.connect() as connection:
        result = connection.execute(
            "UPDATE business_keys SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ? AND project_id = ?",
            (_now(), key_id, customer.project_id),
        )
    if not result.rowcount:
        raise _error(404, "api_key_not_found", "业务 API Key 不存在")
    return Response(status_code=204)
