import hashlib
import hmac
import re
import sqlite3
import time
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import Field, field_validator

from . import assets
from .accounts import BusinessPrincipal
from .urls import is_public_https


router = APIRouter(prefix="/api/asset-group/liveness", tags=["Asset library"])
AUTHORIZATION_HOST = "ark.volcengine.com"
AUTHORIZATION_PATH = "/region:cn-beijing/mobile/livenees-face-manage/authorization"
SESSION_TTL_SECONDS = 180
TOKEN_PATTERN = r"^[A-Za-z0-9_-]{1,256}$"


class StartAuthorization(assets.NamedInput):
    callbackUrl: str = Field(min_length=1, max_length=2048)

    @field_validator("callbackUrl")
    @classmethod
    def public_callback(cls, value: str) -> str:
        if not is_public_https(value):
            raise ValueError("callbackUrl must be a public HTTPS URL")
        if {key.casefold() for key in parse_qs(urlsplit(value).query, keep_blank_values=True)} & {"bytedtoken", "resultcode"}:
            raise ValueError("callbackUrl cannot contain authorization result fields")
        return value


class CompleteAuthorization(assets.NamedInput):
    sessionId: str = Field(pattern=r"^lvs_[0-9a-f]{32}$")
    bytedToken: str = Field(pattern=TOKEN_PATTERN)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _session(
    connection: sqlite3.Connection, principal: BusinessPrincipal, payload: CompleteAuthorization,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM asset_liveness_sessions WHERE id = ? AND project_id = ? AND key_id = ?",
        (payload.sessionId, principal.project_id, principal.key_id),
    ).fetchone()
    if row is None or not hmac.compare_digest(row["token_hash"], _digest(payload.bytedToken)):
        raise assets._error(404, "asset_liveness_not_found", "没有匹配的真人授权会话")
    return row


def _authorization_url(result: dict, callback_url: str) -> str:
    link = result.get("H5Link")
    if not isinstance(link, str) or len(link) > 8192 or not is_public_https(link):
        raise assets._provider_invalid()
    parts = urlsplit(link)
    if parts.hostname != AUTHORIZATION_HOST or parts.path != AUTHORIZATION_PATH or not parts.query:
        raise assets._provider_invalid()
    if result.get("CallbackURL", callback_url) != callback_url:
        raise assets._provider_invalid()
    return link


@router.post("/start", status_code=201)
async def start_authorization(payload: StartAuthorization, request: Request, principal: assets.Principal) -> dict:
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    upstream_project = assets._current_project(request)
    result = await assets._provider_request(request, "CreateVisualValidateSession", upstream_project, {"CallbackURL": payload.callbackUrl})
    link = _authorization_url(result, payload.callbackUrl)
    token = result.get("BytedToken")
    if not isinstance(token, str) or not re.fullmatch(TOKEN_PATTERN, token, re.ASCII):
        raise assets._provider_invalid()
    session_id = f"lvs_{uuid4().hex}"
    try:
        with request.app.state.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO asset_liveness_sessions (id, project_id, key_id, upstream_project, token_hash, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (session_id, principal.project_id, principal.key_id, upstream_project, _digest(token), expires_at),
            )
    except sqlite3.IntegrityError as error:
        raise assets._provider_invalid() from error
    return {
        "sessionId": session_id, "authorizationUrl": link,
        "expiresAt": datetime.fromtimestamp(expires_at, UTC).isoformat(timespec="seconds"),
    }


@router.post("/complete")
async def complete_authorization(payload: CompleteAuthorization, request: Request, principal: assets.Principal) -> dict:
    database = request.app.state.db
    with database.connect() as connection:
        _session(connection, principal, payload)
    async with assets._resource_operation_lock(request, payload.sessionId):
        with database.connect() as connection:
            row = _session(connection, principal, payload)
            if row["group_id"] is not None:
                return {"group": assets._public_group(assets._group_row(connection, principal.project_id, row["group_id"]))}
        if int(time.time()) >= row["expires_at"]:
            raise assets._error(409, "asset_liveness_expired", "真人授权链接已过期，请重新开始授权")
        upstream_project = assets._current_project(request)
        assets._validate_project_mapping(row["upstream_project"], upstream_project)
        try:
            result = await assets._provider_request(request, "GetVisualValidateResult", upstream_project, {"BytedToken": payload.bytedToken})
        except HTTPException as error:
            if error.detail["code"] == "asset_provider_rejected":
                raise assets._error(409, "asset_liveness_unconfirmed", "方舟尚未确认真人授权") from None
            raise
        if result.get("GroupId") is None:
            raise assets._error(409, "asset_liveness_unconfirmed", "方舟尚未确认真人授权")
        upstream_id = assets._provider_id({"Id": result["GroupId"]}, "group-")
        group_id = f"grp_{uuid4().hex}"
        try:
            with database.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = _session(connection, principal, payload)
                if row["group_id"] is not None:
                    return {"group": assets._public_group(assets._group_row(connection, principal.project_id, row["group_id"]))}
                connection.execute(
                    """
                    INSERT INTO asset_groups (id, project_id, upstream_id, upstream_project, name, description, created_at)
                    VALUES (?, ?, ?, ?, '已授权真人素材组', '', ?)
                    """,
                    (group_id, principal.project_id, upstream_id, upstream_project, assets._now()),
                )
                connection.execute("UPDATE asset_liveness_sessions SET group_id = ? WHERE id = ?", (group_id, row["id"]))
                return {"group": assets._public_group(assets._group_row(connection, principal.project_id, group_id))}
        except sqlite3.IntegrityError as error:
            raise assets._provider_invalid() from error
