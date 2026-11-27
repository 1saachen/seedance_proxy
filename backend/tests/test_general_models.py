import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest
from starlette.requests import ClientDisconnect

from app import general_models, key_limits
from app.config import Settings
from app.main import create_app


ARK_KEY = "sample-provider-key"
TEXT_MODEL = "doubao-seed-2.1-lite"
IMAGE_MODEL = "doubao-seedream-5.5"
CHAT_PATH = "/v1/chat/completions"
IMAGE_PATH = "/v1/images/generations"
USAGE_PATH = "/api/customer/model-usage"
IMAGE_REQUEST = {"model": IMAGE_MODEL, "prompt": "Create a small tree illustration"}


def _app(path, provider):
    return create_app(
        Settings(
            database_path=path, registration_enabled=True, cookie_secure=False,
            ark_api_key=ARK_KEY, video_poll_seconds=0, video_callback_poll_seconds=0, upload_cleanup_seconds=0,
        ),
        ark_transport=httpx.MockTransport(provider),
    )


def _customer(client, name):
    client.cookies.clear()
    response = client.post("/api/customer/register", json={"email": name + "@example.com", "password": "test-password"})
    assert response.status_code == 201, response.text
    account = response.json()
    response = client.post(
        "/api/customer/keys", headers={"X-CSRF-Token": account["csrfToken"]}, json={"name": "Fixture"},
    )
    assert response.status_code == 201, response.text
    key = response.json()
    return {"Authorization": "Bearer " + key["secret"]}, key["key"]["id"]


def _chat(**changes):
    return {"model": TEXT_MODEL, "messages": [{"role": "user", "content": "local-only-prompt"}], **changes}


def _image_result(number):
    return {
        "id": "ark-provider-private-id", "model": general_models.IMAGE_MODELS[IMAGE_MODEL],
        "data": [{"url": f"https://example.com/generated-{number}.png", "size": "2048x2048",
                  "output_format": "png", "private": ARK_KEY}],
        "usage": {"generated_images": 1, "output_tokens": 16384, "total_tokens": 16384, "private": ARK_KEY},
    }


