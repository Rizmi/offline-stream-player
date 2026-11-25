"""Strict .env configuration loader and writer (no hidden defaults or fallbacks)."""

from dataclasses import dataclass, asdict
import os
import threading

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT_DIR, ".env")

REQUIRED_KEYS = (
    "DOWNLOAD_DIR",
    "HOST",
    "PORT",
    "BROWSER",
    "AUTO_OPEN_BROWSER",
    "YT_QUALITY",
    "YT_FORMAT",
    "YT_CONCURRENT_FRAGMENTS",
    "KICK_CHAT_WORKERS",
    "KICK_REQUEST_DELAY",
    "THUMBNAIL_SEEK_SEC",
)

_CONFIG_LOCK = threading.Lock()


class ConfigError(RuntimeError):
    """Raised when .env is missing or contains invalid values."""


def _strip_quotes(val: str) -> str:
    val = val.strip()
    if len(val) >= 2 and ((val[0] == val[-1] == '"') or (val[0] == val[-1] == "'")):
        return val[1:-1]
    return val


def parse_env_file(path: str) -> dict:
    if not os.path.isfile(path):
        raise ConfigError(f"Missing .env file at {path}. Copy .env.example to .env and configure it.")
    values = {}
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ConfigError(f"Invalid line in .env: {line}")
            key, val = line.split("=", 1)
            key = key.strip()
            if key:
                values[key] = _strip_quotes(val)
    return values


def _require_str(values: dict, key: str, allow_empty: bool = False) -> str:
    if key not in values:
        raise ConfigError(f"Missing required key '{key}' in .env")
    val = values[key].strip()
    if not allow_empty and not val:
        raise ConfigError(f"Key '{key}' in .env cannot be empty")
    return val


def _require_bool(values: dict, key: str) -> bool:
    raw = _require_str(values, key).lower()
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off"):
        return False
    raise ConfigError(f"Key '{key}' in .env must be true or false (got '{raw}')")


def _require_int(values: dict, key: str, minimum: int = 1) -> int:
    raw = _require_str(values, key)
    try:
        num = int(raw)
    except ValueError as exc:
        raise ConfigError(f"Key '{key}' in .env must be an integer (got '{raw}')") from exc
    if num < minimum:
        raise ConfigError(f"Key '{key}' in .env must be >= {minimum} (got {num})")
    return num


def _require_float(values: dict, key: str, minimum: float = 0.0) -> float:
    raw = _require_str(values, key)
    try:
        num = float(raw)
    except ValueError as exc:
        raise ConfigError(f"Key '{key}' in .env must be a number (got '{raw}')") from exc
    if num < minimum:
        raise ConfigError(f"Key '{key}' in .env must be >= {minimum} (got {num})")
    return num


@dataclass
class AppConfig:
    download_dir: str
    host: str
    port: int
    browser: str
    auto_open_browser: bool
    yt_quality: str
    yt_format: str
    yt_concurrent_fragments: int
    kick_chat_workers: int
    kick_request_delay: float
    thumbnail_seek_sec: int

    @property
    def cache_dir(self) -> str:
        return os.path.join(self.download_dir, ".osp-cache")

    @property
    def thumbs_dir(self) -> str:
        return os.path.join(self.cache_dir, "thumbs")

    @property
    def library_cache_path(self) -> str:
        return os.path.join(self.cache_dir, "library.json")

    @property
    def history_path(self) -> str:
        return os.path.join(self.cache_dir, "history.json")

    @property
    def pairings_path(self) -> str:
        return os.path.join(self.cache_dir, "pairings.json")

    @property
    def offsets_path(self) -> str:
        return os.path.join(self.cache_dir, "offsets.json")

    def ensure_dirs(self) -> None:
        if not os.path.isdir(self.download_dir):
            raise ConfigError(f"DOWNLOAD_DIR does not exist: {self.download_dir}")
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.thumbs_dir, exist_ok=True)

    def to_env_dict(self) -> dict:
        return {
            "DOWNLOAD_DIR": self.download_dir,
            "HOST": self.host,
            "PORT": str(self.port),
            "BROWSER": self.browser,
            "AUTO_OPEN_BROWSER": "true" if self.auto_open_browser else "false",
            "YT_QUALITY": self.yt_quality,
            "YT_FORMAT": self.yt_format,
            "YT_CONCURRENT_FRAGMENTS": str(self.yt_concurrent_fragments),
            "KICK_CHAT_WORKERS": str(self.kick_chat_workers),
            "KICK_REQUEST_DELAY": str(self.kick_request_delay),
            "THUMBNAIL_SEEK_SEC": str(self.thumbnail_seek_sec),
        }

    def to_api_dict(self) -> dict:
        d = asdict(self)
        d["env"] = self.to_env_dict()
        d["env_path"] = ENV_PATH
        d["cache_dir"] = self.cache_dir
        return d


def _build_config(values: dict) -> AppConfig:
    download_dir = os.path.abspath(os.path.expanduser(_require_str(values, "DOWNLOAD_DIR")))
    if not os.path.isdir(download_dir):
        raise ConfigError(f"DOWNLOAD_DIR does not exist: {download_dir}")

    auto_open = _require_bool(values, "AUTO_OPEN_BROWSER")
    browser = _require_str(values, "BROWSER", allow_empty=True)
    yt_format = _require_str(values, "YT_FORMAT").lower().lstrip(".")
    if yt_format not in ("mkv", "mp4", "webm"):
        raise ConfigError(f"YT_FORMAT in .env must be mkv, mp4, or webm (got '{yt_format}')")

    return AppConfig(
        download_dir=download_dir,
        host=_require_str(values, "HOST"),
        port=_require_int(values, "PORT", minimum=1),
        browser=browser,
        auto_open_browser=auto_open,
        yt_quality=_require_str(values, "YT_QUALITY"),
        yt_format=yt_format,
        yt_concurrent_fragments=_require_int(values, "YT_CONCURRENT_FRAGMENTS", minimum=1),
        kick_chat_workers=_require_int(values, "KICK_CHAT_WORKERS", minimum=1),
        kick_request_delay=_require_float(values, "KICK_REQUEST_DELAY", minimum=0.05),
        thumbnail_seek_sec=_require_int(values, "THUMBNAIL_SEEK_SEC", minimum=1),
    )


def load_config() -> AppConfig:
    """Load and validate configuration strictly from .env."""
    with _CONFIG_LOCK:
        values = parse_env_file(ENV_PATH)
        return _build_config(values)


def save_env_updates(updates: dict) -> AppConfig:
    """Validate updates, write .env atomically preserving comments, and return AppConfig."""
    with _CONFIG_LOCK:
        current = parse_env_file(ENV_PATH)
        merged = dict(current)
        for key, val in updates.items():
            if key not in REQUIRED_KEYS:
                raise ConfigError(f"Unknown configuration key: {key}")
            merged[key] = str(val).strip()

        cfg = _build_config(merged)
        final_env = cfg.to_env_dict()

        lines = []
        seen = set()
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            for raw in f:
                stripped = raw.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k = stripped.split("=", 1)[0].strip()
                    if k in final_env:
                        lines.append(f"{k}={final_env[k]}\n")
                        seen.add(k)
                        continue
                lines.append(raw if raw.endswith("\n") else raw + "\n")

        for k in REQUIRED_KEYS:
            if k not in seen:
                lines.append(f"{k}={final_env[k]}\n")

        tmp_path = ENV_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp_path, ENV_PATH)

        for k, v in final_env.items():
            os.environ[k] = v

        return cfg
