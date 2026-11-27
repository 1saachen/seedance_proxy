"""Official mainland Ark online inference list prices for this model catalog."""

from typing import NamedTuple

SNAPSHOT_VERSION = "2026-11-26"
VERIFIED_ON = "2026-11-27"
SOURCE_URL = "https://docs.volcengine.com/docs/ark/model-pricing?lang=zh&redirect=1"
SOURCE_LABEL = f"火山方舟官网在线推理刊例价（{SNAPSHOT_VERSION}）"
MICROS_PER_YUAN = 1_000_000

# Yuan per million Tokens in the order without input video, with input video.
_PRICE_BANDS = (
    ("doubao-seedance-2-5-260628", ("480p", "720p"), 70, 42),
    ("doubao-seedance-2-5-260628", ("1080p",), 77, 46),
    ("doubao-seedance-2-5-260628", ("4k",), 31, 19),
    ("doubao-seedance-2-5-flash-261115", ("480p", "720p"), 46, 28),
    ("doubao-seedance-2-0-260128", ("480p", "720p"), 46, 28),
    ("doubao-seedance-2-0-260128", ("1080p",), 51, 31),
    ("doubao-seedance-2-0-260128", ("4k",), 26, 16),
    ("doubao-seedance-2-0-fast-260128", ("480p", "720p"), 37, 22),
    ("doubao-seedance-2-0-mini-260615", ("480p", "720p"), 23, 14),
)

PUBLIC_PRICES = {
    (upstream_model, resolution, has_video): price * MICROS_PER_YUAN
    for upstream_model, resolutions, without_video, with_video in _PRICE_BANDS
    for resolution in resolutions
    for has_video, price in ((False, without_video), (True, with_video))
}


class GeneralPriceTier(NamedTuple):
    unit: str
    price_micros: int


# The relay supports regular online chat and ordinary image generation. The prices
# below exclude audio, explicit cache storage, offline inference, and layer splitting.
GENERAL_PRICE_UNITS = {"text": "million_tokens", "image": "image"}
_TEXT_METERS = {
    "input_tokens": "输入 Token（未命中缓存）",
    "cached_input_tokens": "输入 Token（命中缓存）",
    "output_tokens": "输出 Token",
}
GENERAL_METERS = {
    "text": {
        "doubao-seed-2-1-lite-260915": _TEXT_METERS,
        "doubao-seed-2-1-pro-260915": _TEXT_METERS,
    },
    "image": {
        "doubao-seedream-5-5-261028": {
            "input_image": "输入参考图",
            "output_image_up_to_2_61m": "输出图 ≤ 261 万像素",
            "output_image_2_61m_to_4_63m": "输出图 > 261 万至 463 万像素",
            "output_image_4_63m_to_8_30m": "输出图 > 463 万至 830 万像素",
        },
        "doubao-seedream-5-0-pro-260628": {
            "additional_input_image": "输入参考图（首张免费）",
            "output_image_up_to_2_61m": "输出图 ≤ 261 万像素",
            "output_image_over_2_61m": "输出图 > 261 万像素",
        },
    },
}
_GENERAL_PRICE_MICROS = {
    ("text", "doubao-seed-2-1-lite-260915", "input_tokens"): 800_000,
    ("text", "doubao-seed-2-1-lite-260915", "cached_input_tokens"): 160_000,
    ("text", "doubao-seed-2-1-lite-260915", "output_tokens"): 2_700_000,
    ("text", "doubao-seed-2-1-pro-260915", "input_tokens"): 6_000_000,
    ("text", "doubao-seed-2-1-pro-260915", "cached_input_tokens"): 1_200_000,
    ("text", "doubao-seed-2-1-pro-260915", "output_tokens"): 30_000_000,
    ("image", "doubao-seedream-5-5-261028", "input_image"): 50_000,
    ("image", "doubao-seedream-5-5-261028", "output_image_up_to_2_61m"): 130_000,
    ("image", "doubao-seedream-5-5-261028", "output_image_2_61m_to_4_63m"): 230_000,
    ("image", "doubao-seedream-5-5-261028", "output_image_4_63m_to_8_30m"): 520_000,
    ("image", "doubao-seedream-5-0-pro-260628", "additional_input_image"): 20_000,
    ("image", "doubao-seedream-5-0-pro-260628", "output_image_up_to_2_61m"): 300_000,
    ("image", "doubao-seedream-5-0-pro-260628", "output_image_over_2_61m"): 600_000,
}
GENERAL_PRICES = {
    (kind, model, meter): GeneralPriceTier(GENERAL_PRICE_UNITS[kind], amount)
    for (kind, model, meter), amount in _GENERAL_PRICE_MICROS.items()
}