def test_chat_uses_fixed_provider_path_and_counts_only_actual_tokens(tmp_path: Path):
    calls = []

    def provider(request):
        assert request.url == httpx.URL("https://ark.cn-beijing.volces.com/api/v3/chat/completions")
        assert request.headers["authorization"] == "Bearer " + ARK_KEY
        assert "cookie" not in request.headers
        sent = json.loads(request.content)
        calls.append(request.headers["x-client-request-id"])
        assert sent["model"] == general_models.TEXT_MODELS[TEXT_MODEL]
        assert sent["messages"] == _chat()["messages"]
        return httpx.Response(200, json={
            "id": "ark-provider-private-id", "model": sent["model"], "private": ARK_KEY,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "A reply", "private": ARK_KEY},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 18, "completion_tokens": 9, "total_tokens": 27, "private": ARK_KEY,
                      "prompt_tokens_details": {"cached_tokens": 5}},
        })

    with TestClient(_app(tmp_path / "models.sqlite", provider)) as client:
        headers, _ = _customer(client, "chat")
        assert client.post(CHAT_PATH, json=_chat()).status_code == 401
        oversized = client.post(CHAT_PATH, headers=headers, json=_chat(
            messages=[{"role": "user", "content": "p" * general_models.MAX_BODY_BYTES}],
        ))
        assert oversized.status_code == 413 and oversized.json()["detail"]["code"] == "model_request_too_large"
        assert client.post(CHAT_PATH, headers=headers, json=_chat(model=IMAGE_MODEL)).status_code == 422
        assert client.post(CHAT_PATH, headers=headers, json=_chat(model="ep-attacker")).status_code == 422
        assert client.post(CHAT_PATH, headers=headers, json=_chat(api_key="other")).status_code == 422
        assert client.post(CHAT_PATH, headers=headers, json=_chat(
            max_tokens=16, max_completion_tokens=32,
        )).status_code == 422
        result = client.post(CHAT_PATH, headers={**headers, "X-Client-Request-Id": "caller-controlled"}, json=_chat(max_tokens=16))
        assert result.status_code == 200, result.text
        assert result.headers["cache-control"] == "no-store"
        assert result.json() == {
            "id": result.json()["id"], "object": "chat.completion", "created": result.json()["created"],
            "model": TEXT_MODEL, "choices": [{"index": 0, "message": {"role": "assistant", "content": "A reply"},
                                              "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 18, "completion_tokens": 9, "total_tokens": 27},
        }
        assert result.json()["id"].startswith("chatcmpl-") and len(calls) == 1
        assert calls == [result.json()["id"]]
        usage = client.get(USAGE_PATH)
        assert usage.status_code == 200
        assert usage.json()["summary"] == {
            "textCalls": 1, "imageCalls": 0, "promptTokens": 18, "completionTokens": 9, "generatedImages": None,
        }
        assert usage.json()["items"][0]["totalTokens"] == 27
        with client.app.state.db.connect() as connection:
            record = dict(connection.execute("SELECT * FROM general_model_calls").fetchone())
        assert record["upstream_model"] == general_models.TEXT_MODELS[TEXT_MODEL]
        assert (record["provider_request_id"], record["cached_prompt_tokens"]) == ("ark-provider-private-id", 5)
        assert record["settled_at"] >= record["created_at"]
        assert record["cached_result_json"] is None
        assert not any(secret in repr(record) for secret in (ARK_KEY, "local-only-prompt", "A reply"))


def test_streaming_delivers_chunks_and_records_opt_in_and_hidden_usage(tmp_path: Path):
    calls = []
    complete = [
        {"model": "internal", "id": "internal", "choices": [{"delta": {"role": "assistant", "content": "你好"}}]},
        {"choices": [], "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7,
                                 "prompt_tokens_details": {"cached_tokens": 0}}},
    ]

    def provider(request):
        sent = json.loads(request.content)
        calls.append(sent)
        assert sent["stream"] and sent["stream_options"] == {"include_usage": True}
        assert request.headers["accept"] == "text/event-stream"
        chunks = b"".join(b"data: " + json.dumps(obj).encode() + b"\r\n\r\n" for obj in complete)
        if len(calls) <= 2:
            chunks += b"data: [DONE]\r\n\r\n"
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=chunks)

    with TestClient(_app(tmp_path / "stream.sqlite", provider)) as client:
        headers, key_id = _customer(client, "stream")
        with client.app.state.db.connect() as connection:
            key_limits.set_limits(connection, key_id, 1, None)
        for include_usage in (True, False):
            with client.stream(
                "POST", CHAT_PATH, headers=headers, json=_chat(stream=True, stream_options={"include_usage": include_usage}),
            ) as response:
                assert response.status_code == 200
                assert response.headers["x-accel-buffering"] == "no"
                assert response.headers["cache-control"] == "no-store"
                frames = [line[len("data: "):] for line in response.iter_lines() if line.startswith("data: ")]
            assert frames[-1] == "[DONE]"
            parsed = [json.loads(frame) for frame in frames[:-1]]
            assert parsed[0]["choices"][0]["delta"] == {"role": "assistant", "content": "你好"}
            assert all(item["model"] == TEXT_MODEL and item["id"].startswith("chatcmpl-") for item in parsed)
            assert (parsed[-1].get("usage") is not None) == include_usage
        broken = client.post(CHAT_PATH, headers=headers, json=_chat(stream=True))
        assert broken.status_code == 200 and "model_result_unknown" in broken.text
        usage = client.get(USAGE_PATH).json()
        assert usage["summary"]["textCalls"] == 2
        assert usage["summary"]["completionTokens"] == 6
        assert sorted(item["status"] for item in usage["items"]) == ["succeeded", "succeeded", "uncertain"]
        with client.app.state.db.connect() as connection:
            rows = connection.execute("""
                SELECT provider_request_id, cached_prompt_tokens, settled_at FROM general_model_calls WHERE status = 'succeeded'
            """).fetchall()
        assert len(rows) == 2 and all(tuple(row)[:2] == ("internal", 0) and row["settled_at"] for row in rows)


