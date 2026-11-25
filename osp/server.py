"""Threaded HTTP server with 206 range streaming and REST API."""

import http.server
import json
import mimetypes
import os
import re
import socketserver
import threading
import urllib.parse

from osp.chat import merge_chat_files, parse_chat_file
from osp.config import AppConfig, ROOT_DIR, load_config, save_env_updates
from osp.downloader import DownloadManager, probe_url
from osp.health import check_dependencies
from osp.library import MediaLibrary, is_safe_subpath

STATIC_DIR = os.path.join(ROOT_DIR, "static")
STREAM_CHUNK_SIZE = 1024 * 1024  # 1 MiB

MIME_MAP = {
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".json": "application/json; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
}


def parse_range_header(range_header: str, file_size: int):
    match = re.match(r"^bytes=(\d*)-(\d*)$", range_header.strip())
    if not match:
        return None
    start_str, end_str = match.groups()
    if not start_str and not end_str:
        return None

    if not start_str:
        suffix_len = int(end_str)
        if suffix_len <= 0:
            return None
        start = max(0, file_size - suffix_len)
        end = file_size - 1
    else:
        start = int(start_str)
        end = int(end_str) if end_str else file_size - 1

    if start >= file_size or end < start:
        return None
    return start, min(end, file_size - 1)


class AppContext:
    """Shared application state bound to HTTP request handlers."""

    def __init__(self, cfg: AppConfig):
        self.lock = threading.RLock()
        self.cfg = cfg
        self.library = MediaLibrary(cfg)
        self.downloads = DownloadManager(cfg, self.library)
        self.library.scan()

    def reload_settings(self, updates: dict) -> AppConfig:
        with self.lock:
            new_cfg = save_env_updates(updates)
            self.cfg = new_cfg
            self.library.reload_config(new_cfg)
            self.downloads.update_config(new_cfg)
            self.library.scan()
            return new_cfg


