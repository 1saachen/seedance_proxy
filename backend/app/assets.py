import hashlib
import hmac
import ipaddress
import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .accounts import BusinessPrincipal, require_business_key


ARK_ASSET_HOST = "ark.cn-beijing.volcengineapi.com"
ARK_ASSET_ORIGIN = f"https://{ARK_ASSET_HOST}"
ARK_ASSET_VERSION = "2024-01-01"
ASSET_REFERENCE = re.compile(r"asset://ast_[0-9a-f]{32}")
UPSTREAM_RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
PUBLIC_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}")
AssetType = Literal["Image", "Video", "Audio"]
Action = Literal["CreateAssetGroup", "CreateAsset", "GetAsset"]
Principal = Annotated[BusinessPrincipal, Depends(require_business_key)]
PageNumber = Annotated[int, Query(alias="pageNumber", ge=1, le=1_000_000)]
PageSize = Annotated[int, Query(alias="pageSize", ge=1, le=100)]

router = APIRouter(prefix="/api", tags=["Asset library"])


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS asset_groups (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            upstream_id TEXT NOT NULL,
            upstream_project TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(id, project_id),
            UNIQUE(upstream_project, upstream_id)
        );
        CREATE INDEX IF NOT EXISTS asset_groups_project_created
            ON asset_groups(project_id, created_at DESC, id DESC);
        CREATE TABLE IF NOT EXISTS assets (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            group_id TEXT NOT NULL,
            upstream_id TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            asset_type TEXT NOT NULL CHECK (asset_type IN ('Image', 'Video', 'Audio')),
            status TEXT NOT NULL CHECK (status IN ('Processing', 'Active', 'Failed')),
            created_at TEXT NOT NULL,
            FOREIGN KEY(group_id, project_id) REFERENCES asset_groups(id, project_id)
        );
        CREATE INDEX IF NOT EXISTS assets_project_created
            ON assets(project_id, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS assets_group_created
            ON assets(project_id, group_id, created_at DESC, id DESC);
    """)


def is_asset_reference(value: str) -> bool:
    return ASSET_REFERENCE.fullmatch(value) is not None


def is_public_https(value: str) -> bool:
    if not value.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in value) or "\\" in value:
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.port not in (None, 443) or parsed.fragment:
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return bool(PUBLIC_HOST.fullmatch(host)) and not host.endswith((".local", ".internal", ".localhost", ".test", ".invalid"))
        return address.is_global and not address.is_multicast and not address.is_reserved
    except ValueError:
        return False


class NamedInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("name", check_fields=False)
    @classmethod
    def body_name(cls, value: str) -> str:
        name = value.strip()
        if not name or not name.isprintable():
            raise ValueError("a printable name is required")
        return name


class GroupCreate(NamedInput):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=300)


class AssetCreate(NamedInput):
    groupId: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=64)
    assetType: AssetType
    url: str = Field(min_length=1, max_length=4096)

    @field_validator("url")
    @classmethod
    def asset_url(cls, value: str) -> str:
        if not is_public_https(value):
            raise ValueError("asset source must use a public HTTPS URL")
        return value


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def _asset_not_found() -> HTTPException:
    return _error(404, "asset_not_found", "当前项目没有该素材")


def _group_not_found() -> HTTPException:
    return _error(404, "asset_group_not_found", "当前项目没有该素材组")


def _provider_invalid() -> HTTPException:
    return _error(502, "asset_provider_response_invalid", "方舟返回了无法确认的素材结果")


def _submission_unknown() -> HTTPException:
    return _error(502, "asset_submission_unknown", "方舟未确认素材创建结果，请核对后再重试")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _signed_headers(access_key: str, secret_key: str, query: str, body: bytes) -> dict[str, str]:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    content_hash = hashlib.sha256(body).hexdigest()
    headers = {
        "content-type": "application/json",
        "host": ARK_ASSET_HOST,
        "x-content-sha256": content_hash,
        "x-date": timestamp,
    }
    canonical_headers = "".join(f"{name}:{value}\n" for name, value in headers.items())
    signed_headers = ";".join(headers)
    canonical = "\n".join(("POST", "/", query, canonical_headers, signed_headers, content_hash))
    scope_parts = (timestamp[:8], "cn-beijing", "ark", "request")
    scope = "/".join(scope_parts)
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    to_sign = "\n".join(("HMAC-SHA256", timestamp, scope, request_hash))
    signing_key = secret_key.encode()
    for part in scope_parts:
        signing_key = hmac.digest(signing_key, part.encode(), "sha256")
    signature = hmac.digest(signing_key, to_sign.encode(), "sha256").hex()
    headers["authorization"] = (
        f"HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
    )
    return headers


async def _provider_request(request: Request, action: Action, upstream_project: str, payload: dict) -> dict:
    settings = request.app.state.settings
    access_key = settings.ark_asset_access_key.get_secret_value().strip() if settings.ark_asset_access_key else ""
    secret_key = settings.ark_asset_secret_key.get_secret_value().strip() if settings.ark_asset_secret_key else ""
    if not access_key or not secret_key or not upstream_project:
        raise _error(503, "asset_provider_unavailable", "方舟素材服务尚未配置")
    query = f"Action={action}&Version={ARK_ASSET_VERSION}"
    body = json.dumps({**payload, "ProjectName": upstream_project}, ensure_ascii=False, separators=(",", ":")).encode()
    headers = _signed_headers(access_key, secret_key, query, body)
    try:
        response = await request.app.state.ark_assets.post(f"/?{query}", content=body, headers=headers)
    except httpx.RequestError as error:
        if action != "GetAsset":
            raise _submission_unknown() from error
        raise _error(502, "asset_provider_unavailable", "暂时无法查询方舟素材") from error

    if action != "GetAsset" and response.status_code >= 500:
        raise _submission_unknown()
    try:
        data = response.json()
    except ValueError as error:
        raise _provider_invalid() from error
    if not isinstance(data, dict):
        raise _provider_invalid()
    metadata = data.get("ResponseMetadata")
    if not response.is_success or (isinstance(metadata, dict) and metadata.get("Error")):
        raise _error(502, "asset_provider_rejected", "方舟未接受素材请求")
    result = data.get("Result")
    if not isinstance(result, dict):
        raise _provider_invalid()
    return result


def _provider_id(result: dict, prefix: str) -> str:
    value = result.get("Id")
    if not isinstance(value, str) or not value.startswith(prefix) or not UPSTREAM_RESOURCE_ID.fullmatch(value):
        raise _provider_invalid()
    return value


def _current_project(request: Request) -> str:
    return request.app.state.settings.ark_asset_project.strip()


def _group_row(connection: sqlite3.Connection, project_id: str, group_id: str) -> sqlite3.Row:
    group = connection.execute(
        "SELECT * FROM asset_groups WHERE project_id = ? AND id = ?", (project_id, group_id),
    ).fetchone()
    if group is None:
        raise _group_not_found()
    return group


def _asset_row(connection: sqlite3.Connection, project_id: str, asset_id: str) -> sqlite3.Row:
    asset = connection.execute(
        """
        SELECT a.*, g.upstream_project, g.upstream_id AS upstream_group_id
        FROM assets AS a JOIN asset_groups AS g ON g.id = a.group_id AND g.project_id = a.project_id
        WHERE a.project_id = ? AND a.id = ?
        """,
        (project_id, asset_id),
    ).fetchone()
    if asset is None:
        raise _asset_not_found()
    return asset


def _validate_project_mapping(stored: str, current: str) -> None:
    if stored != current.strip():
        raise _error(503, "asset_provider_project_mismatch", "方舟素材项目配置与该素材归属不一致")


def translate_reference(
    connection: sqlite3.Connection,
    project_id: str,
    reference: str,
    *,
    media_type: str,
    upstream_project: str,
) -> str:
    if not is_asset_reference(reference):
        raise _asset_not_found()
    asset = _asset_row(connection, project_id, reference.removeprefix("asset://"))
    _validate_project_mapping(asset["upstream_project"], upstream_project)
    if asset["asset_type"] != media_type:
        raise _error(422, "asset_media_type_mismatch", "素材类型与请求的引用类型不一致")
    if asset["status"] != "Active":
        raise _error(409, "asset_not_active", "素材未处于可使用状态")
    return f"asset://{asset['upstream_id']}"


def _public_group(group: sqlite3.Row | dict) -> dict:
    return {
        "id": group["id"], "name": group["name"], "description": group["description"],
        "createdAt": group["created_at"],
    }


def _public_asset(asset: sqlite3.Row | dict) -> dict:
    public = {
        "id": asset["id"], "groupId": asset["group_id"], "name": asset["name"],
        "assetType": asset["asset_type"], "status": asset["status"], "createdAt": asset["created_at"],
    }
    if asset["status"] == "Active":
        public["reference"] = f"asset://{asset['id']}"
    return public


@router.post("/asset-group/create", status_code=201)
async def create_group(payload: GroupCreate, request: Request, principal: Principal) -> dict:
    upstream_project = _current_project(request)
    result = await _provider_request(request, "CreateAssetGroup", upstream_project, {
        "Name": payload.name, "Description": payload.description, "GroupType": "AIGC",
    })
    group = {"id": f"grp_{uuid4().hex}", "name": payload.name, "description": payload.description, "created_at": _now()}
    try:
        with request.app.state.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO asset_groups (id, project_id, upstream_id, upstream_project, name, description, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (group["id"], principal.project_id, _provider_id(result, "group-"), upstream_project,
                 group["name"], group["description"], group["created_at"]),
            )
    except sqlite3.IntegrityError as error:
        raise _provider_invalid() from error
    return {"group": _public_group(group)}


@router.get("/asset-group/list")
def list_groups(request: Request, principal: Principal, page_number: PageNumber = 1, page_size: PageSize = 20) -> dict:
    with request.app.state.db.connect() as connection:
        rows = connection.execute(
            """
            SELECT id, name, description, created_at FROM asset_groups WHERE project_id = ?
            ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
            """,
            (principal.project_id, page_size, (page_number - 1) * page_size),
        ).fetchall()
    return {"groups": [_public_group(row) for row in rows]}


@router.post("/asset/create", status_code=201)
async def create_asset(payload: AssetCreate, request: Request, principal: Principal) -> dict:
    with request.app.state.db.connect() as connection:
        group = _group_row(connection, principal.project_id, payload.groupId)
    upstream_project = _current_project(request)
    _validate_project_mapping(group["upstream_project"], upstream_project)
    result = await _provider_request(request, "CreateAsset", upstream_project, {
        "GroupId": group["upstream_id"], "URL": payload.url, "Name": payload.name, "AssetType": payload.assetType,
    })
    asset = {
        "id": f"ast_{uuid4().hex}", "group_id": group["id"], "name": payload.name,
        "asset_type": payload.assetType, "status": "Processing", "created_at": _now(),
    }
    try:
        with request.app.state.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO assets (id, project_id, group_id, upstream_id, name, asset_type, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (asset["id"], principal.project_id, group["id"], _provider_id(result, "asset-"), asset["name"],
                 asset["asset_type"], asset["status"], asset["created_at"]),
            )
    except sqlite3.IntegrityError as error:
        raise _provider_invalid() from error
    return {"asset": _public_asset(asset)}


@router.get("/asset/list")
def list_assets(
    request: Request,
    principal: Principal,
    group_id: Annotated[str | None, Query(alias="groupId", min_length=1, max_length=128)] = None,
    page_number: PageNumber = 1,
    page_size: PageSize = 20,
) -> dict:
    conditions = "project_id = ?"
    parameters: list[str | int] = [principal.project_id]
    with request.app.state.db.connect() as connection:
        if group_id is not None:
            _group_row(connection, principal.project_id, group_id)
            conditions += " AND group_id = ?"
            parameters.append(group_id)
        parameters.extend((page_size, (page_number - 1) * page_size))
        rows = connection.execute(
            "SELECT * FROM assets WHERE " + conditions + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            parameters,
        ).fetchall()
    return {"assets": [_public_asset(row) for row in rows]}


@router.get("/asset/get")
async def get_asset(
    request: Request,
    principal: Principal,
    asset_id: Annotated[str, Query(alias="assetId", min_length=1, max_length=128)],
) -> dict:
    with request.app.state.db.connect() as connection:
        asset = _asset_row(connection, principal.project_id, asset_id)
    if asset["status"] != "Processing":
        return {"asset": _public_asset(asset)}
    upstream_project = _current_project(request)
    _validate_project_mapping(asset["upstream_project"], upstream_project)
    result = await _provider_request(request, "GetAsset", upstream_project, {"Id": asset["upstream_id"]})
    expected = {
        "Id": asset["upstream_id"], "GroupId": asset["upstream_group_id"], "AssetType": asset["asset_type"],
        "ProjectName": upstream_project,
    }
    status = result.get("Status")
    if any(result.get(field) != value for field, value in expected.items()) or status not in ("Processing", "Active", "Failed"):
        raise _provider_invalid()
    with request.app.state.db.connect() as connection:
        if status != "Processing":
            connection.execute(
                "UPDATE assets SET status = ? WHERE project_id = ? AND id = ? AND status = 'Processing'",
                (status, principal.project_id, asset_id),
            )
        refreshed = _asset_row(connection, principal.project_id, asset_id)
    return {"asset": _public_asset(refreshed)}
