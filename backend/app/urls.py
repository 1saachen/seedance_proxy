import ipaddress
import re
from urllib.parse import urlsplit


PUBLIC_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}")


def is_public_https(value: str) -> bool:
    if not value.isascii() or any(ord(char) < 33 or ord(char) == 127 for char in value) or "\\" in value:
        return False
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").rstrip(".").lower()
        if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.port not in (None, 443) or parsed.fragment:
            return False
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return bool(PUBLIC_HOST.fullmatch(host)) and not host.endswith((".local", ".internal", ".localhost", ".test", ".invalid"))
        return address.is_global and not address.is_multicast and not address.is_reserved
    except ValueError:
        return False