class StudioHandler(http.server.SimpleHTTPRequestHandler):
    context: AppContext = None  # injected by factory

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def log_message(self, format, *args):
        pass

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 2 * 1024 * 1024))
        return json.loads(raw.decode("utf-8"))

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file_bytes(self, abs_path: str, content_type: str, cache_header: str = "no-store") -> None:
        size = os.path.getsize(abs_path)
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", cache_header)
        self.end_headers()
        with open(abs_path, "rb") as f:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _handle_stream(self, query: dict, head_only: bool = False) -> None:
        vid = (query.get("id") or [""])[0]
        if not vid:
            self._send_json(400, {"error": "Missing video id"})
            return

        abs_path = self.context.library.get_video_path(vid)
        if not abs_path:
            self._send_json(404, {"error": "Video not found"})
            return

        ext = os.path.splitext(abs_path)[1].lower()
        content_type = MIME_MAP.get(ext) or mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
        file_size = os.path.getsize(abs_path)
        range_header = self.headers.get("Range")

        if range_header:
            byte_range = parse_range_header(range_header, file_size) if file_size > 0 else None
            if byte_range is None:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{file_size}")
                self.end_headers()
                return
            start, end = byte_range
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        else:
            start = 0
            length = file_size
            self.send_response(200)

        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        if head_only or length == 0:
            return

        remaining = length
        try:
            with open(abs_path, "rb") as f:
                f.seek(start)
                while remaining > 0:
                    chunk = f.read(min(STREAM_CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/stream":
            self._handle_stream(urllib.parse.parse_qs(parsed.query), head_only=True)
            return
        super().do_HEAD()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        if parsed.path == "/stream":
            self._handle_stream(query, head_only=False)
            return

        if parsed.path == "/api/library":
            try:
                self._send_json(200, self.context.library.scan())
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/thumb":
            vid = (query.get("id") or [""])[0]
            thumb_path = self.context.library.get_thumb_path(vid) if vid else None
            if not thumb_path:
                self.send_response(404)
                self.end_headers()
                return
            self._send_file_bytes(thumb_path, "image/jpeg", cache_header="public, max-age=300")
            return

        if parsed.path == "/api/chat":
            cid = (query.get("id") or [""])[0]
            if not cid:
                self._send_json(400, {"error": "Missing chat id"})
                return
            chat_abs = self.context.library.get_chat_path(cid)
            if not chat_abs:
                self._send_json(404, {"error": "Chat file not found"})
                return
            try:
                self._send_json(200, parse_chat_file(chat_abs))
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/downloads":
            self._send_json(200, {"jobs": self.context.downloads.list_jobs()})
            return

        if parsed.path == "/api/settings":
            self._send_json(200, self.context.cfg.to_api_dict())
            return

        if parsed.path == "/api/health":
            self._send_json(200, check_dependencies(self.context.cfg))
            return

        if parsed.path == "/" or not os.path.isfile(os.path.join(STATIC_DIR, parsed.path.lstrip("/"))):
            self.path = "/index.html"

        super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            body = self._read_json_body()
        except Exception:
            self._send_json(400, {"error": "Invalid JSON body"})
            return

        if parsed.path == "/api/probe":
            url = (body.get("url") or "").strip()
            if not url:
                self._send_json(400, {"error": "URL is required"})
                return
            try:
                info = probe_url(url, self.context.cfg)
                self._send_json(200, info)
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/downloads":
            try:
                job = self.context.downloads.enqueue(
                    url=body.get("url") or "",
                    mode=body.get("mode") or "video_chat",
                    quality=body.get("quality") or "best",
                    fmt=body.get("format") or None,
                    title=body.get("title") or "",
                    target_video_id=body.get("target_video_id") or None,
                )
                self._send_json(201, {"job": job})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if parsed.path == "/api/downloads/cancel":
            jid = body.get("id") or ""
            job = self.context.downloads.cancel(jid)
            if not job:
                self._send_json(404, {"error": "Job not found"})
                return
            self._send_json(200, {"job": job})
            return

        if parsed.path == "/api/history":
            vid = body.get("video_id") or ""
            if not vid:
                self._send_json(400, {"error": "Missing video_id"})
                return
            entry = self.context.library.update_history(
                vid,
                float(body.get("position") or 0.0),
                float(body.get("duration") or 0.0),
            )
            self._send_json(200, {"progress": entry})
            return

        if parsed.path == "/api/pair-chat":
            vid = body.get("video_id") or ""
            chat_rel = body.get("chat_rel_path")
            if not vid:
                self._send_json(400, {"error": "Missing video_id"})
                return
            if not self.context.library.pair_chat(vid, chat_rel):
                self._send_json(404, {"error": "Chat file not found in DOWNLOAD_DIR"})
                return
            self._send_json(200, {"ok": True})
            return

        if parsed.path == "/api/chat-offset":
            vid = body.get("video_id") or ""
            if not vid:
                self._send_json(400, {"error": "Missing video_id"})
                return
            try:
                val = self.context.library.set_chat_offset(vid, float(body.get("offset_sec") or 0.0))
                self._send_json(200, {"offset_sec": val})
            except (TypeError, ValueError):
                self._send_json(400, {"error": "Invalid offset_sec"})
            return

        if parsed.path == "/api/merge-chats":
            p1_rel = (body.get("part1_rel_path") or "").strip()
            p2_rel = (body.get("part2_rel_path") or "").strip()
            offset_sec = float(body.get("offset_sec") or 0.0)
            target_vid = (body.get("target_video_id") or "").strip() or None
            output_name = (body.get("output_name") or "").strip()

            root = self.context.cfg.download_dir
            p1_abs = os.path.join(root, p1_rel)
            p2_abs = os.path.join(root, p2_rel)
            if not (p1_rel and p2_rel and is_safe_subpath(root, p1_abs) and is_safe_subpath(root, p2_abs)):
                self._send_json(400, {"error": "Invalid chat file path"})
                return
            if not (os.path.isfile(p1_abs) and os.path.isfile(p2_abs)):
                self._send_json(404, {"error": "One or both chat files were not found"})
                return

            if target_vid:
                vid_abs = self.context.library.get_video_path(target_vid)
                if not vid_abs:
                    self._send_json(404, {"error": "Target video not found"})
                    return
                out_abs = os.path.splitext(vid_abs)[0] + ".chat.json"
            else:
                safe_name = os.path.basename(output_name or "merged.chat.json")
                if not safe_name.endswith(".json"):
                    safe_name += ".chat.json"
                out_abs = os.path.join(root, safe_name)

            if not is_safe_subpath(root, out_abs):
                self._send_json(400, {"error": "Invalid output path"})
                return

            try:
                res = merge_chat_files(p1_abs, p2_abs, offset_sec, out_abs)
                if target_vid:
                    self.context.library.pair_chat(target_vid, os.path.relpath(out_abs, root))
                self.context.library.scan()
                self._send_json(200, {
                    "ok": True,
                    "count": res["count"],
                    "rel_path": os.path.relpath(out_abs, root),
                })
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if parsed.path == "/api/settings":
            updates = body.get("env") if isinstance(body.get("env"), dict) else body
            try:
                new_cfg = self.context.reload_settings(updates)
                self._send_json(200, new_cfg.to_api_dict())
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        self._send_json(404, {"error": "Unknown endpoint"})

    def do_DELETE(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/api/history":
            vid = (query.get("id") or [""])[0]
            if vid:
                self.context.library.clear_history(vid)
            self._send_json(200, {"ok": True})
            return

        if parsed.path == "/api/media":
            vid = (query.get("id") or [""])[0]
            del_chat = (query.get("delete_chat") or ["0"])[0] == "1"
            if not vid:
                self._send_json(400, {"error": "Missing video id"})
                return
            if not self.context.library.delete_media(vid, delete_chat=del_chat):
                self._send_json(404, {"error": "Video not found"})
                return
            self._send_json(200, {"ok": True})
            return

        self._send_json(404, {"error": "Unknown endpoint"})


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def make_handler(context: AppContext):
    class BoundHandler(StudioHandler):
        pass

    BoundHandler.context = context
    return BoundHandler


def serve(cfg: AppConfig = None) -> None:
    cfg = cfg or load_config()
    cfg.ensure_dirs()
    health = check_dependencies(cfg)
    for item in health["checks"]:
        if not item["ok"]:
            print(f"Warning: {item['name']} check failed ({item['detail']})")
    ctx = AppContext(cfg)
    handler_cls = make_handler(ctx)
    with ThreadingHTTPServer((cfg.host, cfg.port), handler_cls) as httpd:
        print(f"Offline Stream Player ready at http://{cfg.host}:{cfg.port}")
        print(f"Library & Cache directory: {cfg.download_dir}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
