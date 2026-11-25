import asyncio
import hashlib
import logging
import re
import sqlite3
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated, BinaryIO
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .accounts import BusinessPrincipal
from .assets import Principal
from .db import Database
from .media import MAX_UPLOAD_BYTES, MediaInfo, inspect_media
from .upload_storage import StorageError, UploadStorage


MAX_BODY_BYTES = MAX_UPLOAD_BYTES + 64 * 1024
MAX_PART_HEADER_BYTES = 16 * 1024
# A worker can recover local files left behind by an interrupted write.
UPLOAD_RECOVERY_SECONDS = 30 * 60
CLEANUP_LEASE_SECONDS = 5 * 60
CLEANUP_RETRY_SECONDS = 60
UPLOAD_ID = re.compile(r"upl_[0-9a-f]{32}")
logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/asset", tags=["Asset library"])


def initialize(connection: sqlite3.Connection) -> None:
    # Keep the previous storage column so existing remote records remain identifiable.
    connection.executescript("""
        CREATE UNIQUE INDEX IF NOT EXISTS business_keys_id_project ON business_keys(id, project_id);
        CREATE TABLE IF NOT EXISTS asset_uploads (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            api_key_id TEXT NOT NULL,
            idempotency_key TEXT,
            content_sha256 TEXT NOT NULL,
            asset_type TEXT NOT NULL CHECK (asset_type IN ('Image', 'Video', 'Audio')),
            content_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
            bucket TEXT NOT NULL,
            object_key TEXT UNIQUE NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('uploading', 'staged', 'cleanup_pending', 'failed', 'expired')),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            expires_at INTEGER,
            cleanup_after INTEGER,
            FOREIGN KEY(api_key_id, project_id) REFERENCES business_keys(id, project_id),
            UNIQUE(project_id, api_key_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS asset_uploads_local_cleanup ON asset_uploads(cleanup_after)
            WHERE bucket = '' AND cleanup_after IS NOT NULL;
    """)


def _error(status: int, code: str, message: str, upload_id: str | None = None) -> HTTPException:
    detail = {"code": code, "message": message}
    if upload_id is not None:
        detail["uploadId"] = upload_id
    return HTTPException(status, detail=detail)


def _too_large() -> HTTPException:
    return _error(413, "file_too_large", "上传文件超过大小限制")


def _invalid_form() -> HTTPException:
    return _error(422, "invalid_upload_form", "请提交一个 file，可选字段 assetType 为 Image、Video 或 Audio")


def _legacy_upload(upload_id: str) -> HTTPException:
    return _error(409, "upload_storage_changed", "该记录位于旧暂存存储，请使用新的幂等键上传", upload_id)


class _BodyTooLarge(MultiPartException):
    pass


