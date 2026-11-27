import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import posixpath
import re
import sqlite3
import time
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from anyio import CancelScope, create_task_group
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.routing import APIRoute
from fastapi.responses import JSONResponse, Response, StreamingResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from . import key_limits
from .accounts import (
    BusinessPrincipal, CurrentCustomer, authenticate_business_key, business_bearer, require_business_key,
)
from .db import Database
from .urls import is_public_https


TEXT_MODELS = {
    "doubao-seed-2.1-lite": "doubao-seed-2-1-lite-260915",
    "doubao-seed-2.1-pro": "doubao-seed-2-1-pro-260915",
}
IMAGE_MODELS = {
    "doubao-seedream-5.5": "doubao-seedream-5-5-261028",
    "doubao-seedream-5.0-pro": "doubao-seedream-5-0-pro-260628",
}
CHAT_PATH = "/api/v3/chat/completions"
IMAGE_PATH = "/api/v3/images/generations"
MAX_BODY_BYTES = 1024 * 1024
MAX_CHAT_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_RESPONSE_BYTES = 256 * 1024
MAX_USAGE = 10**12
IMAGE_RESULT_TTL = 24 * 60 * 60
REQUEST_LIFETIME = key_limits.GENERAL_REQUEST_LIFETIME_SECONDS
UPSTREAM_TIMEOUT = httpx.Timeout(90, connect=5)
SHANGHAI = timezone(timedelta(hours=8))
PRIVATE_HEADERS = {"Cache-Control": "no-store"}
LIMIT_RESPONSES = {429: {"description": "The API Key concurrency limit or a request rate limit was reached"}}
Principal = Annotated[BusinessPrincipal, Depends(require_business_key)]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]
ModelType = Literal["text", "image"]
OutputFormat = Literal["png", "jpeg"]


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


class _BoundedRequest(Request):
    async def stream(self):
        received = 0
        async for chunk in super().stream():
            received += len(chunk)
            if received > MAX_BODY_BYTES:
                raise _error(413, "model_request_too_large", "Model request exceeds 1 MiB")
            yield chunk


class GeneralModelRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request: Request):
            if request.method == "POST":
                credentials = await business_bearer(request)
                await run_in_threadpool(authenticate_business_key, request, credentials)
                request = _BoundedRequest(request.scope, request.receive)
            return await handler(request)

        return handle


router = APIRouter(route_class=GeneralModelRoute)