def test_images_validate_reference_and_are_idempotent_within_key(tmp_path: Path):
    calls = []

    def provider(request):
        assert request.url == httpx.URL("https://ark.cn-beijing.volces.com/api/v3/images/generations")
        sent = json.loads(request.content)
        calls.append(request.headers["x-client-request-id"])
        assert sent["model"] == general_models.IMAGE_MODELS[IMAGE_MODEL]
        assert sent["size"] == "3840x2160"
        assert sent["response_format"] == "url"
        assert sent["image"] == ["https://example.com/reference.png"]
        result = _image_result(len(calls))
        result["usage"]["input_images"] = 1
        return httpx.Response(200, json=result)

    image_request = {
        **IMAGE_REQUEST, "size": "3840x2160", "output_format": "png", "image": ["https://example.com/reference.png"],
    }
    with TestClient(_app(tmp_path / "images.sqlite", provider)) as client:
        auth, _ = _customer(client, "owner")
        headers = {**auth, "Idempotency-Key": "same-value"}
        for invalid in (
            {**image_request, "model": TEXT_MODEL},
            {**image_request, "model": "doubao-seedream-5.0-pro"},
            {**image_request, "response_format": "b64_json"},
            {**image_request, "image": "asset://ast_0123"},
            {**image_request, "image": "https://example.com/api/asset/source/private-token.png"},
            {**image_request, "image": "https://example.com/api/asset/%73ource/private-token.png"},
            {**image_request, "image": "https://127.0.0.1/example.png"},
        ):
            assert client.post(IMAGE_PATH, headers=headers, json=invalid).status_code == 422
        created = client.post(IMAGE_PATH, headers=headers, json=image_request)
        assert created.status_code == 200, created.text
        assert created.json()["model"] == IMAGE_MODEL
        assert created.json()["usage"]["generated_images"] == 1
        assert calls == [created.json()["id"]]
        assert "ark-provider-private-id" not in created.text and ARK_KEY not in created.text
        assert client.post(IMAGE_PATH, headers=headers, json=image_request).json() == created.json()
        assert client.post(IMAGE_PATH, headers=headers, json={**image_request, "prompt": "different"}).status_code == 409
        assert len(calls) == 1
        owner_usage = client.get(USAGE_PATH).json()
        assert owner_usage["summary"]["generatedImages"] == 1
        assert owner_usage["items"][0]["generatedImages"] == 1
        with client.app.state.db.connect() as connection:
            captured = connection.execute("""
                SELECT input_images, image_sizes_json, provider_request_id, settled_at FROM general_model_calls WHERE id = ?
            """, (created.json()["id"],)).fetchone()
        assert tuple(captured)[:3] == (1, '["2048x2048"]', None)
        assert captured["settled_at"]

        auth, _ = _customer(client, "other")
        assert client.get(USAGE_PATH).json()["items"] == []
        other = client.post(IMAGE_PATH, headers={**auth, "Idempotency-Key": "same-value"}, json=image_request)
        assert other.status_code == 200, other.text
        assert other.json()["id"] != created.json()["id"] and len(calls) == 2
        assert [item["id"] for item in client.get(USAGE_PATH).json()["items"]] == [other.json()["id"]]
        client.cookies.clear()
        assert client.get(USAGE_PATH, headers=auth).status_code == 401


def test_lost_image_response_is_not_retried_and_usage_stays_unknown(tmp_path: Path):
    calls = []

    def provider(request):
        calls.append(request)
        raise httpx.ReadTimeout("Synthetic timeout after send", request=request)

    with TestClient(_app(tmp_path / "lost.sqlite", provider)) as client:
        auth, _ = _customer(client, "lost")
        headers = {**auth, "Idempotency-Key": "lost-result"}
        response = client.post(IMAGE_PATH, headers=headers, json=IMAGE_REQUEST)
        assert response.status_code == 502 and response.json()["detail"]["code"] == "model_result_unknown"
        replay = client.post(IMAGE_PATH, headers=headers, json=IMAGE_REQUEST)
        assert replay.status_code == 409 and replay.json()["detail"]["code"] == "image_request_unknown"
        assert len(calls) == 1
        usage = client.get(USAGE_PATH).json()
        assert usage["summary"]["imageCalls"] == 0 and usage["summary"]["generatedImages"] is None
        assert usage["items"][0]["status"] == "uncertain"