class _UploadFormParser(MultiPartParser):
    ended = False

    def on_part_begin(self) -> None:
        self.header_bytes = 0
        super().on_part_begin()

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._count_header_fragment(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._count_header_fragment(end - start)
        super().on_header_value(data, start, end)

    def _count_header_fragment(self, size: int) -> None:
        self.header_bytes += size
        if self.header_bytes > MAX_PART_HEADER_BYTES:
            raise MultiPartException("Multipart headers are too large")

    def on_end(self) -> None:
        self.ended = True

    async def parse(self) -> FormData:
        try:
            form = await super().parse()
            # A disconnected or truncated form must not be accepted after a complete file part.
            if not self.ended:
                raise MultiPartException("Incomplete multipart body")
            return form
        except BaseException:
            for file in self._files_to_close_on_error:
                if not file.closed:
                    file.close()
            raise


async def _bounded_body(request: Request) -> AsyncIterator[bytes]:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise _BodyTooLarge("Upload is too large")
        yield chunk


async def _read_upload_form(request: Request) -> FormData:
    media_type, options = parse_options_header(request.headers.get("Content-Type"))
    if media_type.lower() != b"multipart/form-data":
        raise _error(415, "multipart_required", "请使用 multipart/form-data 上传文件")
    if not 1 <= len(options.get(b"boundary", b"")) <= 70:
        raise _invalid_form()
    length = request.headers.get("Content-Length", "")
    if length and (not length.isascii() or not length.isdecimal()):
        raise _invalid_form()
    if length and (len(length) > 15 or int(length) > MAX_BODY_BYTES):
        raise _too_large()
    # Bound the bytes actually received, including requests without Content-Length.
    parser = _UploadFormParser(request.headers, _bounded_body(request), max_files=1, max_fields=1, max_part_size=64)
    try:
        return await parser.parse()
    except _BodyTooLarge:
        raise _too_large() from None
    except (MultiPartException, MultipartParseError):
        raise _invalid_form() from None


def _fingerprint(file: BinaryIO) -> str:
    file.seek(0)
    digest = hashlib.file_digest(file, "sha256").hexdigest()
    file.seek(0)
    return digest


def _public_upload(row: sqlite3.Row) -> dict:
    expiry = row["expires_at"]
    status = row["status"]
    if status == "staged" and expiry <= int(time.time()):
        status = "cleanup_pending"
    return {
        "uploadId": row["id"],
        "assetType": row["asset_type"],
        "contentType": row["content_type"],
        "sizeBytes": row["size_bytes"],
        "status": status,
        "expiresAt": datetime.fromtimestamp(expiry, UTC).isoformat() if expiry is not None else None,
    }


def _reserve_upload(
    database: Database, principal: BusinessPrincipal, *, media: MediaInfo, size: int,
    fingerprint: str, idempotency_hash: str | None,
) -> tuple[sqlite3.Row, bool]:
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        existing = None
        if idempotency_hash is not None:
            existing = connection.execute(
                "SELECT * FROM asset_uploads WHERE project_id = ? AND api_key_id = ? AND idempotency_key = ?",
                (principal.project_id, principal.key_id, idempotency_hash),
            ).fetchone()
        if existing is not None:
            upload_id = existing["id"]
            if existing["bucket"]:
                raise _legacy_upload(upload_id)
            if existing["content_sha256"] != fingerprint or existing["content_type"] != media.content_type:
                raise _error(409, "idempotency_key_conflict", "该幂等键已用于不同文件", upload_id)
            status = existing["status"]
            if status == "expired" or (status == "staged" and existing["expires_at"] <= now):
                raise _error(409, "upload_expired", "暂存已过期，请使用新的幂等键上传", upload_id)
            if status == "staged":
                return existing, False
            if status != "failed":
                raise _error(409, "upload_in_progress", "上传或清理尚未完成，请稍后查询", upload_id)
        else:
            upload_id = f"upl_{uuid4().hex}"

        object_key = f"staging/{principal.project_id}/{upload_id}/{uuid4().hex}{media.extension}"
        recovery_at = now + UPLOAD_RECOVERY_SECONDS
        if existing is not None:
            row = connection.execute(
                """
                UPDATE asset_uploads SET object_key = ?, status = 'uploading', updated_at = ?, cleanup_after = ?
                WHERE id = ? RETURNING *
                """,
                (object_key, now, recovery_at, upload_id),
            ).fetchone()
        else:
            row = connection.execute(
                """
                INSERT INTO asset_uploads (
                    id, project_id, api_key_id, idempotency_key, content_sha256, asset_type, content_type,
                    size_bytes, bucket, object_key, status, created_at, updated_at, cleanup_after
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, 'uploading', ?, ?, ?) RETURNING *
                """,
                (upload_id, principal.project_id, principal.key_id, idempotency_hash, fingerprint,
                 media.asset_type, media.content_type, size, object_key, now, now, recovery_at),
            ).fetchone()
    return row, True


def _settle_upload(database: Database, record: sqlite3.Row, ttl: int | None) -> sqlite3.Row | None:
    now = int(time.time())
    status = "cleanup_pending" if ttl is None else "staged"
    expires_at = None if ttl is None else now + ttl
    cleanup_after = now + CLEANUP_RETRY_SECONDS if expires_at is None else expires_at
    with database.connect() as connection:
        return connection.execute(
            """
            UPDATE asset_uploads SET status = ?, expires_at = ?, updated_at = ?, cleanup_after = ?
            WHERE id = ? AND object_key = ? AND status = 'uploading' AND cleanup_after = ? RETURNING *
            """,
            (status, expires_at, now, cleanup_after, record["id"], record["object_key"], record["cleanup_after"]),
        ).fetchone()


@router.post(
    "/upload-file", status_code=201,
    responses={200: {"description": "Replayed staged upload"}},
    openapi_extra={"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["file"], "additionalProperties": False,
        "properties": {
            "file": {"type": "string", "format": "binary"},
            "assetType": {"type": "string", "enum": ["Image", "Video", "Audio"]},
        },
    }}}}},
)
async def upload_file(
    request: Request, response: Response, principal: Principal,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    settings = request.app.state.settings
    storage: UploadStorage = request.app.state.upload_storage
    idempotency_hash = None
    if idempotency_key is not None:
        if not re.fullmatch(r"[\x21-\x7e]{1,128}", idempotency_key):
            raise _error(422, "idempotency_key_invalid", "Idempotency-Key 须为 1 至 128 个可见 ASCII 字符")
        idempotency_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()

    form = await _read_upload_form(request)
    try:
        file = form.get("file")
        declared_type = form.get("assetType")
        if not isinstance(file, UploadFile) or set(form).difference({"file", "assetType"}):
            raise _invalid_form()
        if declared_type is not None and declared_type not in ("Image", "Video", "Audio"):
            raise _invalid_form()
        size = file.size or 0
        if size > MAX_UPLOAD_BYTES:
            raise _too_large()
        media = await run_in_threadpool(inspect_media, file.file, file.content_type, size)
        if declared_type is not None and declared_type != media.asset_type:
            raise _error(415, "media_type_mismatch", "文件内容与声明的素材类型不匹配")
        fingerprint = await run_in_threadpool(_fingerprint, file.file)
        database = request.app.state.db
        row, is_new = await run_in_threadpool(
            _reserve_upload, database, principal, media=media, size=size,
            fingerprint=fingerprint, idempotency_hash=idempotency_hash,
        )
        if not is_new:
            response.status_code = 200
            return _public_upload(row)

        try:
            await run_in_threadpool(
                storage.put, row["object_key"], file.file, size=size, content_type=media.content_type,
            )
        except StorageError:
            await run_in_threadpool(_settle_upload, database, row, None)
            raise _error(502, "upload_storage_failed", "暂存写入失败，请查询清理状态后重试", row["id"]) from None
        saved = await run_in_threadpool(_settle_upload, database, row, settings.upload_ttl_seconds)
        if saved is None:
            raise _error(502, "upload_storage_failed", "暂存写入结果未确认，请查询上传状态", row["id"])
        return _public_upload(saved)
    finally:
        await form.close()


@router.get("/upload-file/{upload_id}")
def get_upload(upload_id: str, request: Request, principal: Principal) -> dict:
    if UPLOAD_ID.fullmatch(upload_id):
        with request.app.state.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM asset_uploads WHERE id = ? AND project_id = ? AND api_key_id = ?",
                (upload_id, principal.project_id, principal.key_id),
            ).fetchone()
        if row is not None:
            if row["bucket"]:
                raise _legacy_upload(upload_id)
            return _public_upload(row)
    raise _error(404, "upload_not_found", "当前业务 Key 没有该上传记录")


