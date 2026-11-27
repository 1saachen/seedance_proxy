import asyncio
import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import upload_registration
from .accounts import BusinessPrincipal, SeedancePrincipal as Principal
from .urls import is_public_https


ARK_ASSET_HOST = "ark.cn-beijing.volcengineapi.com"
ARK_ASSET_ORIGIN = f"https://{ARK_ASSET_HOST}"
ARK_ASSET_VERSION = "2024-01-01"
ASSET_REFERENCE = re.compile(r"asset://ast_[0-9a-f]{32}")
UPSTREAM_RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
AssetType = Literal["Image", "Video", "Audio"]
AssetStatus = Literal["Processing", "Active", "Failed"]
GroupType = Literal["AIGC", "LivenessFace"]
Action = Literal[
    "CreateAssetGroup", "GetAssetGroup", "UpdateAssetGroup", "DeleteAssetGroup",
    "CreateAsset", "GetAsset", "UpdateAsset", "DeleteAsset",
    "CreateVisualValidateSession", "GetVisualValidateResult",
]
CREATE_ACTIONS = {"CreateAssetGroup", "CreateAsset", "CreateVisualValidateSession"}
READ_ACTIONS = {"GetAssetGroup", "GetAsset", "GetVisualValidateResult"}
DELETE_ACTIONS = {"DeleteAssetGroup", "DeleteAsset"}
PageNumber = Annotated[int, Query(alias="pageNumber", ge=1, le=1_000_000)]
PageSize = Annotated[int, Query(alias="pageSize", ge=1, le=100)]
GroupId = Annotated[str, Field(min_length=1, max_length=128)]
GroupFilter = Annotated[list[GroupId] | None, Query(alias="groupId", max_length=100)]
NameFilter = Annotated[str | None, Query(min_length=1, max_length=255)]
SortOrder = Annotated[Literal["asc", "desc"], Query(alias="sortOrder")]
GroupTypeFilter = Annotated[GroupType | None, Query(alias="groupType")]
GROUP_TYPE_SQL = "CASE WHEN ls.group_id IS NULL THEN 'AIGC' ELSE 'LivenessFace' END"

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
        CREATE TABLE IF NOT EXISTS asset_group_deletions (
            group_id TEXT PRIMARY KEY REFERENCES asset_groups(id),
            confirmed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS asset_deletions (
            asset_id TEXT PRIMARY KEY REFERENCES assets(id),
            confirmed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS asset_liveness_sessions (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            key_id TEXT NOT NULL REFERENCES business_keys(id),
            upstream_project TEXT NOT NULL,
            token_hash TEXT NOT NULL UNIQUE,
            expires_at INTEGER NOT NULL,
            group_id TEXT UNIQUE,
            FOREIGN KEY(group_id, project_id) REFERENCES asset_groups(id, project_id)
        );
    """)


def is_asset_reference(value: str) -> bool:
    return ASSET_REFERENCE.fullmatch(value) is not None


class NamedInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("name", check_fields=False)
    @classmethod
    def body_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        name = value.strip()
        if not name or not name.isprintable():
            raise ValueError("a printable name is required")
        return name


class GroupCreate(NamedInput):
    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=300)


class GroupUpdate(NamedInput):
    groupId: GroupId
    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def has_changes(self) -> "GroupUpdate":
        if self.name is None and self.description is None:
            raise ValueError("provide a name or description to update")
        return self


class GroupDelete(NamedInput):
    groupId: GroupId


class AssetUpdate(NamedInput):
    assetId: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=64)


class AssetDelete(NamedInput):
    assetId: str = Field(min_length=1, max_length=128)


class AssetCreate(NamedInput):
    groupId: GroupId
    name: str = Field(min_length=1, max_length=64)
    assetType: AssetType
    url: str | None = Field(default=None, min_length=1, max_length=4096)
    uploadId: str | None = Field(default=None, pattern=r"^upl_[0-9a-f]{32}$")

    @field_validator("url")
    @classmethod
    def asset_url(cls, value: str | None) -> str | None:
        if value is not None and not is_public_https(value):
            raise ValueError("asset source must use a public HTTPS URL")
        return value

    @model_validator(mode="after")
    def one_source(self) -> "AssetCreate":
        if (self.url is None) == (self.uploadId is None):
            raise ValueError("provide one of url or uploadId")
        return self


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


def _uncertain_response(action: Action) -> HTTPException:
    if action in CREATE_ACTIONS:
        return _submission_unknown()
    if action in READ_ACTIONS:
        return _error(502, "asset_provider_unavailable", "暂时无法查询方舟素材")
    return _error(502, "asset_mutation_unknown", "方舟未确认素材操作结果，请使用同一请求重试")


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


async def _provider_request(
    request: Request, action: Action, upstream_project: str, payload: dict, *, deletion_retry: bool = False,
) -> dict:
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
        raise _uncertain_response(action) from error

    if action not in READ_ACTIONS and (response.status_code == 408 or response.status_code >= 500):
        raise _uncertain_response(action)
    try:
        data = response.json()
    except ValueError as error:
        raise _provider_invalid() from error
    if not isinstance(data, dict):
        raise _provider_invalid()
    metadata = data.get("ResponseMetadata")
    provider_error = metadata.get("Error") if isinstance(metadata, dict) else None
    if (action in DELETE_ACTIONS and deletion_retry and response.status_code == 404 and isinstance(provider_error, dict)
            and provider_error.get("Code") in ("NotFound", "ResourceNotFound") and metadata.get("Action") == action):
        # A previous, unconfirmed delete may already have applied to this owned resource.
        return {}
    if not response.is_success or provider_error:
        raise _error(502, "asset_provider_rejected", "方舟未接受素材请求")
    result = data.get("Result")
    if not isinstance(result, dict) or (action in DELETE_ACTIONS and result):
        raise _provider_invalid()
    return result


def _provider_id(result: dict, prefix: str) -> str:
    value = result.get("Id")
    if not isinstance(value, str) or not value.startswith(prefix) or not UPSTREAM_RESOURCE_ID.fullmatch(value):
        raise _provider_invalid()
    return value


def _current_project(request: Request) -> str:
    return request.app.state.settings.ark_asset_project.strip()


def _resource_operation_lock(request: Request, resource_id: str) -> asyncio.Lock:
    # The app uses one SQLite instance. Serialize provider writes to a group without holding a database lock over I/O.
    if not hasattr(request.app.state, "asset_group_mutations"):
        request.app.state.asset_group_mutations = WeakValueDictionary()
    locks = request.app.state.asset_group_mutations
    lock = locks.get(resource_id)
    if lock is None:
        locks[resource_id] = lock = asyncio.Lock()
    return lock


def _group_row(
    connection: sqlite3.Connection, project_id: str, group_id: str, *, allow_deletion: bool = False,
) -> sqlite3.Row:
    group = connection.execute(
        f"""
        SELECT g.*, d.group_id AS deleting_group, d.confirmed_at AS group_deleted_at,
               {GROUP_TYPE_SQL} AS group_type
        FROM asset_groups AS g LEFT JOIN asset_group_deletions AS d ON d.group_id = g.id
        LEFT JOIN asset_liveness_sessions AS ls ON ls.group_id = g.id
        WHERE g.project_id = ? AND g.id = ?
        """,
        (project_id, group_id),
    ).fetchone()
    if group is None or (not allow_deletion and group["group_deleted_at"] is not None):
        raise _group_not_found()
    if not allow_deletion and group["deleting_group"] is not None:
        raise _error(409, "asset_deletion_pending", "素材组的删除结果尚未确认")
    return group


def _asset_row(
    connection: sqlite3.Connection, project_id: str, asset_id: str, *, allow_deletion: bool = False,
) -> sqlite3.Row:
    asset = connection.execute(
        """
        SELECT a.*, g.upstream_project, g.upstream_id AS upstream_group_id,
               gd.group_id AS deleting_group, gd.confirmed_at AS group_deleted_at,
               ad.asset_id AS deleting_asset, ad.confirmed_at AS asset_deleted_at
        FROM assets AS a JOIN asset_groups AS g ON g.id = a.group_id AND g.project_id = a.project_id
        LEFT JOIN asset_group_deletions AS gd ON gd.group_id = g.id
        LEFT JOIN asset_deletions AS ad ON ad.asset_id = a.id
        WHERE a.project_id = ? AND a.id = ?
        """,
        (project_id, asset_id),
    ).fetchone()
    if asset is None or (not allow_deletion and (asset["asset_deleted_at"] or asset["group_deleted_at"])):
        raise _asset_not_found()
    if not allow_deletion and (asset["deleting_group"] or asset["deleting_asset"]):
        raise _error(409, "asset_deletion_pending", "素材的删除结果尚未确认")
    return asset


@asynccontextmanager
async def _owned_group_operation(request: Request, project_id: str, group_id: str, *, allow_deletion: bool = False):
    database = request.app.state.db
    with database.connect() as connection:
        _group_row(connection, project_id, group_id, allow_deletion=allow_deletion)
    async with _resource_operation_lock(request, group_id):
        with database.connect() as connection:
            group = _group_row(connection, project_id, group_id, allow_deletion=allow_deletion)
        yield group


@asynccontextmanager
async def _owned_asset_operation(request: Request, project_id: str, asset_id: str, *, allow_deletion: bool = False):
    database = request.app.state.db
    with database.connect() as connection:
        group_id = _asset_row(connection, project_id, asset_id, allow_deletion=allow_deletion)["group_id"]
    async with _resource_operation_lock(request, group_id):
        with database.connect() as connection:
            asset = _asset_row(connection, project_id, asset_id, allow_deletion=allow_deletion)
        yield asset


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
        "groupType": group["group_type"], "createdAt": group["created_at"],
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
    group = {
        "id": f"grp_{uuid4().hex}", "name": payload.name, "description": payload.description,
        "group_type": "AIGC", "created_at": _now(),
    }
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
def list_groups(
    request: Request, principal: Principal, page_number: PageNumber = 1, page_size: PageSize = 20,
    name: NameFilter = None, group_ids: GroupFilter = None, sort_order: SortOrder = "desc",
    group_type: GroupTypeFilter = None,
) -> dict:
    conditions = ["g.project_id = ?", "d.group_id IS NULL"]
    parameters: list[str | int] = [principal.project_id]
    if name is not None:
        conditions.append("instr(lower(g.name), lower(?)) > 0")
        parameters.append(name)
    if group_ids:
        conditions.append(f"g.id IN ({', '.join('?' for _ in group_ids)})")
        parameters.extend(group_ids)
    if group_type is not None:
        conditions.append(f"({GROUP_TYPE_SQL}) = ?")
        parameters.append(group_type)
    direction = "ASC" if sort_order == "asc" else "DESC"
    parameters.extend((page_size, (page_number - 1) * page_size))
    with request.app.state.db.connect() as connection:
        rows = connection.execute(
            f"SELECT g.id, g.name, g.description, g.created_at, {GROUP_TYPE_SQL} AS group_type FROM asset_groups AS g "
            "LEFT JOIN asset_group_deletions AS d ON d.group_id = g.id "
            "LEFT JOIN asset_liveness_sessions AS ls ON ls.group_id = g.id WHERE " + " AND ".join(conditions)
            + f" ORDER BY g.created_at {direction}, g.id {direction} LIMIT ? OFFSET ?",
            parameters,
        ).fetchall()
    return {"groups": [_public_group(row) for row in rows]}


@router.get("/asset-group/get")
async def get_group(
    request: Request, principal: Principal,
    group_id: Annotated[str, Query(alias="groupId", min_length=1, max_length=128)],
) -> dict:
    async with _owned_group_operation(request, principal.project_id, group_id) as group:
        upstream_project = _current_project(request)
        _validate_project_mapping(group["upstream_project"], upstream_project)
        result = await _provider_request(request, "GetAssetGroup", upstream_project, {"Id": group["upstream_id"]})
        if result.get("Id") != group["upstream_id"] or result.get("ProjectName") != upstream_project or (
            result.get("GroupType") != group["group_type"]
        ):
            raise _provider_invalid()
        if group["group_type"] == "LivenessFace":
            return {"group": _public_group(group)}
        name, description = result.get("Name"), result.get("Description", "")
        if not isinstance(name, str) or not name.strip() or len(name) > 255 or (
            not isinstance(description, str) or len(description) > 300
        ):
            raise _provider_invalid()
        with request.app.state.db.connect() as connection:
            connection.execute(
                "UPDATE asset_groups SET name = ?, description = ? WHERE project_id = ? AND id = ?",
                (name, description, principal.project_id, group_id),
            )
        return {"group": _public_group({**dict(group), "name": name, "description": description})}


@router.post("/asset-group/update")
async def update_group(payload: GroupUpdate, request: Request, principal: Principal) -> dict:
    async with _owned_group_operation(request, principal.project_id, payload.groupId) as group:
        upstream_project = _current_project(request)
        _validate_project_mapping(group["upstream_project"], upstream_project)
        changes = payload.model_dump(exclude={"groupId"}, exclude_none=True)
        result = await _provider_request(request, "UpdateAssetGroup", upstream_project, {
            "Id": group["upstream_id"], **{name.capitalize(): value for name, value in changes.items()},
        })
        if result.get("Id") != group["upstream_id"]:
            raise _provider_invalid()
        updated = {**dict(group), **changes}
        with request.app.state.db.connect() as connection:
            connection.execute(
                "UPDATE asset_groups SET name = ?, description = ? WHERE project_id = ? AND id = ?",
                (updated["name"], updated["description"], principal.project_id, group["id"]),
            )
        return {"group": _public_group(updated)}


async def _delete_owned_resource(
    request: Request, action: Literal["DeleteAssetGroup", "DeleteAsset"],
    local_id: str, upstream_id: str, upstream_project: str,
) -> Response:
    table, column = ("asset_group_deletions", "group_id") if action == "DeleteAssetGroup" else ("asset_deletions", "asset_id")
    database = request.app.state.db
    with database.connect() as connection:
        claimed = connection.execute(
            f"INSERT INTO {table} ({column}) VALUES (?) ON CONFLICT ({column}) DO NOTHING",
            (local_id,),
        ).rowcount
    try:
        await _provider_request(request, action, upstream_project, {"Id": upstream_id}, deletion_retry=not claimed)
    except HTTPException as error:
        if claimed and error.detail["code"] in {"asset_provider_unavailable", "asset_provider_rejected"}:
            with database.connect() as connection:
                connection.execute(f"DELETE FROM {table} WHERE {column} = ? AND confirmed_at IS NULL", (local_id,))
        raise
    with database.connect() as connection:
        connection.execute(f"UPDATE {table} SET confirmed_at = ? WHERE {column} = ?", (_now(), local_id))
        connection.execute(f"UPDATE asset_upload_registrations SET fetch_expires_at = 0 WHERE {column} = ?", (local_id,))
    return Response(status_code=204)


@router.post("/asset-group/delete", status_code=204)
async def delete_group(payload: GroupDelete, request: Request, principal: Principal) -> Response:
    async with _owned_group_operation(request, principal.project_id, payload.groupId, allow_deletion=True) as group:
        if group["group_deleted_at"] is not None:
            return Response(status_code=204)
        upstream_project = _current_project(request)
        _validate_project_mapping(group["upstream_project"], upstream_project)
        return await _delete_owned_resource(request, "DeleteAssetGroup", group["id"], group["upstream_id"], upstream_project)


@router.post("/asset/create", status_code=201, responses={200: {"description": "Previously registered upload"}})
async def create_asset(payload: AssetCreate, request: Request, response: Response, principal: Principal) -> dict:
    async with _owned_group_operation(request, principal.project_id, payload.groupId) as group:
        return await _create_asset(payload, request, response, principal, group)


async def _create_asset(
    payload: AssetCreate, request: Request, response: Response, principal: BusinessPrincipal, group: sqlite3.Row,
) -> dict:
    upstream_project = _current_project(request)
    _validate_project_mapping(group["upstream_project"], upstream_project)

    source_url = payload.url
    if payload.uploadId is not None:
        with request.app.state.db.connect() as connection:
            source = upload_registration.reserve_source(
                connection, request.app.state.settings, request.app.state.upload_storage, principal,
                payload.uploadId, group_id=group["id"], name=payload.name, media_type=payload.assetType,
            )
            if source.asset_id is not None:
                response.status_code = 200
                return {"asset": _public_asset(_asset_row(connection, principal.project_id, source.asset_id))}
        source_url = source.url
    elif upload_registration.is_temporary_source_url(source_url):
        raise _error(422, "upload_source_requires_id", "本服务暂存文件须通过 uploadId 登记")

    try:
        result = await _provider_request(request, "CreateAsset", upstream_project, {
            "GroupId": group["upstream_id"], "URL": source_url, "Name": payload.name, "AssetType": payload.assetType,
        })
    except HTTPException as error:
        if payload.uploadId is not None and error.detail["code"] in {"asset_provider_unavailable", "asset_provider_rejected"}:
            with request.app.state.db.connect() as connection:
                upload_registration.release_rejected_source(connection, payload.uploadId)
        raise

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
            if payload.uploadId is not None:
                upload_registration.complete_source(connection, payload.uploadId, asset["id"])
    except sqlite3.IntegrityError as error:
        raise _provider_invalid() from error
    return {"asset": _public_asset(asset)}


@router.post("/asset/update")
async def update_asset(payload: AssetUpdate, request: Request, principal: Principal) -> dict:
    async with _owned_asset_operation(request, principal.project_id, payload.assetId) as asset:
        upstream_project = _current_project(request)
        _validate_project_mapping(asset["upstream_project"], upstream_project)
        result = await _provider_request(request, "UpdateAsset", upstream_project, {
            "Id": asset["upstream_id"], "Name": payload.name,
        })
        if result.get("Id") != asset["upstream_id"]:
            raise _provider_invalid()
        with request.app.state.db.connect() as connection:
            connection.execute(
                "UPDATE assets SET name = ? WHERE project_id = ? AND id = ?",
                (payload.name, principal.project_id, asset["id"]),
            )
            updated = _asset_row(connection, principal.project_id, asset["id"])
        return {"asset": _public_asset(updated)}


@router.post("/asset/delete", status_code=204)
async def delete_asset(payload: AssetDelete, request: Request, principal: Principal) -> Response:
    async with _owned_asset_operation(request, principal.project_id, payload.assetId, allow_deletion=True) as asset:
        if asset["asset_deleted_at"] or asset["group_deleted_at"]:
            return Response(status_code=204)
        if asset["deleting_group"] is not None:
            raise _error(409, "asset_deletion_pending", "素材组的删除结果尚未确认")
        upstream_project = _current_project(request)
        _validate_project_mapping(asset["upstream_project"], upstream_project)
        return await _delete_owned_resource(request, "DeleteAsset", asset["id"], asset["upstream_id"], upstream_project)


@router.get("/asset/list")
def list_assets(
    request: Request,
    principal: Principal,
    group_ids: GroupFilter = None,
    page_number: PageNumber = 1,
    page_size: PageSize = 20,
    name: NameFilter = None,
    statuses: Annotated[list[AssetStatus] | None, Query(alias="status", max_length=3)] = None,
    asset_type: Annotated[AssetType | None, Query(alias="assetType")] = None,
    sort_order: SortOrder = "desc",
    group_type: GroupTypeFilter = None,
) -> dict:
    conditions = ["a.project_id = ?", "ad.asset_id IS NULL", "gd.group_id IS NULL"]
    parameters: list[str | int] = [principal.project_id]
    with request.app.state.db.connect() as connection:
        if group_ids:
            for group_id in dict.fromkeys(group_ids):
                _group_row(connection, principal.project_id, group_id)
            conditions.append(f"a.group_id IN ({', '.join('?' for _ in group_ids)})")
            parameters.extend(group_ids)
        if name is not None:
            conditions.append("instr(lower(a.name), lower(?)) > 0")
            parameters.append(name)
        if statuses:
            conditions.append(f"a.status IN ({', '.join('?' for _ in statuses)})")
            parameters.extend(statuses)
        if asset_type is not None:
            conditions.append("a.asset_type = ?")
            parameters.append(asset_type)
        if group_type is not None:
            conditions.append(f"({GROUP_TYPE_SQL}) = ?")
            parameters.append(group_type)
        direction = "ASC" if sort_order == "asc" else "DESC"
        parameters.extend((page_size, (page_number - 1) * page_size))
        rows = connection.execute(
            "SELECT a.* FROM assets AS a "
            "LEFT JOIN asset_deletions AS ad ON ad.asset_id = a.id "
            "LEFT JOIN asset_group_deletions AS gd ON gd.group_id = a.group_id "
            "LEFT JOIN asset_liveness_sessions AS ls ON ls.group_id = a.group_id WHERE " + " AND ".join(conditions)
            + f" ORDER BY a.created_at {direction}, a.id {direction} LIMIT ? OFFSET ?",
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
