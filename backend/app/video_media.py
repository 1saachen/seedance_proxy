from base64 import b64decode
from binascii import Error as Base64Error
from io import BytesIO

import av
from fastapi import HTTPException, Request
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from .accounts import authenticate_seedance_business_key, business_bearer
from .media import IMAGE_FORMATS, MAX_DECODED_IMAGE_PIXELS, MAX_IMAGE_PIXELS, MIB, SIZE_LIMITS, inspect_media


MAX_VIDEO_REQUEST_BYTES = 64 * MIB
MAX_INLINE_URL_LENGTH = 64 + 4 * ((SIZE_LIMITS["Image"] + 2) // 3)
HEIF_TYPES = {"image/heic", "image/heif"}
HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}
INLINE_MEDIA_TYPES = {
    **{media.content_type: "Image" for media in IMAGE_FORMATS.values()},
    **dict.fromkeys(HEIF_TYPES, "Image"),
    "image/jpg": "Image",
    "audio/wav": "Audio",
    "audio/mp3": "Audio",
    "audio/mpeg": "Audio",
}


class _BoundedRequest(Request):
    async def stream(self):
        received = 0
        async for chunk in super().stream():
            received += len(chunk)
            if received > MAX_VIDEO_REQUEST_BYTES:
                raise HTTPException(413, {"code": "video_request_too_large", "message": "Video request exceeds 64 MiB"})
            yield chunk


class VideoBodyLimit(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def bounded_handler(request: Request):
            if request.method == "POST":
                # Authenticate before reading a potentially large Base64 body. The route dependency
                # handles rate admission and checks for revocations after parsing.
                credentials = await business_bearer(request)
                await run_in_threadpool(authenticate_seedance_business_key, request, credentials)
            return await handler(_BoundedRequest(request.scope, request.receive))

        return bounded_handler


def inline_media_type(url: str) -> str | None:
    if url.startswith("data:"):
        separator = url.find(",", 5, 64)
        if separator != -1 and url[:separator].endswith(";base64"):
            return INLINE_MEDIA_TYPES.get(url[5:separator - len(";base64")])
    return None


def _inspect_heif(data: bytes, mime: str) -> None:
    box_size = int.from_bytes(data[:4], "big")
    if not 16 <= box_size <= len(data) or data[4:8] != b"ftyp" or data[8:12] not in HEIF_BRANDS:
        raise HTTPException(415, {"code": "media_type_mismatch", "message": "Inline image is not an HEIF file"})
    try:
        # FFmpeg reads HEIF items through the MOV demuxer. External container references stay disabled.
        options = {"enable_drefs": "0", "use_absolute_path": "0"}
        with av.open(BytesIO(data), mode="r", format="mov", container_options=options) as container:
            streams = container.streams.video
            if not streams or container.streams.audio:
                raise ValueError("no HEIF image")
            for stream in streams:
                codec = stream.codec_context
                if codec is None or (mime == "image/heic" and codec.name != "hevc"):
                    raise ValueError("HEIC requires HEVC image data")
                codec.thread_count = 1
                codec.options = {"err_detect": "explode", "max_pixels": str(MAX_IMAGE_PIXELS)}
            pixels = 0
            for packet in container.demux(*streams):
                if packet.is_corrupt:
                    raise ValueError("corrupt HEIF image packet")
                for frame in packet.decode():
                    frame_pixels = frame.width * frame.height
                    pixels += frame_pixels
                    if frame.is_corrupt or frame_pixels > MAX_IMAGE_PIXELS or pixels > MAX_DECODED_IMAGE_PIXELS:
                        raise ValueError("corrupt or oversized HEIF image")
            if pixels == 0:
                raise ValueError("empty HEIF image")
    except (av.error.FFmpegError, ValueError, OSError):
        raise HTTPException(422, {"code": "invalid_media", "message": "Inline HEIF image could not be decoded"}) from None


def validate_inline_media(content: list[dict]) -> None:
    for item in content:
        kind = item["type"]
        if kind not in {"image_url", "audio_url"}:
            continue
        url = item[kind]["url"]
        media_type = inline_media_type(url)
        if media_type is None:
            continue
        separator = url.index(",")
        mime = url[5:separator - len(";base64")]
        limit = SIZE_LIMITS[media_type]
        if len(url) - separator - 1 > 4 * ((limit + 2) // 3):
            raise HTTPException(413, {"code": "file_too_large", "message": "Inline media exceeds its file size limit"})
        try:
            data = b64decode(url[separator + 1:], validate=True)
        except (Base64Error, ValueError):
            raise HTTPException(422, {"code": "invalid_media", "message": "Inline media must be valid Base64"}) from None
        if len(data) > limit:
            raise HTTPException(413, {"code": "file_too_large", "message": "Inline media exceeds its file size limit"})
        if mime in HEIF_TYPES:
            _inspect_heif(data, mime)
        else:
            inspect_media(BytesIO(data), mime, len(data))
