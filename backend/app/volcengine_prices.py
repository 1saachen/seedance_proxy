"""Official mainland Ark online inference list prices for this model catalog."""

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