def test_concurrent_retry_stays_behind_original_and_budget_counts_each_call(tmp_path: Path):
    started = threading.Event()
    finished = threading.Event()
    calls = []

    def provider(request):
        calls.append(request)
        started.set()
        if not finished.wait(5):
            raise AssertionError("Image test timed out waiting for the concurrent request")
        return httpx.Response(200, json=_image_result(1))

    app = _app(tmp_path / "concurrent.sqlite", provider)
    with TestClient(app) as client:
        auth, key_id = _customer(client, "parallel")
        headers = {**auth, "Idempotency-Key": "concurrent"}
        # HTTPX async MockTransport uses the shared service portal. Use a separate portal and
        # keep both ASGI lifespans active so the second request can check the committed reservation.
        with TestClient(_app(tmp_path / "concurrent.sqlite", provider)) as other, ThreadPoolExecutor(max_workers=1) as executor:
            original = executor.submit(client.post, IMAGE_PATH, headers=headers, json=IMAGE_REQUEST)
            try:
                assert started.wait(3)
                pending = other.post(IMAGE_PATH, headers=headers, json=IMAGE_REQUEST)
                assert pending.status_code == 409 and pending.json()["detail"]["code"] == "image_request_pending"
            finally:
                finished.set()
            assert original.result(timeout=5).status_code == 200
        assert len(calls) == 1
        with app.state.db.connect() as connection:
            key_limits.set_limits(connection, key_id, None, 2)
        blocked = client.post(CHAT_PATH, headers=auth, json=_chat())
        assert blocked.status_code == 429 and blocked.json()["detail"]["code"] == "key_rpm_exceeded"


def test_expired_image_replay_keeps_usage_unknown_without_repeating_provider_call(tmp_path: Path):
    calls = []

    def provider(request):
        calls.append(request)
        result = _image_result(1)
        del result["usage"]
        return httpx.Response(200, json=result)

    with TestClient(_app(tmp_path / "expiration.sqlite", provider)) as client:
        auth, _ = _customer(client, "expiry")
        headers = {**auth, "Idempotency-Key": "will-expire"}
        first = client.post(IMAGE_PATH, headers=headers, json=IMAGE_REQUEST)
        assert first.status_code == 200 and "usage" not in first.json()
        with client.app.state.db.connect() as connection:
            connection.execute("UPDATE general_model_calls SET cached_result_expires_at = 0 WHERE id = ?", (first.json()["id"],))
        replay = client.post(IMAGE_PATH, headers=headers, json=IMAGE_REQUEST)
        assert replay.status_code == 409 and replay.json()["detail"]["code"] == "image_result_expired"
        assert len(calls) == 1
        usage = client.get(USAGE_PATH).json()
        assert usage["summary"]["imageCalls"] == 1 and usage["summary"]["generatedImages"] is None
        assert usage["items"][0]["generatedImages"] is None
        with client.app.state.db.connect() as connection:
            assert connection.execute("SELECT cached_result_json FROM general_model_calls").fetchone()[0] is None