async def cleanup_uploads(app: FastAPI) -> None:
    storage: UploadStorage = app.state.upload_storage
    database = app.state.db
    with database.connect() as connection:
        due = connection.execute(
            "SELECT id FROM asset_uploads WHERE bucket = '' AND cleanup_after <= ? ORDER BY cleanup_after LIMIT 100",
            (int(time.time()),),
        ).fetchall()
    for candidate in due:
        now = int(time.time())
        lease = now + CLEANUP_LEASE_SECONDS
        with database.connect() as connection:
            row = connection.execute(
                """
                UPDATE asset_uploads SET cleanup_after = ?, updated_at = ?,
                    status = CASE WHEN status = 'uploading' THEN 'cleanup_pending' ELSE status END
                WHERE id = ? AND bucket = '' AND cleanup_after <= ? RETURNING *
                """,
                (lease, now, candidate["id"], now),
            ).fetchone()
        if row is None:
            continue
        try:
            await run_in_threadpool(storage.delete, row["object_key"])
        except StorageError:
            retry_at = int(time.time()) + CLEANUP_RETRY_SECONDS
            with database.connect() as connection:
                connection.execute(
                    "UPDATE asset_uploads SET cleanup_after = ? WHERE id = ? AND cleanup_after = ?",
                    (retry_at, row["id"], lease),
                )
            logger.warning("Asset upload cleanup will retry for %s", row["id"])
        else:
            with database.connect() as connection:
                connection.execute(
                    """
                    UPDATE asset_uploads SET status = CASE WHEN status = 'staged' THEN 'expired' ELSE 'failed' END,
                        cleanup_after = NULL, updated_at = ?
                    WHERE id = ? AND cleanup_after = ?
                    """,
                    (int(time.time()), row["id"], lease),
                )


async def poll_cleanup(app: FastAPI, seconds: int) -> None:
    while True:
        try:
            await cleanup_uploads(app)
        except Exception:
            logger.error("Asset upload cleanup pass failed")
        await asyncio.sleep(seconds)
