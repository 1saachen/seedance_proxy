import asyncio
import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import billing
from .accounts import BusinessPrincipal, require_business_key
from .assets import is_asset_reference, is_public_https, translate_reference
from .db import Database


router = APIRouter()
logger = logging.getLogger(__name__)
Principal = Annotated[BusinessPrincipal, Depends(require_business_key)]
TASKS_PATH = "/api/v3/contents/generations/tasks"
VARIANT_RESOLUTIONS = ("480p", "720p")
STANDARD_RESOLUTIONS = (*VARIANT_RESOLUTIONS, "1080p")


@dataclass(frozen=True)
class VideoCapabilities:
    max_duration: int
    resolutions: tuple[str, ...]
    audio_only: bool
    frame_ratio_adaptive: bool
    max_reference_images: int = 9
    max_reference_videos: int = 3
    max_reference_audios: int = 3


@dataclass(frozen=True)
class SeedanceModel:
    upstream_id: str
    video: VideoCapabilities


SEEDANCE_20_VARIANT = VideoCapabilities(
    max_duration=15, resolutions=VARIANT_RESOLUTIONS, audio_only=False, frame_ratio_adaptive=False,
)
SEEDANCE_MODELS = {
    "doubao-seedance-2.5": SeedanceModel(
        "doubao-seedance-2-5-260628",
        VideoCapabilities(max_duration=30, resolutions=STANDARD_RESOLUTIONS, audio_only=True, frame_ratio_adaptive=True),
    ),
    "doubao-seedance-2.5-flash": SeedanceModel(
        "doubao-seedance-2-5-flash-261115",
        VideoCapabilities(
            max_duration=30, resolutions=VARIANT_RESOLUTIONS, audio_only=True, frame_ratio_adaptive=True,
            max_reference_images=10, max_reference_videos=5, max_reference_audios=5,
        ),
    ),
    "doubao-seedance-2.0": SeedanceModel(
        "doubao-seedance-2-0-260128",
        VideoCapabilities(max_duration=15, resolutions=STANDARD_RESOLUTIONS, audio_only=False, frame_ratio_adaptive=False),
    ),
    "doubao-seedance-2.0-fast": SeedanceModel("doubao-seedance-2-0-fast-260128", SEEDANCE_20_VARIANT),
    "doubao-seedance-2.0-mini": SeedanceModel("doubao-seedance-2-0-mini-260615", SEEDANCE_20_VARIANT),
}
MAX_CONTENT_ITEMS = 1 + max(
    model.video.max_reference_images + model.video.max_reference_videos + model.video.max_reference_audios
    for model in SEEDANCE_MODELS.values()
)
UPSTREAM_STATES = {
    "queued": "queued",
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "canceled",
    "canceled": "canceled",
    "expired": "expired",
}
ACTIVE_STATES = {"queued", "running"}
TERMINAL_STATES = {"succeeded", "failed", "canceled", "expired"}
RESOLUTIONS = set(STANDARD_RESOLUTIONS)
RATIOS = {"16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"}
UPSTREAM_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
ASSET_MEDIA_TYPES = {"image_url": "Image", "video_url": "Video", "audio_url": "Audio"}


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS video_tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            key_id TEXT NOT NULL REFERENCES business_keys(id),
            idempotency_key TEXT,
            request_hash TEXT NOT NULL,
            model TEXT NOT NULL,
            upstream_id TEXT UNIQUE,
            status TEXT NOT NULL,
            result_json TEXT NOT NULL DEFAULT '{}',
            requested_resolution TEXT,
            contains_video_input INTEGER NOT NULL CHECK (contains_video_input IN (0, 1)),
            output_tokens INTEGER CHECK (output_tokens >= 0),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            settled_at TEXT,
            last_polled_at TEXT,
            upstream_model TEXT,
            UNIQUE(key_id, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS video_tasks_project_settled
            ON video_tasks(project_id, settled_at) WHERE status = 'succeeded';
        CREATE INDEX IF NOT EXISTS video_tasks_project_created ON video_tasks(project_id, created_at);
        CREATE INDEX IF NOT EXISTS video_tasks_pending_poll
            ON video_tasks(COALESCE(last_polled_at, created_at), created_at, id)
            WHERE status IN ('queued', 'running') AND upstream_id IS NOT NULL;
    """)
    if "upstream_model" not in {column[1] for column in connection.execute("PRAGMA table_info(video_tasks)")}:
        connection.execute("ALTER TABLE video_tasks ADD COLUMN upstream_model TEXT")


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MediaURL(ModelInput):
    url: str = Field(min_length=1, max_length=4096)

    @field_validator("url")
    @classmethod
    def valid_media_reference(cls, value: str) -> str:
        if not (is_asset_reference(value) or is_public_https(value)):
            raise ValueError("reference media must use a registered asset reference or a public HTTPS URL")
        return value


class TextContent(ModelInput):
    type: Literal["text"]
    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must contain text")
        return value


class ImageContent(ModelInput):
    type: Literal["image_url"]
    image_url: MediaURL
    role: Literal["first_frame", "last_frame", "reference_image"] = "reference_image"


class VideoContent(ModelInput):
    type: Literal["video_url"]
    video_url: MediaURL
    role: Literal["reference_video"] = "reference_video"


class AudioContent(ModelInput):
    type: Literal["audio_url"]
    audio_url: MediaURL
    role: Literal["reference_audio"] = "reference_audio"


Content = Annotated[TextContent | ImageContent | VideoContent | AudioContent, Field(discriminator="type")]


class VideoRequest(ModelInput):
    model: str = Field(min_length=1, max_length=80)
    content: list[Content] = Field(min_length=1, max_length=MAX_CONTENT_ITEMS)
    duration: int | None = None
    resolution: Literal["480p", "720p", "1080p"] | None = None
    ratio: Literal["16:9", "4:3", "1:1", "3:4", "9:16", "21:9", "adaptive"] | None = None
    seed: int | None = Field(default=None, ge=-1, le=2**32 - 1)
    generate_audio: bool | None = None
    return_last_frame: bool | None = None
    watermark: bool | None = None

    @model_validator(mode="after")
    def validate_request(self):
        model = SEEDANCE_MODELS.get(self.model)
        if model is None:
            raise ValueError("unknown model alias")
        limits = model.video
        if self.duration is not None and self.duration != -1 and not 4 <= self.duration <= limits.max_duration:
            raise ValueError(f"duration must be -1 or from 4 to {limits.max_duration} seconds")
        if self.resolution is not None and self.resolution not in limits.resolutions:
            raise ValueError(f"resolution must be one of {', '.join(limits.resolutions)} for {self.model}")
        kinds = [item.type for item in self.content]
        for kind, count in (
            ("text", 1),
            ("image_url", limits.max_reference_images),
            ("video_url", limits.max_reference_videos),
            ("audio_url", limits.max_reference_audios),
        ):
            if kinds.count(kind) > count:
                raise ValueError(f"too many {kind} content items")
        frames = [item.role for item in self.content if isinstance(item, ImageContent)]
        if {"first_frame", "last_frame"} & set(frames):
            if frames not in (["first_frame"], ["first_frame", "last_frame"], ["last_frame", "first_frame"]) or {"video_url", "audio_url"} & set(kinds):
                raise ValueError("frame generation needs one first frame and may include one last frame")
            if limits.frame_ratio_adaptive and self.ratio not in (None, "adaptive"):
                raise ValueError(f"{self.model} frame generation uses the adaptive ratio")
        if not limits.audio_only and "audio_url" in kinds and not {"image_url", "video_url"} & set(kinds):
            raise ValueError(f"{self.model} requires a reference image or video with reference audio")
        return self


def _error(http_status: int, code: str, message: str, **details) -> HTTPException:
    return HTTPException(http_status, {"code": code, "message": message, **details})


def _ark_key(app: FastAPI) -> str | None:
    key = app.state.settings.ark_api_key
    value = key.get_secret_value().strip() if key is not None else ""
    return value if re.fullmatch(r"[\x21-\x7e]{1,512}", value) else None


def _ark_headers(app: FastAPI) -> dict[str, str]:
    value = _ark_key(app)
    if value is None:
        raise _error(503, "provider_unavailable", "Seedance is not configured")
    return {"Authorization": f"Bearer {value}"}


def _task(request: Request, principal: BusinessPrincipal, task_id: str) -> sqlite3.Row:
    with request.app.state.db.connect() as connection:
        row = connection.execute(
            "SELECT * FROM video_tasks WHERE id = ? AND project_id = ? AND key_id = ?",
            (task_id, principal.project_id, principal.key_id),
        ).fetchone()
    if row is None:
        raise _error(404, "video_task_not_found", "Video task was not found")
    return row


def _public_task(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "model": row["model"],
        "status": "cancelled" if row["status"] == "canceled" else row["status"],
        **json.loads(row["result_json"]),
    }


def _response_object(response: httpx.Response) -> dict:
    try:
        value = response.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _submission_error(status: str, task_id: str) -> HTTPException:
    if status == "submission_failed":
        return _error(502, "video_submission_failed", "Seedance rejected the task", task_id=task_id)
    return _error(502, "video_submission_unknown", "Seedance submission was not confirmed", task_id=task_id)


@router.get("/v1/models")
def list_models(request: Request, principal: Principal):
    models = SEEDANCE_MODELS if _ark_key(request.app) else {}
    return {
        "object": "list",
        "data": [
            {"id": alias, "object": "model", "owned_by": "volcengine", "video": asdict(model.video)}
            for alias, model in models.items()
        ],
    }


@router.post(TASKS_PATH)
async def create_task(
    request: Request,
    principal: Principal,
    payload: VideoRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    if idempotency_key is not None and not re.fullmatch(r"[\x21-\x7e]{1,128}", idempotency_key):
        raise _error(422, "idempotency_key_invalid", "Idempotency-Key must contain 1 to 128 visible ASCII characters")
    clean = payload.model_dump(exclude_none=True)
    fingerprint = hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    task_id = f"vid_{uuid4().hex}"
    idempotency_hash = hashlib.sha256(idempotency_key.encode()).hexdigest() if idempotency_key is not None else None
    if idempotency_hash is not None:
        with request.app.state.db.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM video_tasks WHERE project_id = ? AND key_id = ? AND idempotency_key = ?",
                (principal.project_id, principal.key_id, idempotency_hash),
            ).fetchone()
        if existing is not None:
            return _replay(existing, fingerprint)
    headers = _ark_headers(request.app)
    upstream_model = SEEDANCE_MODELS[payload.model].upstream_id
    with request.app.state.db.connect() as connection:
        result = connection.execute(
            """INSERT INTO video_tasks
                (id, project_id, key_id, idempotency_key, request_hash, model, status,
                 requested_resolution, contains_video_input, upstream_model)
                SELECT ?, ?, ?, ?, ?, ?, 'submitting', ?, ?, ?
                WHERE (SELECT COUNT(*) FROM video_tasks WHERE project_id = ?
                       AND created_at >= DATETIME('now', '+8 hours', 'start of day', '-8 hours')) < ?
                ON CONFLICT(key_id, idempotency_key) DO NOTHING""",
            (task_id, principal.project_id, principal.key_id, idempotency_hash, fingerprint,
             payload.model, payload.resolution, any(isinstance(item, VideoContent) for item in payload.content),
             upstream_model, principal.project_id, request.app.state.settings.video_daily_limit),
        )
        if result.rowcount == 0:
            existing = connection.execute(
                "SELECT * FROM video_tasks WHERE project_id = ? AND key_id = ? AND idempotency_key = ?",
                (principal.project_id, principal.key_id, idempotency_hash),
            ).fetchone()
            if existing is not None:
                return _replay(existing, fingerprint)
            raise _error(429, "video_daily_limit", "Project reached its daily video submission limit")
        for item in clean["content"]:
            kind = item["type"]
            if kind == "text" or not is_asset_reference(item[kind]["url"]):
                continue
            item[kind]["url"] = translate_reference(
                connection, principal.project_id, item[kind]["url"], media_type=ASSET_MEDIA_TYPES[kind],
                upstream_project=request.app.state.settings.ark_asset_project,
            )
    clean["model"] = upstream_model
    upstream_id = None
    status = "submission_unknown"
    try:
        response = await request.app.state.ark.post(TASKS_PATH, json=clean, headers=headers)
        if response.is_success:
            candidate = _response_object(response).get("id")
            if isinstance(candidate, str) and UPSTREAM_ID.fullmatch(candidate):
                upstream_id, status = candidate, "queued"
        elif 400 <= response.status_code < 500 and response.status_code != 408:
            status = "submission_failed"
    except httpx.RequestError:
        pass
    with request.app.state.db.connect() as connection:
        try:
            connection.execute("UPDATE video_tasks SET status = ?, upstream_id = ? WHERE id = ?", (status, upstream_id, task_id))
        except sqlite3.IntegrityError:
            # The same provider task cannot be assigned to a second business Key or charged twice.
            upstream_id, status = None, "submission_unknown"
            connection.execute("UPDATE video_tasks SET status = ? WHERE id = ?", (status, task_id))
    if upstream_id is None:
        raise _submission_error(status, task_id)
    return {"id": task_id}


def _replay(row: sqlite3.Row, fingerprint: str) -> dict:
    if row["request_hash"] != fingerprint:
        raise _error(409, "idempotency_conflict", "Idempotency-Key was already used with a different request")
    if row["upstream_id"]:
        return {"id": row["id"]}
    if row["status"] == "submitting":
        raise _error(409, "video_submission_pending", "Submission has not been confirmed", task_id=row["id"])
    raise _submission_error(row["status"], row["id"])


def _provider_result(data: dict, status: str) -> tuple[dict, int | None]:
    result = {}
    for field, allowed in (("resolution", RESOLUTIONS), ("ratio", RATIOS)):
        value = data.get(field)
        if isinstance(value, str) and value in allowed:
            result[field] = value
    duration = data.get("duration")
    if type(duration) is int and 0 < duration <= 30:
        result["duration"] = duration
    if status == "succeeded":
        content = data.get("content")
        if isinstance(content, dict):
            urls = {
                field: content[field] for field in ("video_url", "last_frame_url")
                if isinstance(content.get(field), str) and is_public_https(content[field])
            }
            if urls:
                result["content"] = urls
        usage = data.get("usage")
        if isinstance(usage, dict):
            tokens = {field: usage[field] for field in ("completion_tokens", "total_tokens")
                      if type(usage.get(field)) is int and 0 <= usage[field] <= billing.MAX_TOKENS}
            if tokens:
                result["usage"] = tokens
            return result, tokens.get("completion_tokens")
    if status == "failed":
        result["error"] = {"code": "video_generation_failed", "message": "Seedance could not generate the video"}
    return result, None


def _provider_settlement_time(data: dict, created_at: str) -> str | None:
    updated_at = data.get("updated_at")
    now = datetime.now(timezone.utc)
    if type(updated_at) is not int or not 0 < updated_at <= now.timestamp() + 60:
        return None
    updated = datetime.fromtimestamp(updated_at, tz=timezone.utc)
    created = datetime.fromisoformat(created_at).replace(tzinfo=timezone.utc)
    if updated < created - timedelta(minutes=1):
        return None
    return updated.strftime("%Y-%m-%d %H:%M:%S")


def _record_charge(database: Database, settled: sqlite3.Row) -> None:
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        billing.record_task(connection, settled)


async def _refresh_task(app: FastAPI, row: sqlite3.Row, headers: dict[str, str]) -> None:
    with app.state.db.connect() as connection:
        connection.execute("UPDATE video_tasks SET last_polled_at = CURRENT_TIMESTAMP WHERE id = ?", (row["id"],))
    try:
        response = await app.state.ark.get(f"{TASKS_PATH}/{row['upstream_id']}", headers=headers)
    except httpx.RequestError:
        raise _error(502, "video_status_unavailable", "Seedance task status is temporarily unavailable") from None
    data = _response_object(response) if response.is_success else {}
    upstream_status = data.get("status")
    status = UPSTREAM_STATES.get(upstream_status) if isinstance(upstream_status, str) else None
    if status is None or ("id" in data and data["id"] != row["upstream_id"]):
        raise _error(502, "video_status_unavailable", "Seedance task status is temporarily unavailable")
    result, output_tokens = _provider_result(data, status)
    settled_at = None
    if status in TERMINAL_STATES:
        settled_at = _provider_settlement_time(data, row["created_at"])
        if status != "succeeded" and settled_at is None:
            settled_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with app.state.db.connect() as connection:
        settled = connection.execute(
            """UPDATE video_tasks
               SET status = ?, result_json = ?, output_tokens = ?, settled_at = ?
               WHERE id = ? AND (status = 'queued' OR (status = 'running' AND ? <> 'queued')) RETURNING *""",
            (status, json.dumps(result), output_tokens, settled_at, row["id"], status),
        ).fetchone()
    if settled is not None and status == "succeeded":
        try:
            await asyncio.to_thread(_record_charge, app.state.db, settled)
        except Exception:
            # Settlement remains available. The billing reconciliation pass can replay it.
            logger.warning("Could not record the charge for video task %s", row["id"])


async def poll_unfinished(app: FastAPI) -> int:
    """Refresh a bounded batch without blocking customer polling or resubmitting tasks."""
    try:
        headers = _ark_headers(app)
    except HTTPException:
        return 0
    with app.state.db.connect() as connection:
        pending = connection.execute(
            "SELECT * FROM video_tasks WHERE status IN ('queued', 'running') AND upstream_id IS NOT NULL "
            "ORDER BY COALESCE(last_polled_at, created_at), created_at, id LIMIT 20"
        ).fetchall()

    async def refresh(row: sqlite3.Row) -> None:
        try:
            await _refresh_task(app, row, headers)
        except HTTPException:
            pass  # Leave the current state intact so the next pass can retry the lookup.

    for offset in range(0, len(pending), 4):
        await asyncio.gather(*(refresh(row) for row in pending[offset:offset + 4]))
    return len(pending)


@router.get(TASKS_PATH + "/{task_id}")
async def get_task(request: Request, principal: Principal, task_id: str):
    row = _task(request, principal, task_id)
    if row["status"] not in ACTIVE_STATES:
        return _public_task(row)
    await _refresh_task(request.app, row, _ark_headers(request.app))
    return _public_task(_task(request, principal, task_id))