def list_public_models() -> list[dict]:
    return [
        {"id": alias, "object": "model", "owned_by": "volcengine", "type": kind}
        for kind, entries in (("text", TEXT_MODELS), ("image", IMAGE_MODELS))
        for alias in entries
    ]


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS general_model_calls (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            key_id TEXT NOT NULL REFERENCES business_keys(id),
            modality TEXT NOT NULL CHECK (modality IN ('text', 'image')),
            model TEXT NOT NULL,
            upstream_model TEXT NOT NULL,
            idempotency_hash TEXT,
            request_hash TEXT,
            status TEXT NOT NULL CHECK (status IN ('in_progress', 'succeeded', 'failed', 'uncertain')),
            error_status INTEGER,
            error_code TEXT,
            error_message TEXT,
            prompt_tokens INTEGER CHECK (prompt_tokens >= 0),
            completion_tokens INTEGER CHECK (completion_tokens >= 0),
            total_tokens INTEGER CHECK (total_tokens >= 0),
            generated_images INTEGER CHECK (generated_images >= 0),
            created_at INTEGER NOT NULL,
            cached_result_json TEXT,
            cached_result_expires_at INTEGER,
            UNIQUE(key_id, modality, idempotency_hash)
        );
        CREATE INDEX IF NOT EXISTS general_model_calls_project_time
            ON general_model_calls(project_id, created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS general_model_calls_active
            ON general_model_calls(created_at) WHERE status = 'in_progress';
        CREATE INDEX IF NOT EXISTS general_model_calls_active_by_key
            ON general_model_calls(key_id, modality, created_at) WHERE status = 'in_progress';
        CREATE INDEX IF NOT EXISTS general_model_calls_cached_results
            ON general_model_calls(cached_result_expires_at) WHERE cached_result_json IS NOT NULL;
    """)
    connection.execute("BEGIN IMMEDIATE")
    columns = {column[1] for column in connection.execute("PRAGMA table_info(general_model_calls)")}
    for name, definition in (
        ("provider_request_id", "TEXT"),
        ("cached_prompt_tokens", "INTEGER CHECK (cached_prompt_tokens >= 0)"),
        ("input_images", "INTEGER CHECK (input_images >= 0)"),
        ("image_sizes_json", "TEXT"),
        ("settled_at", "INTEGER CHECK (settled_at >= 0)"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE general_model_calls ADD COLUMN {name} {definition}")
    connection.execute("""
        CREATE INDEX IF NOT EXISTS general_model_calls_provider_request
        ON general_model_calls(provider_request_id) WHERE provider_request_id IS NOT NULL
    """)
    connection.execute("""
        CREATE INDEX IF NOT EXISTS general_model_calls_settled
        ON general_model_calls(settled_at) WHERE status = 'succeeded'
    """)


class ModelInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChatMessage(ModelInput):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=32768)

    @field_validator("content")
    @classmethod
    def meaningful_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Message cannot be blank")
        return value


class StreamOptions(ModelInput):
    include_usage: bool = False


class ThinkingOptions(ModelInput):
    type: Literal["enabled", "disabled"]


class ChatRequest(ModelInput):
    model: Literal["doubao-seed-2.1-lite", "doubao-seed-2.1-pro"]
    messages: list[ChatMessage] = Field(min_length=1, max_length=100)
    stream: bool = False
    stream_options: StreamOptions | None = None
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(default=None, gt=0, le=1, allow_inf_nan=False)
    max_tokens: int | None = Field(default=None, ge=1, le=65536)
    max_completion_tokens: int | None = Field(default=None, ge=1, le=65536)
    thinking: ThinkingOptions | None = None
    n: Literal[1] | None = None

    @model_validator(mode="after")
    def validate_chat(self):
        if not any(message.role == "user" for message in self.messages):
            raise ValueError("At least one user message is required")
        if self.stream_options is not None and not self.stream:
            raise ValueError("stream_options requires stream=true")
        if self.max_tokens is not None and self.max_completion_tokens is not None:
            raise ValueError("Choose either max_tokens or max_completion_tokens")
        return self


class ImageRequest(ModelInput):
    model: Literal["doubao-seedream-5.5", "doubao-seedream-5.0-pro"]
    prompt: str = Field(min_length=1, max_length=8000)
    image: str | Annotated[list[str], Field(min_length=1, max_length=10)] | None = None
    size: str | None = Field(default=None, pattern=r"^(?:1K|1\.5K|2K|[1-9]\d{0,5}x[1-9]\d{0,5})$")
    response_format: Literal["url"] = "url"
    output_format: OutputFormat | None = None
    watermark: bool | None = None
    n: Literal[1] | None = None

    @model_validator(mode="after")
    def validate_image(self):
        if not self.prompt.strip():
            raise ValueError("Prompt cannot be blank")
        if self.image is not None:
            images = [self.image] if isinstance(self.image, str) else self.image
            for url in images:
                if len(url) > 4096 or not is_public_https(url):
                    raise ValueError("Use public HTTPS reference images")
                path = posixpath.normpath("/" + unquote(urlsplit(url).path).lstrip("/")).casefold()
                if path == "/api/asset/source" or path.startswith("/api/asset/source/"):
                    raise ValueError("Private asset source URLs are not image references")
        if self.size is not None and "x" in self.size:
            width, height = map(int, self.size.split("x"))
            maximum = 8_294_400 if self.model == "doubao-seedream-5.5" else 4_624_220
            if not 921_600 <= width * height <= maximum or max(width, height) > 16 * min(width, height):
                raise ValueError("Image size is outside the supported range for this model")
        return self


@dataclass(frozen=True)
class ModelUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    generated_images: int | None = None
    cached_prompt_tokens: int | None = None
    input_images: int | None = None


def _known_counts(value: object, names: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        name: amount for name in names
        if type(amount := value.get(name)) is int and 0 <= amount <= MAX_USAGE
    }


def _usage_fields(value: dict) -> ModelUsage:
    return ModelUsage(*(value.get(name) for name in (
        "prompt_tokens", "completion_tokens", "total_tokens", "generated_images", "cached_prompt_tokens", "input_images",
    )))


def _cache_usage(raw: dict, reported: dict) -> int | None:
    usage = raw.get("usage")
    details = usage.get("prompt_tokens_details") if isinstance(usage, dict) else None
    cached = _known_counts(details, ("cached_tokens",)).get("cached_tokens")
    prompt = reported.get("prompt_tokens")
    return cached if cached is not None and (prompt is None or cached <= prompt) else None


def _provider_request_id(raw: dict) -> str | None:
    value = raw.get("id")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) else None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _provider_headers(request: Request, *, stream: bool = False) -> dict[str, str]:
    secret = request.app.state.settings.ark_api_key
    credential = secret.get_secret_value().strip() if secret is not None else ""
    if not re.fullmatch(r"[\x21-\x7e]{1,512}", credential):
        raise _error(503, "provider_unavailable", "Model provider is not configured")
    return {"Authorization": f"Bearer {credential}", "Accept": "text/event-stream" if stream else "application/json"}


def _reserve(
    database: Database, principal: BusinessPrincipal, kind: ModelType, model: str, upstream_model: str,
    *, idempotency_key: str | None = None, fingerprint: str | None = None,
) -> tuple[str, int, sqlite3.Row | None]:
    request_id = ("chatcmpl-" if kind == "text" else "img_") + uuid4().hex
    key_hash = _digest(idempotency_key) if idempotency_key is not None else None
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        now = int(time.time())
        connection.execute("""
            UPDATE general_model_calls SET status = 'uncertain'
            WHERE status = 'in_progress' AND created_at <= ?
        """, (key_limits.general_request_cutoff(now),))
        connection.execute("""
            UPDATE general_model_calls SET cached_result_json = NULL
            WHERE cached_result_json IS NOT NULL AND cached_result_expires_at <= ?
        """, (now,))
        if key_hash is not None:
            existing = connection.execute("""
                SELECT * FROM general_model_calls
                WHERE project_id = ? AND key_id = ? AND modality = ? AND idempotency_hash = ?
            """, (principal.project_id, principal.key_id, kind, key_hash)).fetchone()
            if existing is not None:
                return request_id, now, existing
        key_limits.enforce_concurrency(connection, principal.key_id)
        connection.execute("""
            INSERT INTO general_model_calls (
                id, project_id, key_id, modality, model, upstream_model, idempotency_hash, request_hash, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?)
        """, (request_id, principal.project_id, principal.key_id, kind, model, upstream_model, key_hash, fingerprint, now))
    return request_id, now, None


def _finish(
    database: Database, request_id: str, *, status: Literal["succeeded", "failed", "uncertain"],
    usage: ModelUsage = ModelUsage(), cache: dict | None = None, cache_created_at: int | None = None,
    error: HTTPException | None = None,
    provider_request_id: str | None = None, image_sizes: tuple[str, ...] | None = None,
) -> None:
    with database.connect() as connection:
        connection.execute("""
            UPDATE general_model_calls SET status = ?, error_status = ?, error_code = ?, error_message = ?,
                prompt_tokens = ?, completion_tokens = ?, total_tokens = ?, generated_images = ?,
                cached_result_json = ?, cached_result_expires_at = ?,
                provider_request_id = ?, cached_prompt_tokens = ?, input_images = ?, image_sizes_json = ?, settled_at = ?
            WHERE id = ? AND status IN ('in_progress', 'uncertain')
        """, (
            status, error.status_code if error else None, error.detail["code"] if error else None,
            error.detail["message"] if error else None, usage.prompt_tokens, usage.completion_tokens,
            usage.total_tokens, usage.generated_images,
            json.dumps(cache, separators=(",", ":")) if cache is not None else None,
            cache_created_at + IMAGE_RESULT_TTL if cache_created_at is not None else None,
            provider_request_id, usage.cached_prompt_tokens, usage.input_images,
            json.dumps(image_sizes, separators=(",", ":")) if image_sizes is not None else None,
            int(time.time()) if status == "succeeded" else None, request_id,
        ))


def _mark_abandoned(database: Database, request_id: str, provider_request_id: str | None) -> None:
    with database.connect() as connection:
        connection.execute(
            "UPDATE general_model_calls SET status = 'uncertain', provider_request_id = ? "
            "WHERE id = ? AND status = 'in_progress'",
            (provider_request_id, request_id),
        )


async def _close_upstream(upstream: httpx.Response) -> None:
    with CancelScope(shield=True):
        await upstream.aclose()


async def _release_abandoned(
    database: Database, request_id: str, upstream: httpx.Response | None = None,
    *, provider_request_id: str | None = None,
) -> None:
    with CancelScope(shield=True):
        try:
            if upstream is not None:
                await upstream.aclose()
        finally:
            await run_in_threadpool(_mark_abandoned, database, request_id, provider_request_id)


@asynccontextmanager
async def _release_on_error(database: Database, request_id: str):
    try:
        yield
    except HTTPException:
        # Expected provider errors are already recorded after closing the upstream response.
        raise
    except (Exception, asyncio.CancelledError):
        await _release_abandoned(database, request_id)
        raise


def _replay_image(existing: sqlite3.Row, fingerprint: str) -> JSONResponse:
    if existing["request_hash"] != fingerprint:
        raise _error(409, "idempotency_conflict", "Idempotency-Key was already used with a different image request")
    if existing["status"] == "succeeded":
        if not existing["cached_result_json"]:
            raise _error(409, "image_result_expired", "The cached image result has expired")
        return JSONResponse(json.loads(existing["cached_result_json"]), headers=PRIVATE_HEADERS)
    if existing["status"] == "failed":
        raise _error(existing["error_status"], existing["error_code"], existing["error_message"])
    if existing["status"] == "in_progress":
        raise _error(409, "image_request_pending", "The previous image request is still in progress")
    raise _error(409, "image_request_unknown", "The previous image request result could not be confirmed")


def _provider_failure(status: int) -> HTTPException:
    if status in (401, 403):
        return _error(503, "provider_unavailable", "Model access is unavailable at the provider")
    if status == 429:
        return _error(429, "provider_rate_limited", "The provider reached its request limit")
    if 400 <= status < 500 and status != 408:
        return _error(422, "provider_rejected", "The provider rejected the model request")
    return _error(502, "model_result_unknown", "The provider result could not be confirmed")


def _invalid_provider_response() -> HTTPException:
    return _error(502, "model_result_unknown", "The provider result could not be confirmed")


async def _upstream(
    request: Request, path: str, payload: dict, headers: dict[str, str], request_id: str, deadline: float,
) -> httpx.Response:
    client = request.app.state.ark
    try:
        async with asyncio.timeout_at(deadline):
            outbound = client.build_request(
                "POST", path, json=payload, headers={**headers, "X-Client-Request-Id": request_id},
                timeout=UPSTREAM_TIMEOUT,
            )
            response = await client.send(outbound, stream=True)
    except (TimeoutError, httpx.RequestError):
        error = _invalid_provider_response()
        await run_in_threadpool(_finish, request.app.state.db, request_id, status="uncertain", error=error)
        raise error from None
    if response.is_success:
        return response
    await _close_upstream(response)
    error = _provider_failure(response.status_code)
    definite_failure = 400 <= response.status_code < 500 and response.status_code != 408
    await run_in_threadpool(
        _finish, request.app.state.db, request_id, status="failed" if definite_failure else "uncertain", error=error,
    )
    raise error


async def _read_json(response: httpx.Response, limit: int, deadline: float) -> dict:
    try:
        async with asyncio.timeout_at(deadline):
            content = bytearray()
            async for chunk in response.aiter_bytes(8192):
                if len(content) + len(chunk) > limit:
                    raise ValueError("Provider response exceeds the limit")
                content.extend(chunk)
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError("Invalid provider response")
            return value
    finally:
        await _close_upstream(response)


def _result_envelope(model: str, request_id: str, created: int, *, stream: bool) -> dict:
    return {
        "id": request_id, "model": model, "created": created,
        "object": "chat.completion.chunk" if stream else "chat.completion",
    }


def _chat_choices(value: object, *, stream: bool) -> list[dict]:
    if not isinstance(value, list) or len(value) > 1:
        raise ValueError("Unexpected chat choices")
    results = []
    field = "delta" if stream else "message"
    for choice in value:
        if not isinstance(choice, dict) or not isinstance(choice.get(field), dict):
            raise ValueError("Invalid chat choice")
        original = choice[field]
        cleaned = {}
        if original.get("role") == "assistant" or not stream:
            cleaned["role"] = "assistant"
        if "content" in original:
            content = original["content"]
            if content is not None and not isinstance(content, str):
                raise ValueError("Invalid chat text")
            cleaned["content"] = content
        if isinstance(original.get("reasoning_content"), str):
            cleaned["reasoning_content"] = original["reasoning_content"]
        if isinstance(original.get("refusal"), str):
            cleaned["refusal"] = original["refusal"]
        finish = choice.get("finish_reason")
        results.append({
            "index": 0, field: cleaned,
            "finish_reason": finish if finish in ("stop", "length", "content_filter", "tool_calls") else None,
        })
    return results


_CHAT_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
_IMAGE_USAGE_KEYS = ("generated_images", "input_images", "output_tokens", "total_tokens")


def _chat_response(raw: dict, envelope: dict, *, stream: bool) -> tuple[dict, dict[str, int]]:
    choices = _chat_choices(raw.get("choices"), stream=stream)
    if not stream and not choices:
        raise ValueError("A completed chat needs a choice")
    usage = _known_counts(raw.get("usage"), _CHAT_USAGE_KEYS)
    return {**envelope, "choices": choices, **({"usage": usage} if usage else {})}, usage


async def _sse_lines(response: httpx.Response) -> AsyncIterator[bytes]:
    active = bytearray()
    received = 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > MAX_CHAT_RESPONSE_BYTES:
            raise ValueError("Chat stream exceeds the limit")
        active.extend(chunk)
        while (position := active.find(b"\n")) != -1:
            yield bytes(active[:position]).removesuffix(b"\r")
            del active[:position + 1]
    if active:
        yield bytes(active).removesuffix(b"\r")


async def _cancel_on_disconnect(receive: Receive, pending: CancelScope) -> None:
    while (await receive())["type"] != "http.disconnect":
        pass
    pending.cancel()


async def _open_chat_stream(
    request: Request, parameters: dict, headers: dict[str, str], request_id: str, deadline: float,
) -> httpx.Response | None:
    upstream = None
    error = None
    async with create_task_group() as group:
        group.start_soon(_cancel_on_disconnect, request.receive, group.cancel_scope)
        try:
            upstream = await _upstream(request, CHAT_PATH, parameters, headers, request_id, deadline)
        except HTTPException as exc:
            error = exc
        finally:
            group.cancel_scope.cancel()
    if error is not None:
        raise error
    return upstream


class _TrackedChatResponse(StreamingResponse):
    def __init__(
        self, upstream: httpx.Response, database: Database, envelope: dict, include_usage: bool, deadline: float,
    ) -> None:
        self._upstream = upstream
        self._database = database
        self._request_id = envelope["id"]
        self._settled = False
        self._upstream_ids: set[str] = set()
        self._chunks = self._stream(envelope, include_usage, deadline)
        super().__init__(
            self._chunks, media_type="text/event-stream", headers={**PRIVATE_HEADERS, "X-Accel-Buffering": "no"},
        )

    def _known_provider_id(self) -> str | None:
        return next(iter(self._upstream_ids)) if len(self._upstream_ids) == 1 else None

    async def _stream(
        self, envelope: dict, include_usage: bool, deadline: float,
    ) -> AsyncGenerator[bytes, None]:
        upstream = self._upstream
        database = self._database
        request_id = self._request_id
        usage = {}
        saw_choice = False
        try:
            async with asyncio.timeout_at(deadline):
                async for line in _sse_lines(upstream):
                    if not line.startswith(b"data:"):
                        continue
                    data = line.removeprefix(b"data:").strip()
                    if data == b"[DONE]":
                        if not saw_choice:
                            raise ValueError("A completed chat stream needs a choice")
                        await run_in_threadpool(
                            _finish, database, request_id, status="succeeded", usage=_usage_fields(usage),
                            provider_request_id=self._known_provider_id(),
                        )
                        self._settled = True
                        yield b"data: [DONE]\n\n"
                        return
                    raw = json.loads(data)
                    if not isinstance(raw, dict):
                        raise ValueError("Invalid stream object")
                    upstream_id = _provider_request_id(raw)
                    if upstream_id is not None and len(self._upstream_ids) < 2:
                        self._upstream_ids.add(upstream_id)
                    chunk, reported = _chat_response(raw, envelope, stream=True)
                    if reported:
                        usage.update(reported)
                        cached = _cache_usage(raw, usage)
                        if cached is not None:
                            usage["cached_prompt_tokens"] = cached
                    if chunk["choices"]:
                        saw_choice = True
                    elif not (include_usage and reported):
                        continue
                    if not include_usage:
                        chunk.pop("usage", None)
                    yield b"data: " + json.dumps(chunk, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n\n"
            raise ValueError("Provider closed chat before its terminal event")
        except (TimeoutError, ValueError, httpx.RequestError):
            await _release_abandoned(database, request_id, upstream, provider_request_id=self._known_provider_id())
            self._settled = True
            yield b'data: {"error":{"code":"model_result_unknown","message":"The provider did not finish the chat"}}\n\n'
            yield b"data: [DONE]\n\n"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            # Listen even when the server supports send errors, since a paused provider may send no chunks.
            async with create_task_group() as group:
                group.start_soon(_cancel_on_disconnect, receive, group.cancel_scope)
                try:
                    await self.stream_response(send)
                except OSError:
                    pass  # A disconnected client can also be reported by the ASGI sender.
                finally:
                    group.cancel_scope.cancel()
        finally:
            # ASGI may disconnect before the generator starts, or while sending an already-yielded chunk.
            with CancelScope(shield=True):
                try:
                    await self._chunks.aclose()
                finally:
                    if self._settled:
                        await self._upstream.aclose()
                    else:
                        await _release_abandoned(
                            self._database, self._request_id, self._upstream, provider_request_id=self._known_provider_id(),
                        )


@router.post("/v1/chat/completions", tags=["Text models"], responses=LIMIT_RESPONSES)
async def chat_completions(payload: ChatRequest, request: Request, principal: Principal):
    model = payload.model
    upstream_model = TEXT_MODELS[model]
    headers = _provider_headers(request, stream=payload.stream)
    parameters = payload.model_dump(exclude_none=True, exclude={"n", "stream_options"})
    parameters["model"] = upstream_model
    if payload.stream:
        # Meter usage on the server even when the caller does not request an SSE usage frame.
        parameters["stream_options"] = {"include_usage": True}
    database = request.app.state.db
    deadline = asyncio.get_running_loop().time() + REQUEST_LIFETIME
    request_id, created, _ = await run_in_threadpool(_reserve, database, principal, "text", model, upstream_model)
    envelope = _result_envelope(model, request_id, created, stream=payload.stream)
    async with _release_on_error(database, request_id):
        if payload.stream:
            upstream = await _open_chat_stream(request, parameters, headers, request_id, deadline)
            if upstream is None:
                await _release_abandoned(database, request_id)
                return Response(status_code=499, headers=PRIVATE_HEADERS)
            if upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "text/event-stream":
                await _close_upstream(upstream)
                error = _invalid_provider_response()
                await run_in_threadpool(_finish, database, request_id, status="uncertain", error=error)
                raise error
            send_usage = payload.stream_options is not None and payload.stream_options.include_usage
            return _TrackedChatResponse(upstream, database, envelope, send_usage, deadline)
        upstream = await _upstream(request, CHAT_PATH, parameters, headers, request_id, deadline)
        try:
            raw = await _read_json(upstream, MAX_CHAT_RESPONSE_BYTES, deadline)
            body, reported = _chat_response(raw, envelope, stream=False)
        except (TimeoutError, ValueError, httpx.RequestError):
            error = _invalid_provider_response()
            await run_in_threadpool(_finish, database, request_id, status="uncertain", error=error)
            raise error from None
        await run_in_threadpool(
            _finish, database, request_id, status="succeeded",
            usage=_usage_fields({**reported, "cached_prompt_tokens": _cache_usage(raw, reported)}),
            provider_request_id=_provider_request_id(raw),
        )
        return JSONResponse(body, headers=PRIVATE_HEADERS)


def _image_response(raw: dict, model: str, request_id: str, created: int) -> tuple[dict, dict[str, int]]:
    items = raw.get("data")
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        raise ValueError("Invalid image result")
    images = []
    for item in items:
        url = item.get("url") if isinstance(item, dict) else None
        if not isinstance(url, str) or len(url) > 4096 or not is_public_https(url):
            raise ValueError("Invalid image URL")
        result = {"url": url}
        size = item.get("size")
        if isinstance(size, str) and re.fullmatch(r"[1-9]\d{0,5}x[1-9]\d{0,5}", size):
            result["size"] = size
        if item.get("output_format") in ("png", "jpeg"):
            result["output_format"] = item["output_format"]
        images.append(result)
    usage = _known_counts(raw.get("usage"), _IMAGE_USAGE_KEYS)
    return {
        "id": request_id, "model": model, "created": created, "data": images, **({"usage": usage} if usage else {}),
    }, usage


@router.post("/v1/images/generations", tags=["Image models"], responses=LIMIT_RESPONSES)
async def generate_image(
    payload: ImageRequest, request: Request, principal: Principal, idempotency_key: IdempotencyHeader = None,
):
    if idempotency_key is not None and not re.fullmatch(r"[\x21-\x7e]{1,128}", idempotency_key):
        raise _error(422, "idempotency_key_invalid", "Idempotency-Key must contain 1 to 128 visible ASCII characters")
    parameters = payload.model_dump(exclude_none=True, exclude={"n"})
    fingerprint = _digest(json.dumps(parameters, sort_keys=True, separators=(",", ":")))
    model = payload.model
    upstream_model = IMAGE_MODELS[model]
    headers = _provider_headers(request)
    database = request.app.state.db
    deadline = asyncio.get_running_loop().time() + REQUEST_LIFETIME
    request_id, created, existing = await run_in_threadpool(
        _reserve, database, principal, "image", model, upstream_model,
        idempotency_key=idempotency_key, fingerprint=fingerprint,
    )
    if existing is not None:
        return _replay_image(existing, fingerprint)
    parameters["model"] = upstream_model
    async with _release_on_error(database, request_id):
        upstream = await _upstream(request, IMAGE_PATH, parameters, headers, request_id, deadline)
        try:
            raw = await _read_json(upstream, MAX_IMAGE_RESPONSE_BYTES, deadline)
            body, reported = _image_response(raw, model, request_id, created)
        except (TimeoutError, ValueError, httpx.RequestError):
            error = _invalid_provider_response()
            await run_in_threadpool(_finish, database, request_id, status="uncertain", error=error)
            raise error from None
        retain_result = idempotency_key is not None
        sizes = tuple(item["size"] for item in body["data"] if "size" in item)
        image_sizes = sizes if len(sizes) == len(body["data"]) and reported.get("generated_images", len(sizes)) == len(sizes) else None
        await run_in_threadpool(
            _finish, database, request_id, status="succeeded", usage=_usage_fields(reported),
            cache=body if retain_result else None, cache_created_at=created if retain_result else None,
            image_sizes=image_sizes,
        )
        return JSONResponse(body, headers=PRIVATE_HEADERS)


def _usage_bounds(month: str | None) -> tuple[str, int, int]:
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=SHANGHAI) if month else datetime.now(SHANGHAI).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0,
    )
    end = start.replace(year=start.year + (start.month == 12), month=start.month % 12 + 1)
    return start.strftime("%Y-%m"), int(start.timestamp()), int(end.timestamp())


@router.get("/api/customer/model-usage", tags=["Customer usage"])
def customer_model_usage(
    request: Request, customer: CurrentCustomer,
    month: Annotated[str | None, Query(pattern=r"^2\d{3}-(0[1-9]|1[0-2])$")] = None,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=100)] = 100,
) -> dict:
    selected, start, end = _usage_bounds(month)
    bindings = (customer.project_id, start, end)
    scope = " WHERE project_id = ? AND created_at >= ? AND created_at < ?"
    with request.app.state.db.connect() as connection:
        connection.execute("BEGIN")
        summary = connection.execute("""
            SELECT COUNT(CASE WHEN modality = 'text' THEN 1 END) AS textCalls,
                COUNT(CASE WHEN modality = 'image' THEN 1 END) AS imageCalls,
                SUM(prompt_tokens) AS promptTokens, SUM(completion_tokens) AS completionTokens,
                SUM(generated_images) AS generatedImages
            FROM general_model_calls
        """ + scope + " AND status = 'succeeded'", bindings).fetchone()
        records = connection.execute("""
            SELECT id, model, modality AS type, status, prompt_tokens AS promptTokens,
                completion_tokens AS completionTokens, total_tokens AS totalTokens,
                generated_images AS generatedImages, created_at
            FROM general_model_calls
        """ + scope + " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
        (*bindings, page_size, (page - 1) * page_size)).fetchall()
    items = []
    cutoff = key_limits.general_request_cutoff(int(time.time()))
    for record in records:
        item = dict(record)
        timestamp = item.pop("created_at")
        if item["status"] == "in_progress" and timestamp <= cutoff:
            item["status"] = "uncertain"
        items.append({**item, "createdAt": datetime.fromtimestamp(timestamp, timezone.utc).isoformat()})
    return {"month": selected, "page": page, "pageSize": page_size, "summary": dict(summary), "items": items}
