"""Direct DOWNLOAD_DIR media scanner, ffprobe metadata cache, ffmpeg thumbnails, and history."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from typing import Optional

from osp.chat import VIDEO_EXTS, clean_display_title, find_matching_chat
from osp.config import AppConfig

IGNORED_SUBSTRS = (".part", ".ytdl", ".temp.")


def make_media_id(rel_path: str) -> str:
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:12]


def is_safe_subpath(root_dir: str, candidate: str) -> bool:
    root_real = os.path.realpath(root_dir)
    cand_real = os.path.realpath(candidate)
    return cand_real == root_real or cand_real.startswith(root_real + os.sep)


def _read_json(path: str, expected_type):
    if not os.path.isfile(path):
        return expected_type()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, expected_type):
        raise ValueError(f"Corrupted cache file: {path}")
    return data


def _write_json_atomic(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"))
    os.replace(tmp_path, path)


def _probe_video(abs_path: str) -> dict:
    if not shutil.which("ffprobe"):
        raise RuntimeError("ffprobe not found in PATH")
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate",
        "-of", "json",
        abs_path,
    ]
    out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=15)
    parsed = json.loads(out)
    fmt = parsed.get("format") or {}
    info = {
        "duration": float(fmt["duration"]) if fmt.get("duration") else 0.0,
        "width": 0,
        "height": 0,
        "fps": 0,
        "vcodec": "",
        "acodec": "",
    }
    for st in parsed.get("streams") or ():
        ctype = st.get("codec_type")
        if ctype == "video" and not info["vcodec"]:
            info["vcodec"] = st.get("codec_name", "")
            info["width"] = int(st.get("width") or 0)
            info["height"] = int(st.get("height") or 0)
            rate = st.get("avg_frame_rate") or ""
            if "/" in rate:
                num, den = rate.split("/", 1)
                if float(den or 0) > 0:
                    info["fps"] = round(float(num) / float(den))
        elif ctype == "audio" and not info["acodec"]:
            info["acodec"] = st.get("codec_name", "")
    return info


class MediaLibrary:
    """Scans top-level DOWNLOAD_DIR and manages .osp-cache metadata, thumbnails, and history."""

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._lock = threading.RLock()
        self._thumb_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="thumb")
        self._thumbs_inflight = set()
        self._meta_cache = {}
        self._history = {}
        self._pairings = {}
        self._video_index = {}
        self._chat_index = {}
        self.reload_config(cfg)

    def reload_config(self, cfg: AppConfig) -> None:
        with self._lock:
            self.cfg = cfg
            cfg.ensure_dirs()
            self._meta_cache = _read_json(cfg.library_cache_path, dict)
            self._history = _read_json(cfg.history_path, dict)
            self._pairings = _read_json(cfg.pairings_path, dict)
            self._video_index = {}
            self._chat_index = {}

    def _generate_thumbnail_worker(self, video_id: str, video_abs: str, duration: float, thumb_path: str) -> None:
        try:
            if not shutil.which("ffmpeg"):
                return
            seek = min(float(self.cfg.thumbnail_seek_sec), max(1.0, duration * 0.15)) if duration > 0 else 15.0
            tmp_thumb = thumb_path + ".tmp.jpg"
            cmd = [
                "ffmpeg", "-y", "-v", "error",
                "-ss", f"{seek:.1f}",
                "-i", video_abs,
                "-an", "-sn", "-vframes", "1",
                "-vf", "scale=480:-2",
                "-q:v", "4",
                tmp_thumb,
            ]
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25, check=False)
            if os.path.isfile(tmp_thumb) and os.path.getsize(tmp_thumb) > 0:
                os.replace(tmp_thumb, thumb_path)
            elif os.path.exists(tmp_thumb):
                os.remove(tmp_thumb)
        finally:
            with self._lock:
                self._thumbs_inflight.discard(video_id)

    def _queue_thumbnail(self, video_id: str, video_abs: str, duration: float) -> bool:
        thumb_path = os.path.join(self.cfg.thumbs_dir, f"{video_id}.jpg")
        if os.path.isfile(thumb_path):
            return True
        with self._lock:
            if video_id in self._thumbs_inflight:
                return False
            self._thumbs_inflight.add(video_id)
        self._thumb_executor.submit(self._generate_thumbnail_worker, video_id, video_abs, duration, thumb_path)
        return False

    def scan(self) -> dict:
        """Scan DOWNLOAD_DIR directly (top-level files only) using cached metadata."""
        root = self.cfg.download_dir
        if not os.path.isdir(root):
            raise FileNotFoundError(f"DOWNLOAD_DIR does not exist: {root}")

        videos_found = []
        chats_found = []

        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name.startswith(".") or not entry.is_file():
                    continue
                if any(tok in entry.name for tok in IGNORED_SUBSTRS):
                    continue
                ext = os.path.splitext(entry.name)[1].lower()
                st = entry.stat()
                if ext in VIDEO_EXTS:
                    vid = make_media_id(entry.name)
                    videos_found.append((vid, entry.name, entry.path, st))
                elif ext == ".json":
                    cid = make_media_id(entry.name)
                    chats_found.append({
                        "id": cid,
                        "rel_path": entry.name,
                        "abs_path": entry.path,
                        "name": entry.name,
                        "title": clean_display_title(entry.name),
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    })

        chat_by_rel = {c["rel_path"]: c for c in chats_found}
        meta_dirty = False
        video_records = []
        new_video_index = {}
        new_chat_index = {c["id"]: c for c in chats_found}

        for vid, rel_path, abs_path, st in videos_found:
            cached = self._meta_cache.get(vid)
            is_fresh = (
                isinstance(cached, dict)
                and cached.get("rel_path") == rel_path
                and cached.get("size") == st.st_size
                and cached.get("mtime_ns") == st.st_mtime_ns
            )
            if not is_fresh:
                probe = _probe_video(abs_path)
                cached = {
                    "id": vid,
                    "rel_path": rel_path,
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    **probe,
                }
                self._meta_cache[vid] = cached
                meta_dirty = True

            paired_rel = self._pairings.get(vid)
            chat_info = chat_by_rel.get(paired_rel) if paired_rel else None
            if not chat_info:
                auto_abs = find_matching_chat(abs_path)
                if auto_abs:
                    chat_info = chat_by_rel.get(os.path.basename(auto_abs))

            has_thumb = self._queue_thumbnail(vid, abs_path, cached.get("duration") or 0.0)
            hist = self._history.get(vid) or {}

            video_records.append({
                "id": vid,
                "name": rel_path,
                "title": clean_display_title(rel_path),
                "rel_path": rel_path,
                "size": st.st_size,
                "mtime": st.st_mtime,
                "duration": cached["duration"],
                "width": cached["width"],
                "height": cached["height"],
                "fps": cached["fps"],
                "vcodec": cached["vcodec"],
                "acodec": cached["acodec"],
                "has_thumb": has_thumb,
                "thumb_url": f"/api/thumb?id={vid}",
                "chat": (
                    {"id": chat_info["id"], "name": chat_info["name"], "rel_path": chat_info["rel_path"], "size": chat_info["size"]}
                    if chat_info
                    else None
                ),
                "progress": {
                    "position": hist.get("position", 0.0),
                    "duration": hist.get("duration", cached["duration"]),
                    "updated_at": hist.get("updated_at", 0),
                },
            })
            new_video_index[vid] = abs_path

        with self._lock:
            self._video_index = new_video_index
            self._chat_index = new_chat_index
            if meta_dirty:
                _write_json_atomic(self.cfg.library_cache_path, self._meta_cache)

        video_records.sort(key=lambda r: r["mtime"], reverse=True)
        chats_found.sort(key=lambda c: c["mtime"], reverse=True)
        return {
            "download_dir": root,
            "videos": video_records,
            "chats": [{k: v for k, v in c.items() if k != "abs_path"} for c in chats_found],
        }

    def get_video_path(self, video_id: str) -> Optional[str]:
        with self._lock:
            path = self._video_index.get(video_id)
        if path and os.path.isfile(path):
            return path
        self.scan()
        with self._lock:
            path = self._video_index.get(video_id)
        return path if path and os.path.isfile(path) else None

    def get_chat_path(self, chat_id: str) -> Optional[str]:
        with self._lock:
            entry = self._chat_index.get(chat_id)
        if entry and os.path.isfile(entry["abs_path"]):
            return entry["abs_path"]
        self.scan()
        with self._lock:
            entry = self._chat_index.get(chat_id)
        return entry["abs_path"] if entry and os.path.isfile(entry["abs_path"]) else None

    def get_thumb_path(self, video_id: str) -> Optional[str]:
        thumb_path = os.path.join(self.cfg.thumbs_dir, f"{video_id}.jpg")
        return thumb_path if os.path.isfile(thumb_path) else None

    def update_history(self, video_id: str, position: float, duration: float = 0.0) -> dict:
        with self._lock:
            if duration > 0 and position >= duration - 5:
                self._history.pop(video_id, None)
            elif position >= 5:
                self._history[video_id] = {
                    "position": round(float(position), 1),
                    "duration": round(float(duration), 1) if duration > 0 else 0.0,
                    "updated_at": int(time.time() * 1000),
                }
            else:
                return self._history.get(video_id, {})
            _write_json_atomic(self.cfg.history_path, self._history)
            return self._history.get(video_id, {})

    def clear_history(self, video_id: str) -> None:
        with self._lock:
            if video_id in self._history:
                del self._history[video_id]
                _write_json_atomic(self.cfg.history_path, self._history)

    def pair_chat(self, video_id: str, chat_rel_path: Optional[str]) -> bool:
        with self._lock:
            if not chat_rel_path:
                self._pairings.pop(video_id, None)
            else:
                abs_chat = os.path.join(self.cfg.download_dir, chat_rel_path)
                if not is_safe_subpath(self.cfg.download_dir, abs_chat) or not os.path.isfile(abs_chat):
                    return False
                self._pairings[video_id] = chat_rel_path
            _write_json_atomic(self.cfg.pairings_path, self._pairings)
            return True

    def delete_media(self, video_id: str, delete_chat: bool = False) -> bool:
        video_abs = self.get_video_path(video_id)
        if not video_abs or not is_safe_subpath(self.cfg.download_dir, video_abs):
            return False

        chat_abs = None
        if delete_chat:
            paired_rel = self._pairings.get(video_id)
            if paired_rel:
                cand = os.path.join(self.cfg.download_dir, paired_rel)
                if is_safe_subpath(self.cfg.download_dir, cand) and os.path.isfile(cand):
                    chat_abs = cand
            if not chat_abs:
                chat_abs = find_matching_chat(video_abs)

        os.remove(video_abs)
        if chat_abs and os.path.isfile(chat_abs):
            os.remove(chat_abs)

        thumb_path = os.path.join(self.cfg.thumbs_dir, f"{video_id}.jpg")
        if os.path.isfile(thumb_path):
            os.remove(thumb_path)

        with self._lock:
            self._meta_cache.pop(video_id, None)
            self._history.pop(video_id, None)
            self._pairings.pop(video_id, None)
            _write_json_atomic(self.cfg.library_cache_path, self._meta_cache)
            _write_json_atomic(self.cfg.history_path, self._history)
            _write_json_atomic(self.cfg.pairings_path, self._pairings)

        return True
