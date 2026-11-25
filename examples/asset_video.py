"""Stage an image and submit a Seedance task that references it."""

import argparse
import ipaddress
import json
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import httpx


DEFAULT_MODEL = "doubao-seedance-2.5"
CREATE_TASK_PATH = "/api/v3/contents/generations/tasks"


class FlowError(Exception):
    def __init__(self, step: str, code: str, status: int | None = None):
        reason = code if status is None else f"HTTP {status}, {code}"
        super().__init__(f"{step} failed ({reason})")


def _request_json(client: httpx.Client, step: str, method: str, path: str, **options) -> tuple[int, dict]:
    try:
        response = client.request(method, path, **options)
    except httpx.TimeoutException:
        raise FlowError(step, "request_timeout") from None
    except httpx.RequestError:
        raise FlowError(step, "request_failed") from None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not response.is_success:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) or "vap_live_" in code:
            code = "request_rejected"
        raise FlowError(step, code, response.status_code)
    if not isinstance(payload, dict):
        raise FlowError(step, "invalid_response", response.status_code)
    return response.status_code, payload


def _read_id(record, field: str, prefix: str, step: str, status: int) -> str:
    identifier = record.get(field) if isinstance(record, dict) else None
    if not isinstance(identifier, str) or not re.fullmatch(prefix + r"_[0-9a-f]{32}", identifier):
        raise FlowError(step, "invalid_response", status)
    return identifier


def _wait_for_asset(client: httpx.Client, asset_id: str, interval: float, timeout: float) -> str:
    deadline = time.monotonic() + timeout
    step = "poll-asset"
    while (remaining := deadline - time.monotonic()) > 0:
        status, body = _request_json(
            client, step, "GET", "/api/asset/get", params={"assetId": asset_id}, timeout=min(30, remaining),
        )
        asset = body.get("asset")
        if _read_id(asset, "id", "ast", step, status) != asset_id:
            raise FlowError(step, "invalid_response", status)
        state = asset.get("status")
        if state == "Active":
            reference = asset.get("reference")
            if reference != f"asset://{asset_id}":
                raise FlowError(step, "invalid_response", status)
            return reference
        if state == "Failed":
            raise FlowError(step, "asset_processing_failed", status)
        if state != "Processing":
            raise FlowError(step, "invalid_response", status)
        time.sleep(min(interval, max(0, deadline - time.monotonic())))
    raise FlowError(step, "asset_timeout")


def _run(client: httpx.Client, image: BinaryIO, model: str, poll_interval: float, asset_timeout: float) -> dict[str, str]:
    status, body = _request_json(client, "create-group", "POST", "/api/asset-group/create", json={
        "name": f"Asset video example {uuid4().hex}",
    })
    group_id = _read_id(body.get("group"), "id", "grp", "create-group", status)
    status, body = _request_json(
        client, "upload-image", "POST", "/api/asset/upload-file",
        headers={"Idempotency-Key": uuid4().hex},
        data={"assetType": "Image"},
        files={"file": ("reference", image, "application/octet-stream")},
    )
    upload_id = _read_id(body, "uploadId", "upl", "upload-image", status)
    status, body = _request_json(client, "register-asset", "POST", "/api/asset/create", json={
        "groupId": group_id,
        "uploadId": upload_id,
        "assetType": "Image",
        "name": "Video reference",
    })
    asset_id = _read_id(body.get("asset"), "id", "ast", "register-asset", status)
    reference = _wait_for_asset(client, asset_id, poll_interval, asset_timeout)
    status, body = _request_json(
        client, "submit-video", "POST", CREATE_TASK_PATH, headers={"Idempotency-Key": uuid4().hex}, json={
            "model": model,
            "content": [
                {"type": "text", "text": "Create a short video inspired by this reference image."},
                {"type": "image_url", "image_url": {"url": reference}, "role": "reference_image"},
            ],
            "duration": 4,
            "resolution": "480p",
        },
    )
    task_id = _read_id(body, "id", "vid", "submit-video", status)
    return {"uploadId": upload_id, "assetId": asset_id, "reference": reference, "taskId": task_id}


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("enter a positive number of seconds") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("enter a positive number of seconds")
    return seconds


def _base_url(value: str) -> httpx.URL:
    message = "enter an HTTP or HTTPS base URL without credentials, query parameters, or a fragment"
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise argparse.ArgumentTypeError(message) from None
    if url.scheme not in {"http", "https"} or not url.host or url.userinfo or url.query or url.fragment:
        raise argparse.ArgumentTypeError(message)
    if url.scheme == "http" and url.host != "localhost":
        try:
            loopback = ipaddress.ip_address(url.host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise argparse.ArgumentTypeError("use HTTPS for any non-loopback server")
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, type=_base_url, help="Richi Proxy API base URL")
    parser.add_argument("--image", required=True, type=Path, help="local reference image")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Seedance model alias (default {DEFAULT_MODEL})")
    parser.add_argument("--poll-interval", type=_positive_seconds, default=2, help="seconds between status checks (default 2)")
    parser.add_argument("--asset-timeout", type=_positive_seconds, default=120, help="seconds to wait for Active (default 120)")
    args = parser.parse_args()
    api_key = os.environ.get("API_KEY", "").strip()
    if not re.fullmatch(r"[\x21-\x7e]+", api_key):
        parser.error("set API_KEY to your Richi Proxy business Key")
    try:
        with args.image.open("rb") as image, httpx.Client(
            base_url=args.base_url, headers={"Authorization": f"Bearer {api_key}"},
            follow_redirects=False, trust_env=False, timeout=httpx.Timeout(60, connect=5),
        ) as client:
            result = _run(client, image, args.model, args.poll_interval, args.asset_timeout)
    except OSError:
        print("upload-image failed (image_unreadable)", file=sys.stderr)
        return 1
    except FlowError as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
