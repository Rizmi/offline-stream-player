"""Unified YouTube & Kick media/chat downloader with job queue and CLI entrypoint."""

import argparse
from collections import Counter, deque
import concurrent.futures
import datetime
from datetime import timezone
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Optional
import urllib.error
import urllib.parse
import urllib.request
import uuid

from osp.config import AppConfig, load_config
from osp.library import MediaLibrary

_YT_PROGRESS_RE = re.compile(
    r"\[download\]\s+(\d+(?:\.\d+)?)%\s+of\s+~?(\S+)(?:\s+at\s+(\S+))?(?:\s+ETA\s+(\S+))?"
)
_OSP_PROG_RE = re.compile(r"^(?:\d+:\s*)?OSP_PROG\|(.*)$")
_OSP_PP_RE = re.compile(r"^OSP_PP\|([^|]*)\|(.*)$")
_FFMPEG_PROGRESS_RE = re.compile(r"size=\s*(\S+)\s+time=(\S+).*?speed=\s*(\S+)")
_KICK_EMOTE_RE = re.compile(r"\[emote:(\d+):([^\]]+)\]")
_KICK_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def detect_platform(url: str) -> str:
    host = urllib.parse.urlparse(url.strip()).netloc.lower()
    if "kick.com" in host:
        return "kick"
    if "youtube.com" in host or "youtu.be" in host:
        return "youtube"
    return "unknown"


