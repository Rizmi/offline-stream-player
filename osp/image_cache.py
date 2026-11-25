"""Unified local disk cache for chat avatars, badges, and emojis/emotes."""

import hashlib
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

_CACHE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

_ALLOWED_IMAGE_HOST_SUFFIXES = (
    "ggpht.com",
    "googleusercontent.com",
    "gstatic.com",
    "ytimg.com",
    "kick.com",
    "kick-files-prod.s3.us-west-2.amazonaws.com",
)

_EXT_TO_MIME = (
    ("webp", "image/webp"),
    ("png", "image/png"),
    ("jpg", "image/jpeg"),
    ("gif", "image/gif"),
    ("svg", "image/svg+xml"),
    ("bin", "application/octet-stream"),
)

_MIME_TO_EXT = {
    "image/webp": "webp",
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/svg+xml": "svg",
}

_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MiB guard


def is_allowed_image_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse((url or "").strip())
    except Exception:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in _ALLOWED_IMAGE_HOST_SUFFIXES)


def _infer_ext(content_type: str, url: str) -> str:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype in _MIME_TO_EXT:
        return _MIME_TO_EXT[ctype]
    path = urllib.parse.urlparse(url).path.lower()
    for ext in ("webp", "png", "jpg", "jpeg", "gif", "svg"):
        if path.endswith("." + ext):
            return "jpg" if ext == "jpeg" else ext
    return "bin"


class LocalImageCache:
    """Thread-safe disk cache storing remote chat images once by URL hash."""

    def __init__(self, images_dir: str):
        self._lock = threading.RLock()
        self._images_dir = ""
        self._misses = set()
        self._inflight = {}
        self.set_dir(images_dir)

    def set_dir(self, images_dir: str) -> None:
        with self._lock:
            if images_dir == self._images_dir:
                return
            self._images_dir = images_dir
            self._misses = set()
            if images_dir:
                os.makedirs(images_dir, exist_ok=True)

    def _find_cached(self, digest: str) -> Optional[tuple]:
        images_dir = self._images_dir
        if not images_dir:
            return None
        for ext, mime in _EXT_TO_MIME:
            cand = os.path.join(images_dir, f"{digest}.{ext}")
            if os.path.isfile(cand) and os.path.getsize(cand) > 0:
                return cand, mime
        return None

    def get_or_fetch(self, url: str, cached_only: bool = False) -> Optional[tuple]:
        url = (url or "").strip()
        if not is_allowed_image_url(url):
            return None

        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        with self._lock:
            hit = self._find_cached(digest)
            if hit:
                return hit
            if cached_only or digest in self._misses:
                return None
            wait_evt = self._inflight.get(digest)
            if wait_evt is None:
                wait_evt = threading.Event()
                self._inflight[digest] = wait_evt
                owner = True
            else:
                owner = False

        if not owner:
            wait_evt.wait(timeout=12.0)
            with self._lock:
                return self._find_cached(digest)

        try:
            req = urllib.request.Request(url, headers=_CACHE_HEADERS)
            with urllib.request.urlopen(req, timeout=10) as resp:
                ctype = resp.headers.get("Content-Type") or ""
                if ctype and not ctype.lower().startswith("image/") and "octet-stream" not in ctype.lower():
                    with self._lock:
                        self._misses.add(digest)
                    return None
                data = resp.read(_MAX_IMAGE_BYTES + 1)
            if not data or len(data) > _MAX_IMAGE_BYTES:
                return None

            ext = _infer_ext(ctype, url)
            mime = dict(_EXT_TO_MIME).get(ext, "application/octet-stream")
            with self._lock:
                os.makedirs(self._images_dir, exist_ok=True)
                dest = os.path.join(self._images_dir, f"{digest}.{ext}")
                tmp = dest + ".tmp"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, dest)
                self._misses.discard(digest)
                return dest, mime
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404, 410):
                with self._lock:
                    self._misses.add(digest)
            return None
        except Exception:
            return None
        finally:
            with self._lock:
                evt = self._inflight.pop(digest, None)
                if evt:
                    evt.set()
