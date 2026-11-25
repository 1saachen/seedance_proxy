from dataclasses import dataclass
from io import SEEK_END
from struct import error as StructError
from typing import BinaryIO, Literal

import av
from fastapi import HTTPException
from PIL import Image, ImageSequence


MediaType = Literal["Image", "Video", "Audio"]
MIB = 1024 * 1024
MAX_UPLOAD_BYTES = 200 * MIB
MAX_IMAGE_PIXELS = 36_000_000
MAX_DECODED_IMAGE_PIXELS = 144_000_000
MAX_VIDEO_BOXES = 16_384
MAX_VIDEO_FRAMES = 3600
MAX_DECODED_VIDEO_PIXELS = 16_200_000_000
MAX_DECODED_AUDIO_SAMPLES = 100_000_000


@dataclass(frozen=True)
class MediaInfo:
    asset_type: MediaType
    content_type: str
    extension: str


IMAGE_FORMATS = {
    "JPEG": MediaInfo("Image", "image/jpeg", ".jpg"),
    "PNG": MediaInfo("Image", "image/png", ".png"),
    "WEBP": MediaInfo("Image", "image/webp", ".webp"),
    "GIF": MediaInfo("Image", "image/gif", ".gif"),
    "BMP": MediaInfo("Image", "image/bmp", ".bmp"),
    "TIFF": MediaInfo("Image", "image/tiff", ".tiff"),
}
IMAGE_END_MARKERS = {"PNG": b"\x00\x00\x00\x00IEND\xaeB\x60\x82", "GIF": b";"}
MP4 = MediaInfo("Video", "video/mp4", ".mp4")
MOV = MediaInfo("Video", "video/quicktime", ".mov")
WAV = MediaInfo("Audio", "audio/wav", ".wav")
MP3 = MediaInfo("Audio", "audio/mpeg", ".mp3")
SIZE_LIMITS = {"Image": 30 * MIB, "Video": MAX_UPLOAD_BYTES, "Audio": 15 * MIB}
MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
    "image/x-ms-bmp": "image/bmp",
    "video/x-quicktime": "video/quicktime",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/mp3": "audio/mpeg",
}
SUPPORTED_MIMES = {media.content_type for media in (*IMAGE_FORMATS.values(), MP4, MOV, WAV, MP3)}
VIDEO_BOXES = (b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide")
MP3_SAMPLE_RATES = {0: (11025, 12000, 8000), 2: (22050, 24000, 16000), 3: (44100, 48000, 32000)}
MP3_BITRATES_MPEG1 = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
MP3_BITRATES_MPEG2 = (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160)


def _error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def _inspect_image(file: BinaryIO, declared: str, size: int) -> MediaInfo | None:
    try:
        image = Image.open(file, formats=tuple(IMAGE_FORMATS))
    except Image.UnidentifiedImageError:
        return None
    with image:
        image_format = image.format
        assert image_format is not None
        media = IMAGE_FORMATS[image_format]
        _check_type_and_size(media, declared, size)
        image.verify()
    marker = IMAGE_END_MARKERS.get(image_format)
    if marker:
        file.seek(-len(marker), SEEK_END)
        if file.read(len(marker)) != marker:
            raise ValueError("incomplete image trailer")
    file.seek(0)
    with Image.open(file, formats=[image_format]) as image:
        pixels = 0
        for frame in ImageSequence.Iterator(image):
            frame_pixels = frame.width * frame.height
            pixels += frame_pixels
            if frame_pixels > MAX_IMAGE_PIXELS or pixels > MAX_DECODED_IMAGE_PIXELS:
                raise _error(422, "invalid_media", "图片的解码像素数超过允许范围")
            frame.load()
    return media


def _video_format(file: BinaryIO, size: int) -> MediaInfo:
    """Check the container boundaries without reading the media data into memory."""
    required_boxes = {b"moov", b"mdat"}
    offset = 0
    boxes = 0
    major_brand = b"qt  "  # Legacy QuickTime files can omit ftyp.
    while offset < size:
        boxes += 1
        if boxes > MAX_VIDEO_BOXES:
            raise ValueError("too many video boxes")
        file.seek(offset)
        header = file.read(8)
        if len(header) != 8:
            raise ValueError("incomplete video box header")
        box_size = int.from_bytes(header[:4], "big")
        box_type = header[4:8]
        header_size = 8
        if box_size == 1:
            box_size = int.from_bytes(file.read(8), "big")
            header_size = 16
        elif box_size == 0:
            box_size = size - offset
        if box_size < header_size or offset + box_size > size:
            raise ValueError("incomplete video box")
        if box_type == b"ftyp":
            if box_size < header_size + 8:
                raise ValueError("incomplete video file type")
            major_brand = file.read(4)
        required_boxes.discard(box_type)
        offset += box_size
    if required_boxes:
        raise ValueError("missing video metadata or data")
    if major_brand.startswith((b"3gp", b"3g2")) or major_brand in {b"avif", b"avis", b"mif1", b"heic", b"heix"}:
        raise _error(415, "unsupported_media_type", "视频仅支持 MP4 和 MOV")
    return MOV if major_brand == b"qt  " else MP4


def _check_mp3_packet(packet: av.Packet) -> None:
    # The decoder can conceal missing bytes in the final Layer III frame.
    if packet.size < 4:
        raise ValueError("incomplete MP3 frame header")
    header = memoryview(packet)[:4]
    version = (header[1] >> 3) & 3
    rate_index = (header[2] >> 2) & 3
    bitrate_index = header[2] >> 4
    if version not in MP3_SAMPLE_RATES or rate_index == 3 or bitrate_index == 15:
        raise ValueError("invalid MP3 frame header")
    if bitrate_index == 0:  # The free-format frame size comes from the decoder.
        return
    sample_rate = MP3_SAMPLE_RATES[version][rate_index]
    bitrates = MP3_BITRATES_MPEG1 if version == 3 else MP3_BITRATES_MPEG2
    samples_per_frame = 1152 if version == 3 else 576
    bits_per_second = bitrates[bitrate_index] * 1000
    padding_byte = (header[2] >> 1) & 1
    size = (samples_per_frame * bits_per_second) // (sample_rate * 8) + padding_byte
    if packet.size != size:
        raise ValueError("incomplete MP3 frame")


def _decode_av(file: BinaryIO, demuxer: str, media: MediaInfo) -> None:
    file.seek(0)
    # Use only the selected demuxer and forbid external media references from MOV/MP4.
    options = {"enable_drefs": "0", "use_absolute_path": "0"} if demuxer == "mov" else {}
    with av.open(file, mode="r", format=demuxer, container_options=options) as container:
        if media.asset_type == "Video":
            stream = next((
                item for item in container.streams.video
                if not (item.disposition & av.stream.Disposition.attached_pic)
            ), None)
        else:
            stream = next(iter(container.streams.audio), None)
        if stream is None or (media == MP3 and stream.codec_context.name not in {"mp3", "mp3float"}):
            raise ValueError("missing expected media stream")

        stream.codec_context.thread_count = 1
        stream.codec_context.options = {"err_detect": "explode", "max_pixels": str(MAX_IMAGE_PIXELS)}
        frames = 0
        decoded_size = 0
        for packet in container.demux(stream):
            if packet.is_corrupt:
                raise ValueError("corrupt media packet")
            if media == MP3 and packet.size:
                _check_mp3_packet(packet)
            for frame in packet.decode():
                if frame.is_corrupt:
                    raise ValueError("corrupt media frame")
                frames += 1
                if media.asset_type == "Video":
                    decoded_size += frame.width * frame.height
                    if frames > MAX_VIDEO_FRAMES or decoded_size > MAX_DECODED_VIDEO_PIXELS:
                        raise _error(422, "invalid_media", "视频超过允许的解码帧数或像素数")
                else:
                    decoded_size += frame.samples * len(frame.layout.channels)
                    if decoded_size > MAX_DECODED_AUDIO_SAMPLES:
                        raise _error(422, "invalid_media", "音频超过允许的解码样本数")
        if not frames:
            raise ValueError("empty media stream")


def _check_type_and_size(media: MediaInfo, declared: str, size: int) -> None:
    if declared and declared != "application/octet-stream" and declared != media.content_type:
        raise _error(415, "media_type_mismatch", "文件内容与声明的 Content-Type 不一致")
    if size > SIZE_LIMITS[media.asset_type]:
        raise _error(413, "file_too_large", "文件超过该素材类型允许的大小")


def inspect_media(file: BinaryIO, content_type: str | None, size: int) -> MediaInfo:
    """Identify and validate a staged binary stream, then rewind it for storage."""
    if not size:
        raise _error(422, "invalid_media", "素材文件为空")
    if size > MAX_UPLOAD_BYTES:
        raise _error(413, "file_too_large", "文件超过允许的大小")
    declared = (content_type or "").partition(";")[0].strip().lower()
    declared = MIME_ALIASES.get(declared, declared)
    file.seek(0)
    try:
        header = file.read(12)
        file.seek(0)
        mp3_frame_sync = len(header) >= 2 and header[0] == 0xFF and (header[1] & 0xE6) == 0xE2
        if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
            media, demuxer = WAV, "wav"
            if int.from_bytes(header[4:8], "little") + 8 != size:
                raise ValueError("incomplete WAV container")
        elif header[4:8] in VIDEO_BOXES:
            media, demuxer = _video_format(file, size), "mov"
        elif header.startswith(b"ID3") or mp3_frame_sync:
            media, demuxer = MP3, "mp3"
        else:
            image = _inspect_image(file, declared, size)
            if image is not None:
                return image
            if declared in SUPPORTED_MIMES:
                raise ValueError("no recognizable media header")
            raise _error(415, "unsupported_media_type", "不支持该文件的真实媒体格式")
        _check_type_and_size(media, declared, size)
        _decode_av(file, demuxer, media)
        return media
    except (
        av.error.FFmpegError, Image.DecompressionBombError, OSError, ValueError, SyntaxError, EOFError, StructError,
    ) as error:
        raise _error(422, "invalid_media", "素材文件已损坏或没有可读取的媒体内容") from error
    finally:
        file.seek(0)