def _format_bytes_str(num_bytes: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if num_bytes < 1024.0 or unit == "TiB":
            return f"{num_bytes:.1f}{unit}" if unit != "B" else f"{int(num_bytes)}B"
        num_bytes /= 1024.0
    return ""


def _parse_int_field(val: str) -> int:
    val = (val or "").strip()
    if not val or val in ("NA", "None", "null"):
        return 0
    try:
        return int(float(val))
    except ValueError:
        return 0


def _normalize_ytdlp_field(val: str, invalid: tuple = ("NA", "None", "null")) -> str:
    cleaned = " ".join((val or "").split())
    if not cleaned or cleaned in invalid:
        return ""
    return cleaned


def _normalize_kick_avatar_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc.endswith("amazonaws.com") and parsed.path.startswith("/images/"):
        return f"https://files.kick.com{parsed.path}"
    if parsed.scheme in ("http", "https"):
        return url
    return ""


_KICK_MISS_SENTINEL = "__MISS__"


class KickAvatarResolver:
    """Thread-safe resolver and disk cache for permanent Kick user profile images."""

    def __init__(self, cache_dir: str):
        self._lock = threading.RLock()
        self._cache_path = ""
        self._cache = {}
        self._misses = set()
        self._inflight = {}
        self._cooldown_until = 0.0
        self.set_cache_dir(cache_dir)

    def set_cache_dir(self, cache_dir: str) -> None:
        path = os.path.join(cache_dir, "kick_avatars.json") if cache_dir else ""
        with self._lock:
            if path == self._cache_path:
                return
            self._cache_path = path
            self._cache = {}
            self._misses = set()
            if path and os.path.isfile(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    if isinstance(raw, dict):
                        for k, v in raw.items():
                            ks = str(k)
                            vs = str(v)
                            if vs == _KICK_MISS_SENTINEL:
                                self._misses.add(ks)
                            else:
                                norm = _normalize_kick_avatar_url(vs)
                                if norm:
                                    self._cache[ks] = norm
                except Exception:
                    self._cache = {}
                    self._misses = set()

    def flush(self) -> None:
        with self._lock:
            if not self._cache_path:
                return
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            payload = dict(self._cache)
            for mk in self._misses:
                payload.setdefault(mk, _KICK_MISS_SENTINEL)
            tmp_path = self._cache_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            os.replace(tmp_path, self._cache_path)

    def get_cached(self, key: str) -> str:
        with self._lock:
            return self._cache.get(key.lower(), "")

    def _fetch_remote(self, username: str, slug: str) -> tuple:
        clean_user = (username or "").strip().lstrip("@")
        chan_slug = (slug or re.sub(r"_+", "-", clean_user.lower())).strip()
        candidates = []
        if chan_slug:
            candidates.append((f"https://kick.com/api/v2/channels/{urllib.parse.quote(chan_slug)}", "channel"))
        if clean_user:
            candidates.append((f"https://kick.com/api/v1/users/{urllib.parse.quote(clean_user)}", "user"))

        for target_url, kind in candidates:
            with self._lock:
                wait_left = self._cooldown_until - time.monotonic()
            if wait_left > 0:
                return "", True
            req = urllib.request.Request(target_url, headers=_KICK_HEADERS)
            try:
                with urllib.request.urlopen(req, timeout=8) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                raw_url = (
                    (data.get("user") or {}).get("profile_pic")
                    if kind == "channel"
                    else (data.get("profilepic") or data.get("profile_pic"))
                )
                return _normalize_kick_avatar_url(raw_url or ""), False
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    continue
                if exc.code in (403, 429):
                    with self._lock:
                        self._cooldown_until = max(self._cooldown_until, time.monotonic() + 15.0)
                    return "", True
                return "", False
            except Exception:
                return "", False
        return "", False

    def resolve(
        self,
        username: str = "",
        slug: str = "",
        persist: bool = True,
        cached_only: bool = False,
    ) -> str:
        clean_user = (username or "").strip().lstrip("@")
        key = (slug or clean_user).strip().lower()
        if not key:
            return ""

        with self._lock:
            if key in self._cache:
                return self._cache[key]
            if clean_user and clean_user.lower() in self._cache:
                return self._cache[clean_user.lower()]
            if cached_only or key in self._misses or (clean_user and clean_user.lower() in self._misses):
                return ""
            wait_evt = self._inflight.get(key)
            if wait_evt is None:
                wait_evt = threading.Event()
                self._inflight[key] = wait_evt
                owner = True
            else:
                owner = False

        if not owner:
            wait_evt.wait(timeout=10.0)
            with self._lock:
                return self._cache.get(key, "")

        try:
            url, rate_limited = self._fetch_remote(clean_user, slug)
            should_flush = False
            with self._lock:
                if url:
                    self._cache[key] = url
                    if clean_user:
                        self._cache[clean_user.lower()] = url
                    self._misses.discard(key)
                    should_flush = True
                elif not rate_limited:
                    self._misses.add(key)
                    should_flush = True
            if should_flush and persist:
                try:
                    self.flush()
                except Exception:
                    pass
            return url
        finally:
            with self._lock:
                evt = self._inflight.pop(key, None)
                if evt:
                    evt.set()


def _ytdlp_env(download_dir: str) -> dict:
    env = os.environ.copy()
    env["TMPDIR"] = download_dir
    env["TEMP"] = download_dir
    env["TMP"] = download_dir
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _resolve_zen_profile_dir() -> Optional[str]:
    candidates = [
        os.path.expanduser("~/.zen"),
        os.path.expanduser("~/.config/zen"),
        os.path.expanduser("~/.var/app/app.zen_browser.zen/.zen"),
        os.path.expanduser("~/Library/Application Support/zen"),
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(os.path.join(appdata, "zen"))
    for path in candidates:
        if os.path.isdir(path):
            return path
    return None


def _build_yt_cookie_args(cfg: AppConfig) -> list:
    if cfg.yt_cookies_file:
        if not os.path.isfile(cfg.yt_cookies_file):
            raise RuntimeError(f"Configured YT_COOKIES_FILE not found: {cfg.yt_cookies_file}")
        return ["--cookies", cfg.yt_cookies_file]

    spec = (cfg.yt_cookies_from_browser or "").strip()
    if not spec:
        return []

    base_part, *container_part = spec.split("::", 1)
    browser_name, *profile_part = base_part.split(":", 1)
    if browser_name.strip().lower() in ("zen", "zen-browser"):
        zen_dir = _resolve_zen_profile_dir()
        if not zen_dir:
            raise RuntimeError(
                "Zen Browser profile directory was not found (~/.zen or ~/.config/zen). "
                "Select another browser or provide a cookies.txt file in Settings."
            )
        target = zen_dir
        if profile_part and profile_part[0].strip():
            prof = os.path.expanduser(profile_part[0].strip())
            target = prof if os.path.isabs(prof) else os.path.join(zen_dir, prof)
        resolved = f"firefox:{target}"
        if container_part:
            resolved += f"::{container_part[0]}"
        return ["--cookies-from-browser", resolved]

    return ["--cookies-from-browser", spec]


def _format_yt_error(err_msg: str, cfg: AppConfig) -> str:
    low = (err_msg or "").lower()
    if (
        ("sign in" in low or "confirm your age" in low or "not a bot" in low)
        and not cfg.yt_cookies_file
        and not cfg.yt_cookies_from_browser
    ):
        return (
            f"{err_msg}\n\n"
            "Tip: Open Settings (gear icon) and set YouTube Cookies Browser "
            "(e.g. Zen, Firefox, Chrome) or a cookies.txt file."
        )
    return err_msg


def probe_url(url: str, cfg: AppConfig) -> dict:
    """Extract metadata and available qualities for a YouTube or Kick URL."""
    url = url.strip()
    platform = detect_platform(url)
    if platform == "unknown":
        raise ValueError("Unsupported URL. Please enter a YouTube or Kick URL.")

    cfg.ensure_dirs()
    cmd = ["yt-dlp", "-J", "--no-playlist", "--no-cache-dir"]
    if platform == "youtube":
        cmd.extend(["--remote-components", "ejs:github"])
        cmd.extend(_build_yt_cookie_args(cfg))
    cmd.append(url)
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_ytdlp_env(cfg.download_dir),
        timeout=35,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        err_msg = (proc.stderr or "Failed to extract metadata").strip().splitlines()[-1]
        if platform == "youtube":
            err_msg = _format_yt_error(err_msg, cfg)
        raise RuntimeError(err_msg)

    info = json.loads(proc.stdout)
    heights = sorted(
        {
            int(f["height"])
            for f in (info.get("formats") or ())
            if f.get("vcodec") not in (None, "none") and f.get("height")
        },
        reverse=True,
    )

    has_live_chat = False
    if platform == "kick":
        has_live_chat = bool(info.get("channel_id") and info.get("timestamp") and info.get("duration"))
    else:
        subs = info.get("subtitles") or {}
        has_live_chat = "live_chat" in subs

    return {
        "platform": platform,
        "id": info.get("id", ""),
        "title": info.get("title") or "Untitled Stream",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": float(info.get("duration") or 0),
        "thumbnail": info.get("thumbnail") or "",
        "has_live_chat": has_live_chat,
        "qualities": heights,
        "url": url,
    }


def _build_yt_format_selector(quality: str, default_selector: str) -> str:
    q = (quality or "best").strip().lower()
    if q in ("best", "auto", ""):
        return default_selector
    if q.isdigit():
        h = int(q)
        return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
    return quality


def _resolve_ytdlp_base_name(url: str, fallback_title: str, fallback_id: str, cfg: AppConfig) -> str:
    cmd = [
        "yt-dlp",
        "--no-cache-dir",
        "--windows-filenames",
        "--print",
        "filename",
        "-o",
        "%(title)s [%(id)s]",
        url,
    ]
    try:
        out = subprocess.check_output(cmd, env=_ytdlp_env(cfg.download_dir), stderr=subprocess.DEVNULL, timeout=20)
        lines = [line.strip() for line in out.decode("utf-8", errors="replace").splitlines() if line.strip()]
        if lines:
            return lines[-1]
    except Exception:
        pass
    safe = re.sub(r'[\\/*?:"<>|]', "_", fallback_title or "Kick_Stream")
    return f"{safe} [{fallback_id or 'vod'}]"


def download_kick_chat(
    url: str,
    dest_dir: str,
    cfg: AppConfig,
    output_stem: Optional[str] = None,
    log_cb: Optional[Callable[[str], None]] = None,
    progress_cb: Optional[Callable[[float, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    avatar_resolver: Optional[KickAvatarResolver] = None,
) -> dict:
    """Download full Kick VOD chat replay with rate-limited cursor pagination."""
    os.makedirs(dest_dir, exist_ok=True)
    log = log_cb or (lambda msg: None)
    report = progress_cb or (lambda pct, phase: None)
    cancelled = cancel_event or threading.Event()

    log("Extracting Kick VOD metadata...")
    raw_info = subprocess.check_output(
        ["yt-dlp", "-j", "--no-playlist", "--no-cache-dir", url],
        env=_ytdlp_env(dest_dir),
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    info = json.loads(raw_info)
    video_title = info.get("title", "Kick_Stream")
    video_id = info.get("id", "vod")
    chatroom_id = info.get("channel_id")
    start_ts = info.get("timestamp")
    duration_sec = info.get("duration", 0)

    if not chatroom_id or not start_ts or not duration_sec:
        raise RuntimeError("Missing chatroom_id, start timestamp, or duration in Kick VOD metadata.")

    base_name = output_stem or _resolve_ytdlp_base_name(url, video_title, video_id, cfg)
    output_path = os.path.join(dest_dir, f"{base_name}.chat.json")

    start_dt = datetime.datetime.fromtimestamp(start_ts, tz=timezone.utc)
    end_dt = start_dt + datetime.timedelta(seconds=duration_sec)
    slices = []
    cur_dt = start_dt
    while cur_dt < end_dt:
        next_dt = min(cur_dt + datetime.timedelta(seconds=1800), end_dt)
        slices.append((cur_dt, next_dt))
        cur_dt = next_dt

    log(f"Kick VOD: {video_title} ({len(slices)} chat segments)")

    rate_lock = threading.Lock()
    state = {"last_req": 0.0, "cooldown_until": 0.0}

    def fetch_json_with_retry(target_url: str, max_retries: int = 7) -> dict:
        for attempt in range(max_retries):
            if cancelled.is_set():
                raise InterruptedError("Cancelled")
            with rate_lock:
                now = time.monotonic()
                wait_for = max(state["cooldown_until"] - now, (state["last_req"] + cfg.kick_request_delay) - now)
                if wait_for > 0:
                    time.sleep(wait_for)
                state["last_req"] = time.monotonic()

            req = urllib.request.Request(target_url, headers=_KICK_HEADERS)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    try:
                        delay = max(5.0, float(retry_after)) if retry_after else min(45.0, 5.0 * (2 ** attempt))
                    except ValueError:
                        delay = min(45.0, 5.0 * (2 ** attempt))
                    with rate_lock:
                        state["cooldown_until"] = max(state["cooldown_until"], time.monotonic() + delay)
                    continue
                raise
            except Exception:
                if attempt < max_retries - 1:
                    time.sleep(min(30.0, 2.0 * (2 ** attempt)))
                    continue
                raise

    def parse_iso_utc(ts_str: str) -> datetime.datetime:
        return datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))

    def fetch_slice(args):
        s_idx, s_start, s_end = args
        buf_end = s_end + datetime.timedelta(seconds=45)
        next_url = (
            f"https://web.kick.com/api/v1/chat/{chatroom_id}/history"
            f"?start_time={urllib.parse.quote(buf_end.strftime('%Y-%m-%dT%H:%M:%S.000Z'))}"
        )
        msgs_out = []
        seen = set()
        seen_cursors = set()
        consecutive_empty = 0
        failed_err = None

        while next_url and not cancelled.is_set():
            try:
                d = fetch_json_with_retry(next_url)
            except InterruptedError:
                raise
            except Exception as exc:
                failed_err = str(exc)
                break

            payload = d.get("data") or {}
            msgs = payload.get("messages") or []
            cur = payload.get("cursor")

            stop = False
            if not msgs:
                consecutive_empty += 1
                if consecutive_empty >= 30:
                    break
            else:
                consecutive_empty = 0
                for m in msgs:
                    mid = m.get("id")
                    try:
                        msg_dt = parse_iso_utc(m.get("created_at", ""))
                    except Exception:
                        continue
                    if msg_dt < s_start:
                        stop = True
                        break
                    if msg_dt <= buf_end and mid not in seen:
                        seen.add(mid)
                        msgs_out.append(m)

            if stop or not cur:
                break
            cur_str = str(cur)
            if cur_str in seen_cursors:
                break
            seen_cursors.add(cur_str)
            next_url = f"https://web.kick.com/api/v1/chat/{chatroom_id}/history?cursor={urllib.parse.quote(cur_str)}"

        return s_idx, s_start, s_end, msgs_out, failed_err

    tasks = [(i, st, en) for i, (st, en) in enumerate(slices)]
    all_raw_msgs = []
    seen_global = set()
    failed_slices = []
    done_count = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=cfg.kick_chat_workers) as executor:
        for s_idx, s_start, s_end, res, err in executor.map(fetch_slice, tasks):
            if cancelled.is_set():
                raise InterruptedError("Download cancelled")
            done_count += 1
            pct = (done_count / max(1, len(slices))) * 88.0
            report(pct, f"Kick chat segment {done_count}/{len(slices)}")
            log(f"Fetched chat segment {done_count}/{len(slices)} ({len(res)} msgs)")
            if err:
                failed_slices.append((s_idx + 1, s_start, s_end, err))
            for m in res:
                mid = m.get("id")
                if mid not in seen_global:
                    seen_global.add(mid)
                    all_raw_msgs.append(m)

    if cancelled.is_set():
        raise InterruptedError("Download cancelled")

    if failed_slices:
        log(f"Warning: {len(failed_slices)}/{len(slices)} chat segments returned errors")

    resolver = avatar_resolver or KickAvatarResolver(cfg.cache_dir)
    direct_avatars = {}
    sender_counts = Counter()
    sender_slugs = {}
    for m in all_raw_msgs:
        sender = m.get("sender") or {}
        uname = (sender.get("username") or "").strip()
        slug = (sender.get("slug") or "").strip().lower()
        key = (slug or uname).lower()
        if not key:
            continue
        sender_counts[key] += 1
        if uname and key not in sender_slugs:
            sender_slugs[key] = (uname, slug)
        raw_pic = sender.get("profile_pic") or sender.get("profile_picture") or sender.get("profilepic")
        if raw_pic and key not in direct_avatars:
            norm = _normalize_kick_avatar_url(str(raw_pic))
            if norm:
                direct_avatars[key] = norm

    uncached_senders = [
        (key, *sender_slugs[key])
        for key, _ in sender_counts.most_common(200)
        if key in sender_slugs and key not in direct_avatars and not resolver.get_cached(key)
    ]
    if uncached_senders and not cancelled.is_set():
        total_av = len(uncached_senders)
        log(f"Fetching profile avatars for {total_av} Kick chatters...")
        av_done = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as av_exec:
            futs = [
                av_exec.submit(resolver.resolve, uname, slug, False)
                for _, uname, slug in uncached_senders
            ]
            for fut in concurrent.futures.as_completed(futs):
                if cancelled.is_set():
                    break
                try:
                    fut.result()
                except Exception:
                    pass
                av_done += 1
                if av_done % 10 == 0 or av_done == total_av:
                    report(88.0 + (av_done / max(1, total_av)) * 12.0, f"Fetching Kick avatars ({av_done}/{total_av})")
        try:
            resolver.flush()
        except Exception:
            pass
    else:
        report(100.0, "Formatting Kick chat...")

    formatted_msgs = []
    for m in all_raw_msgs:
        try:
            msg_dt = parse_iso_utc(m.get("created_at", ""))
            offset_ms = max(0, int((msg_dt - start_dt).total_seconds() * 1000))
        except Exception:
            offset_ms = 0

        sender = m.get("sender") or {}
        raw_uname = (sender.get("username") or "").strip()
        raw_slug = (sender.get("slug") or "").strip().lower()
        sender_key = (raw_slug or raw_uname).lower()
        author_name = "@" + (raw_uname or "Anonymous")
        avatar_url = (
            direct_avatars.get(sender_key)
            or (resolver.get_cached(sender_key) if sender_key else "")
            or (resolver.get_cached(raw_uname.lower()) if raw_uname else "")
        )
        ident = sender.get("identity") or {}

        badge_icon = ""
        badge_title = ""
        is_member = False
        for b in ident.get("badges_v2") or ():
            b_name = b.get("name", "")
            if any(k in b_name.lower() for k in ("sub", "vip", "mod", "founder", "broadcaster")):
                is_member = True
                badge_title = b_name
            if b.get("image_url"):
                badge_icon = b["image_url"]

        content = m.get("content", "")
        runs = []
        last_pos = 0
        for match in _KICK_EMOTE_RE.finditer(content):
            start_pos, end_pos = match.span()
            if start_pos > last_pos:
                runs.append({"t": content[last_pos:start_pos]})
            runs.append({
                "e": match.group(2),
                "u": f"https://files.kick.com/emotes/{match.group(1)}/fullsize",
            })
            last_pos = end_pos
        if last_pos < len(content):
            runs.append({"t": content[last_pos:]})

        formatted_msgs.append({
            "o": offset_ms,
            "a": author_name,
            "p": avatar_url,
            "m": is_member,
            "b": badge_title,
            "bi": badge_icon,
            "r": runs,
            "sc": "",
        })

    formatted_msgs.sort(key=lambda x: x["o"])
    tmp_dest = output_path + ".tmp"
    with open(tmp_dest, "w", encoding="utf-8") as f:
        json.dump(formatted_msgs, f, separators=(",", ":"))
    os.replace(tmp_dest, output_path)

    return {
        "output_path": output_path,
        "count": len(formatted_msgs),
        "failed_slices": len(failed_slices),
        "title": video_title,
    }


class DownloadManager:
    """Thread-safe background job queue for YouTube and Kick downloads."""

    def __init__(
        self,
        cfg: AppConfig,
        library: MediaLibrary,
        kick_avatars: Optional[KickAvatarResolver] = None,
    ):
        self.cfg = cfg
        self.library = library
        self._kick_avatars = kick_avatars or KickAvatarResolver(cfg.cache_dir)
        self._lock = threading.RLock()
        self._jobs = {}
        self._job_order = []
        self._queue_cond = threading.Condition(self._lock)
        self._pending_ids = deque()
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="download-worker")
        self._worker.start()

    def update_config(self, cfg: AppConfig) -> None:
        with self._lock:
            self.cfg = cfg
            self._kick_avatars.set_cache_dir(cfg.cache_dir)

    def list_jobs(self) -> list:
        with self._lock:
            return [self._serialize_job(self._jobs[jid]) for jid in reversed(self._job_order)]

    def _serialize_job(self, job: dict) -> dict:
        return {
            "id": job["id"],
            "url": job["url"],
            "platform": job["platform"],
            "title": job["title"],
            "mode": job["mode"],
            "quality": job["quality"],
            "format": job["format"],
            "status": job["status"],
            "phase": job["phase"],
            "progress": round(job["progress"], 1),
            "indeterminate": bool(job.get("indeterminate", False)),
            "speed": job["speed"],
            "eta": job["eta"],
            "total_size": job["total_size"],
            "error": job["error"],
            "target_video_id": job["target_video_id"],
            "output_files": list(job["output_files"]),
            "logs": list(job["logs"]),
            "created_at": job["created_at"],
            "finished_at": job["finished_at"],
        }

    def enqueue(
        self,
        url: str,
        mode: str = "video_chat",
        quality: Optional[str] = None,
        fmt: Optional[str] = None,
        title: str = "",
        target_video_id: Optional[str] = None,
    ) -> dict:
        url = url.strip()
        platform = detect_platform(url)
        if platform == "unknown":
            raise ValueError("Unsupported URL. Only YouTube and Kick links are supported.")
        if mode not in ("video_chat", "chat_only", "video_only"):
            raise ValueError("Invalid download mode.")

        job_id = uuid.uuid4().hex[:10]
        job = {
            "id": job_id,
            "url": url,
            "platform": platform,
            "title": title or url,
            "mode": mode,
            "quality": quality or "best",
            "format": (fmt or self.cfg.yt_format or "mkv").lower().lstrip("."),
            "status": "queued",
            "phase": "Queued",
            "progress": 0.0,
            "indeterminate": False,
            "speed": "",
            "eta": "",
            "total_size": "",
            "error": "",
            "target_video_id": target_video_id,
            "output_files": [],
            "logs": deque(maxlen=60),
            "created_at": int(time.time() * 1000),
            "finished_at": 0,
            "cancel_event": threading.Event(),
            "proc": None,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._job_order.append(job_id)
            self._pending_ids.append(job_id)
            self._queue_cond.notify()
            return self._serialize_job(job)

    def cancel(self, job_id: str) -> Optional[dict]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job["status"] in ("completed", "failed", "cancelled"):
                return self._serialize_job(job)
            job["cancel_event"].set()
            job["status"] = "cancelled"
            job["phase"] = "Cancelling..."
            proc = job.get("proc")
            if proc and proc.poll() is None:
                try:
                    if hasattr(os, "killpg"):
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    else:
                        proc.terminate()
                except Exception:
                    try:
                        proc.terminate()
                    except Exception:
                        pass
            if job["finished_at"] == 0:
                job["finished_at"] = int(time.time() * 1000)
            return self._serialize_job(job)

    def _append_log(self, job: dict, line: str) -> None:
        line = line.strip()
        if not line:
            return
        with self._lock:
            job["logs"].append(line)

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                while not self._pending_ids:
                    self._queue_cond.wait()
                job_id = self._pending_ids.popleft()
                job = self._jobs.get(job_id)
                if not job or job["cancel_event"].is_set():
                    continue
                job["status"] = "running"
                job["phase"] = "Starting..."

            self._run_job(job)

    def _set_phase(
        self,
        job: dict,
        phase: str,
        min_progress: Optional[float] = None,
        indeterminate: bool = True,
        clear_rate: bool = True,
    ) -> None:
        with self._lock:
            job["phase"] = phase
            job["indeterminate"] = indeterminate
            if min_progress is not None:
                job["progress"] = max(job["progress"], min(100.0, min_progress))
            if clear_rate:
                job["speed"] = ""
                job["eta"] = ""

    def _apply_ytdlp_progress(
        self,
        job: dict,
        payload: str,
        progress_range: tuple,
        state: dict,
    ) -> None:
        parts = payload.split("|", 8)
        if len(parts) != 9:
            return
        prog_status, dl_raw, tot_raw, est_raw, speed_raw, eta_raw, vcodec_raw, acodec_raw, proto_raw = parts
        prog_status = _normalize_ytdlp_field(prog_status)
        dl_bytes = _parse_int_field(dl_raw)
        tot_bytes = _parse_int_field(tot_raw) or _parse_int_field(est_raw)
        speed = _normalize_ytdlp_field(speed_raw, ("Unknown B/s", "NA", "None", "null"))
        eta = _normalize_ytdlp_field(eta_raw, ("Unknown", "NA", "None", "null"))
        vcodec = _normalize_ytdlp_field(vcodec_raw)
        acodec = _normalize_ytdlp_field(acodec_raw)
        protocol = _normalize_ytdlp_field(proto_raw)

        start_pct, end_pct = progress_range
        split_mid = start_pct + (end_pct - start_pct) * 0.84
        is_chat = "live_chat" in protocol
        is_video_only = not is_chat and bool(vcodec and vcodec != "none") and (not acodec or acodec == "none")
        is_audio_only = not is_chat and bool(acodec and acodec != "none") and (not vcodec or vcodec == "none")

        if is_chat:
            size_str = _format_bytes_str(dl_bytes)
            with self._lock:
                if job["mode"] != "chat_only":
                    job["progress"] = max(job["progress"], min(end_pct, start_pct + 5.0))
                if prog_status == "finished":
                    job["total_size"] = size_str
                    job["phase"] = f"Live chat downloaded ({size_str})" if size_str else "Live chat downloaded"
                    job["indeterminate"] = False
                    job["speed"] = ""
                    job["eta"] = ""
                else:
                    job["total_size"] = size_str
                    job["phase"] = f"Downloading live chat ({size_str})" if size_str else "Downloading live chat..."
                    job["indeterminate"] = True
                    job["speed"] = speed
                    job["eta"] = eta
            return

        state["media_started"] = True
        if is_video_only:
            state["saw_split_video"] = True
            label = "Downloading video"
            t_start, t_end = start_pct, split_mid
        elif is_audio_only:
            label = "Downloading audio"
            t_start, t_end = (split_mid, end_pct) if state["saw_split_video"] else (start_pct, end_pct)
        else:
            label = "Downloading media"
            t_start, t_end = start_pct, end_pct

        if prog_status == "finished":
            frac = 1.0
        elif tot_bytes > 0:
            frac = min(1.0, max(0.0, dl_bytes / tot_bytes))
        else:
            frac = None

        with self._lock:
            if frac is not None:
                mapped = t_start + frac * (t_end - t_start)
                job["progress"] = max(job["progress"], min(end_pct, mapped))
                job["indeterminate"] = False
                if prog_status == "finished":
                    done_str = _format_bytes_str(tot_bytes or dl_bytes)
                    job["total_size"] = done_str
                    job["phase"] = f"{label} complete ({done_str})" if done_str else f"{label} complete"
                    job["speed"] = ""
                    job["eta"] = ""
                else:
                    job["total_size"] = _format_bytes_str(tot_bytes)
                    job["phase"] = f"{label} ({_format_bytes_str(dl_bytes)} / {job['total_size']})"
                    job["speed"] = speed
                    job["eta"] = eta
            elif dl_bytes > 0:
                job["total_size"] = _format_bytes_str(dl_bytes)
                job["phase"] = f"{label} ({job['total_size']})"
                job["indeterminate"] = True
                job["speed"] = speed
                job["eta"] = eta

    def _run_ytdlp(
        self,
        job: dict,
        cmd: list,
        dest_dir: str,
        progress_range: tuple = (0.0, 95.0),
    ) -> list:
        created_paths = []
        popen_kwargs = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "env": _ytdlp_env(dest_dir),
            "text": True,
            "bufsize": 1,
        }
        if os.name != "nt":
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **popen_kwargs)
        with self._lock:
            job["proc"] = proc

        state = {"saw_split_video": False, "media_started": False}
        postproc_labels = (
            ("[Merger]", "Merging video & audio...", 96.0),
            ("[VideoRemuxer]", "Remuxing media container...", 96.5),
            ("[VideoConvertor]", "Converting media container...", 96.5),
            ("[ExtractAudio]", "Extracting audio...", 96.5),
            ("[Fixup", "Fixing media timestamps...", 97.0),
            ("[Metadata]", "Writing metadata...", 97.5),
            ("[EmbedSubtitle]", "Embedding subtitles...", 97.5),
            ("[EmbedThumbnail]", "Embedding thumbnail...", 97.5),
            ("Deleting original file", "Cleaning up temporary files...", 98.0),
            ("[MoveFiles]", "Moving files to library...", 98.5),
            ("Moving file ", "Moving files to library...", 98.5),
        )

        try:
            while True:
                raw_line = proc.stdout.readline()
                if not raw_line:
                    break
                if job["cancel_event"].is_set():
                    break
                line = raw_line.strip()
                if not line:
                    continue

                m_prog = _OSP_PROG_RE.match(line)
                if m_prog:
                    self._apply_ytdlp_progress(job, m_prog.group(1), progress_range, state)
                    continue

                m_pp = _OSP_PP_RE.match(line)
                if m_pp:
                    pp_name = _normalize_ytdlp_field(m_pp.group(1))
                    pp_status = _normalize_ytdlp_field(m_pp.group(2))
                    if pp_name and pp_status != "finished":
                        self._set_phase(job, f"Post-processing ({pp_name})...", min_progress=96.0)
                    continue

                m = _YT_PROGRESS_RE.search(line)
                if m:
                    start_pct, end_pct = progress_range
                    raw_pct = min(100.0, max(0.0, float(m.group(1))))
                    mapped = start_pct + (raw_pct / 100.0) * (end_pct - start_pct)
                    with self._lock:
                        job["progress"] = max(job["progress"], min(end_pct, mapped))
                        job["indeterminate"] = False
                        job["total_size"] = m.group(2) or ""
                        job["speed"] = _normalize_ytdlp_field(m.group(3) or "", ("Unknown B/s", "NA", "None"))
                        job["eta"] = _normalize_ytdlp_field(m.group(4) or "", ("Unknown", "NA", "None"))
                        job["phase"] = f"Downloading media ({job['total_size']})" if job["total_size"] else "Downloading media"
                    continue

                m_ff = _FFMPEG_PROGRESS_RE.search(line)
                if m_ff:
                    with self._lock:
                        job["indeterminate"] = True
                        job["phase"] = f"Downloading stream ({m_ff.group(1)} @ {m_ff.group(2)})"
                        job["speed"] = m_ff.group(3)
                        job["eta"] = ""
                    continue

                if line.startswith("AFTER_MOVE:"):
                    path = line.split("AFTER_MOVE:", 1)[1].strip()
                    if path:
                        created_paths.append(path)
                    continue

                matched_post = False
                for token, phase_label, min_pct in postproc_labels:
                    if token in line:
                        self._set_phase(job, phase_label, min_progress=min_pct)
                        matched_post = True
                        break

                if not matched_post:
                    if "Writing video subtitles" in line or "Downloading subtitles:" in line or "[youtube_live_chat" in line:
                        self._set_phase(job, "Downloading live chat...")
                    elif "[download] Destination:" in line:
                        self._set_phase(job, "Starting stream download...", indeterminate=False)
                    elif "[ffmpeg]" in line.lower():
                        self._set_phase(job, "Processing media streams...", min_progress=96.0)
                    elif ("[youtube]" in line or "[kick]" in line or "[info]" in line) and not state["media_started"]:
                        self._set_phase(job, "Extracting stream info...")

                self._append_log(job, line)

            ret = proc.wait()
        finally:
            with self._lock:
                job["proc"] = None

        if job["cancel_event"].is_set():
            raise InterruptedError("Download cancelled")
        if ret != 0:
            last_err = job["logs"][-1] if job["logs"] else f"yt-dlp exited with code {ret}"
            raise RuntimeError(last_err)
        return created_paths

    def _run_job(self, job: dict) -> None:
        cfg = self.cfg
        cfg.ensure_dirs()
        dest_dir = cfg.download_dir
        prog_tmpl = (
            "download:OSP_PROG|%(progress.status)s|%(progress.downloaded_bytes)s|"
            "%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|"
            "%(progress._speed_str)s|%(progress._eta_str)s|"
            "%(info.vcodec)s|%(info.acodec)s|%(info.protocol)s"
        )
        pp_tmpl = "postprocess:OSP_PP|%(progress.postprocessor)s|%(progress.status)s"

        try:
            target_stem = None
            if job["target_video_id"] and job["mode"] == "chat_only":
                target_abs = self.library.get_video_path(job["target_video_id"])
                if target_abs:
                    target_stem = os.path.splitext(os.path.basename(target_abs))[0]

            if job["platform"] == "kick":
                if job["mode"] in ("video_chat", "chat_only"):
                    self._set_phase(job, "Downloading Kick chat...")
                    chat_scale = 0.30 if job["mode"] == "video_chat" else 0.95
                    chat_res = download_kick_chat(
                        job["url"],
                        dest_dir,
                        cfg,
                        output_stem=target_stem,
                        log_cb=lambda msg: self._append_log(job, msg),
                        progress_cb=lambda pct, phase: self._update_progress(job, pct * chat_scale, phase),
                        cancel_event=job["cancel_event"],
                        avatar_resolver=self._kick_avatars,
                    )
                    with self._lock:
                        job["title"] = chat_res.get("title") or job["title"]
                        job["output_files"].append(chat_res["output_path"])
                    if job["target_video_id"] and job["mode"] == "chat_only":
                        rel_chat = os.path.relpath(chat_res["output_path"], dest_dir)
                        self.library.pair_chat(job["target_video_id"], rel_chat)

                if job["mode"] in ("video_chat", "video_only"):
                    self._set_phase(job, "Extracting Kick stream info...")
                    cmd = [
                        "yt-dlp",
                        "--no-cache-dir",
                        "--no-quiet",
                        "--newline",
                        "--progress",
                        "--no-playlist",
                        "--progress-template", prog_tmpl,
                        "--progress-template", pp_tmpl,
                        "-P", f"home:{dest_dir}",
                        "-P", f"temp:{dest_dir}",
                        "--windows-filenames",
                        "-f", "bestvideo+bestaudio/best",
                        "-N", str(cfg.yt_concurrent_fragments),
                        "--continue",
                        "--print", "after_move:AFTER_MOVE:%(filepath)s",
                        "-o", "%(title)s [%(id)s].%(ext)s",
                        job["url"],
                    ]
                    video_range = (30.0, 95.0) if job["mode"] == "video_chat" else (0.0, 95.0)
                    files = self._run_ytdlp(job, cmd, dest_dir, progress_range=video_range)
                    with self._lock:
                        job["output_files"].extend(files)
            else:
                self._set_phase(job, "Extracting stream info...")
                out_tmpl = f"{target_stem}.%(ext)s" if target_stem else "%(title)s [%(id)s].%(ext)s"
                cmd = [
                    "yt-dlp",
                    "--no-cache-dir",
                    "--remote-components", "ejs:github",
                    "--no-quiet",
                    "--newline",
                    "--progress",
                    "--no-playlist",
                    "--progress-template", prog_tmpl,
                    "--progress-template", pp_tmpl,
                    "-P", f"home:{dest_dir}",
                    "-P", f"temp:{dest_dir}",
                    "--windows-filenames",
                    "-N", str(cfg.yt_concurrent_fragments),
                    "--continue",
                    "--print", "after_move:AFTER_MOVE:%(filepath)s",
                    "-o", out_tmpl,
                ]
                cmd.extend(_build_yt_cookie_args(cfg))
                if job["mode"] == "chat_only":
                    cmd.extend(["--skip-download", "--write-subs", "--sub-langs", "live_chat"])
                else:
                    fmt_sel = _build_yt_format_selector(job["quality"], cfg.yt_quality)
                    cmd.extend([
                        "-f", fmt_sel,
                        "--merge-output-format", job["format"],
                    ])
                    if job["mode"] == "video_chat":
                        cmd.extend(["--write-subs", "--sub-langs", "live_chat"])

                cmd.append(job["url"])
                files = self._run_ytdlp(job, cmd, dest_dir, progress_range=(0.0, 95.0))
                with self._lock:
                    job["output_files"].extend(files)

                if job["target_video_id"] and job["mode"] == "chat_only" and target_stem:
                    cand = os.path.join(dest_dir, f"{target_stem}.live_chat.json")
                    if os.path.isfile(cand):
                        self.library.pair_chat(job["target_video_id"], os.path.relpath(cand, dest_dir))

            self._set_phase(job, "Scanning library & generating preview...", min_progress=99.0)
            self.library.scan()
            with self._lock:
                job["status"] = "completed"
                job["phase"] = "Completed"
                job["progress"] = 100.0
                job["indeterminate"] = False
                job["speed"] = ""
                job["eta"] = ""
                job["finished_at"] = int(time.time() * 1000)
        except InterruptedError:
            with self._lock:
                job["status"] = "cancelled"
                job["phase"] = "Cancelled"
                job["indeterminate"] = False
                job["speed"] = ""
                job["eta"] = ""
                job["finished_at"] = int(time.time() * 1000)
        except Exception as exc:
            err_text = _format_yt_error(str(exc), cfg) if job["platform"] == "youtube" else str(exc)
            with self._lock:
                job["status"] = "failed"
                job["phase"] = "Failed"
                job["indeterminate"] = False
                job["speed"] = ""
                job["eta"] = ""
                job["error"] = err_text
                job["finished_at"] = int(time.time() * 1000)
                job["logs"].append(f"Error: {err_text}")

    def _update_progress(self, job: dict, pct: float, phase: str) -> None:
        with self._lock:
            job["progress"] = max(job["progress"], min(100.0, max(0.0, pct)))
            job["phase"] = phase
            job["indeterminate"] = False
            job["speed"] = ""
            job["eta"] = ""


def cli_main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline Stream Player CLI Downloader")
    parser.add_argument("platform", choices=["youtube", "kick"], help="Source platform")
    parser.add_argument("url", help="Stream or VOD URL")
    parser.add_argument("dest_dir", nargs="?", default=None, help="Destination directory (defaults to .env DOWNLOAD_DIR)")
    parser.add_argument("--chat-only", "--chat", "-c", action="store_true", help="Download chat replay only")
    args = parser.parse_args(argv)

    cfg = load_config()
    dest_dir = os.path.abspath(os.path.expanduser(args.dest_dir)) if args.dest_dir else cfg.download_dir
    if not os.path.isdir(dest_dir):
        print(f"Error: Destination directory '{dest_dir}' does not exist.", file=sys.stderr)
        return 1

    os.environ["TMPDIR"] = dest_dir
    if args.platform == "kick":
        res = download_kick_chat(
            args.url,
            dest_dir,
            cfg,
            log_cb=lambda msg: print(msg),
        )
        print(f"Kick Chat saved: {res['count']} msgs -> {res['output_path']}")
        if not args.chat_only:
            cmd = [
                "yt-dlp",
                "--no-cache-dir",
                "-P", f"home:{dest_dir}",
                "-P", f"temp:{dest_dir}",
                "--windows-filenames",
                "-f", "bestvideo+bestaudio/best",
                "-N", str(cfg.yt_concurrent_fragments),
                "--continue",
                "-o", "%(title)s [%(id)s].%(ext)s",
                args.url,
            ]
            return subprocess.call(cmd, env=_ytdlp_env(dest_dir))
        return 0

    cmd = [
        "yt-dlp",
        "--no-cache-dir",
        "--remote-components", "ejs:github",
        "-P", f"home:{dest_dir}",
        "-P", f"temp:{dest_dir}",
        "--windows-filenames",
        "-N", str(cfg.yt_concurrent_fragments),
        "--continue",
        "-o", "%(title)s [%(id)s].%(ext)s",
    ]
    cmd.extend(_build_yt_cookie_args(cfg))
    if args.chat_only:
        cmd.extend(["--skip-download", "--write-subs", "--sub-langs", "live_chat"])
    else:
        cmd.extend(["-f", cfg.yt_quality, "--merge-output-format", cfg.yt_format, "--write-subs", "--sub-langs", "live_chat"])
    cmd.append(args.url)
    return subprocess.call(cmd, env=_ytdlp_env(dest_dir))


if __name__ == "__main__":
    raise SystemExit(cli_main())