@pytest.mark.parametrize("disconnect", ["receive", "receive_24", "send", "before_headers"])
def test_stream_disconnect_closes_provider_and_releases_shared_slot(tmp_path: Path, disconnect: str):
    upstream_closed = threading.Event()
    calls = 0
    notify_provider_started = None

    class HeldStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"id":"ark-aborted-call","choices":[{"delta":{"content":"Hi"}}]}\n\n'
            await asyncio.Future()

        async def aclose(self):
            upstream_closed.set()

    async def provider(request):
        nonlocal calls
        calls += 1
        if request.url.path == general_models.CHAT_PATH:
            if notify_provider_started is not None:
                notify_provider_started()
                try:
                    await asyncio.Future()
                finally:
                    upstream_closed.set()
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=HeldStream())
        return httpx.Response(200, json=_image_result(1))

    app = _app(tmp_path / "disconnect.sqlite", provider)
    with TestClient(app) as client:
        auth, key_id = _customer(client, "disconnect")
        with app.state.db.connect() as connection:
            key_limits.set_limits(connection, key_id, 1, None)
            project = connection.execute("SELECT project_id FROM business_keys WHERE id = ?", (key_id,)).fetchone()[0]

        async def run_stream():
            nonlocal notify_provider_started
            disconnect_event = asyncio.Event()
            sent_request = False
            sent_content = False
            body = json.dumps(_chat(stream=True)).encode()
            path = CHAT_PATH
            scope = {
                "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3" if disconnect == "receive" else "2.4"},
                "http_version": "1.1", "method": "POST", "scheme": "http", "path": path, "raw_path": path.encode(),
                "query_string": b"", "root_path": "", "headers": [
                    (b"content-type", b"application/json"), (b"authorization", auth["Authorization"].encode()),
                ],
            }

            async def receive():
                nonlocal sent_request
                if not sent_request:
                    sent_request = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await disconnect_event.wait()
                return {"type": "http.disconnect"}

            def check_slot_is_occupied():
                with app.state.db.connect() as connection:
                    metrics = key_limits.project_key_metrics(connection, project)[key_id]
                    assert metrics["activeTextRequests"] == metrics["activeConcurrentRequests"] == 1

            async def send(message):
                nonlocal sent_content
                if not message.get("body", b"").startswith(b"data: "):
                    return
                sent_content = True
                check_slot_is_occupied()
                if disconnect == "send":
                    raise BrokenPipeError("synthetic client disconnect")
                disconnect_event.set()

            if disconnect == "before_headers":
                def abort_pre_header_stream():
                    check_slot_is_occupied()
                    disconnect_event.set()

                notify_provider_started = abort_pre_header_stream

            async with asyncio.timeout(5):
                try:
                    await app(scope, receive, send)
                except (BrokenPipeError, ClientDisconnect):
                    assert disconnect == "send"
            assert sent_content == (disconnect != "before_headers")

        client.portal.call(run_stream)
        assert upstream_closed.is_set()
        with app.state.db.connect() as connection:
            metrics = key_limits.project_key_metrics(connection, project)[key_id]
            assert metrics["activeTextRequests"] == metrics["activeConcurrentRequests"] == 0
            captured = connection.execute("SELECT status, provider_request_id FROM general_model_calls").fetchone()
            assert tuple(captured) == ("uncertain", None if disconnect == "before_headers" else "ark-aborted-call")
        assert client.post(IMAGE_PATH, headers=auth, json=IMAGE_REQUEST).status_code == 200
        assert calls == 2


def test_image_failure_and_timeout_release_shared_slots(tmp_path: Path):
    attempts = 0

    def provider(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(400)
        if attempts == 2:
            raise httpx.ReadTimeout("synthetic provider timeout", request=request)
        return httpx.Response(200, json=_image_result(1))

    app = _app(tmp_path / "image-release.sqlite", provider)
    with TestClient(app) as client:
        auth, key_id = _customer(client, "image-release")
        with app.state.db.connect() as connection:
            key_limits.set_limits(connection, key_id, 1, None)
        assert client.post(IMAGE_PATH, headers=auth, json=IMAGE_REQUEST).status_code == 422
        assert client.post(IMAGE_PATH, headers=auth, json=IMAGE_REQUEST).status_code == 502
        assert client.post(IMAGE_PATH, headers=auth, json=IMAGE_REQUEST).status_code == 200
        assert attempts == 3
        with app.state.db.connect() as connection:
            rows = connection.execute("SELECT status FROM general_model_calls ORDER BY rowid").fetchall()
        assert [row[0] for row in rows] == ["failed", "uncertain", "succeeded"]
