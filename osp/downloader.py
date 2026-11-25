"""Unified YouTube & Kick media/chat downloader with job queue and CLI entrypoint."""

import argparse
from collections import deque
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


def _ytdlp_env(download_dir: str) -> dict:
    env = os.environ.copy()
    env["TMPDIR"] = download_dir
    env["TEMP"] = download_dir
    env["TMP"] = download_dir
    return env


def probe_url(url: str, cfg: AppConfig) -> dict:
    """Extract metadata and available qualities for a YouTube or Kick URL."""
    url = url.strip()
    platform = detect_platform(url)
    if platform == "unknown":
        raise ValueError("Unsupported URL. Please enter a YouTube or Kick URL.")

    cfg.ensure_dirs()
    cmd = ["yt-dlp", "-J", "--no-playlist", "--no-cache-dir", url]
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
            pct = (done_count / max(1, len(slices))) * 100.0
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

    formatted_msgs = []
    for m in all_raw_msgs:
        try:
            msg_dt = parse_iso_utc(m.get("created_at", ""))
            offset_ms = max(0, int((msg_dt - start_dt).total_seconds() * 1000))
        except Exception:
            offset_ms = 0

        sender = m.get("sender") or {}
        author_name = "@" + (sender.get("username") or "Anonymous")
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
            "p": "",
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

    def __init__(self, cfg: AppConfig, library: MediaLibrary):
        self.cfg = cfg
        self.library = library
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

    def _run_ytdlp(self, job: dict, cmd: list, dest_dir: str) -> list:
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

        try:
            for raw_line in proc.stdout:
                if job["cancel_event"].is_set():
                    break
                line = raw_line.strip()
                if not line:
                    continue

                m = _YT_PROGRESS_RE.search(line)
                if m:
                    with self._lock:
                        job["progress"] = float(m.group(1))
                        job["total_size"] = m.group(2) or ""
                        job["speed"] = m.group(3) or ""
                        job["eta"] = m.group(4) or ""
                        job["phase"] = "Downloading media"
                    continue

                if "[Merger]" in line or "[Fixup" in line or "[ffmpeg]" in line.lower():
                    with self._lock:
                        job["phase"] = "Remuxing / Merging streams..."
                elif "Writing video subtitles" in line or "live_chat" in line:
                    with self._lock:
                        job["phase"] = "Downloading live chat..."
                elif line.startswith("AFTER_MOVE:"):
                    path = line.split("AFTER_MOVE:", 1)[1].strip()
                    if path:
                        created_paths.append(path)
                    continue

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

        try:
            target_stem = None
            if job["target_video_id"] and job["mode"] == "chat_only":
                target_abs = self.library.get_video_path(job["target_video_id"])
                if target_abs:
                    target_stem = os.path.splitext(os.path.basename(target_abs))[0]

            if job["platform"] == "kick":
                if job["mode"] in ("video_chat", "chat_only"):
                    with self._lock:
                        job["phase"] = "Downloading Kick chat..."
                    chat_res = download_kick_chat(
                        job["url"],
                        dest_dir,
                        cfg,
                        output_stem=target_stem,
                        log_cb=lambda msg: self._append_log(job, msg),
                        progress_cb=lambda pct, phase: self._update_progress(job, pct * (0.35 if job["mode"] == "video_chat" else 1.0), phase),
                        cancel_event=job["cancel_event"],
                    )
                    with self._lock:
                        job["title"] = chat_res.get("title") or job["title"]
                        job["output_files"].append(chat_res["output_path"])
                    if job["target_video_id"] and job["mode"] == "chat_only":
                        rel_chat = os.path.relpath(chat_res["output_path"], dest_dir)
                        self.library.pair_chat(job["target_video_id"], rel_chat)

                if job["mode"] in ("video_chat", "video_only"):
                    with self._lock:
                        job["phase"] = "Downloading Kick video..."
                    cmd = [
                        "yt-dlp",
                        "--no-cache-dir",
                        "--newline",
                        "--no-playlist",
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
                    files = self._run_ytdlp(job, cmd, dest_dir)
                    with self._lock:
                        job["output_files"].extend(files)
            else:
                out_tmpl = f"{target_stem}.%(ext)s" if target_stem else "%(title)s [%(id)s].%(ext)s"
                cmd = [
                    "yt-dlp",
                    "--no-cache-dir",
                    "--newline",
                    "--no-playlist",
                    "-P", f"home:{dest_dir}",
                    "-P", f"temp:{dest_dir}",
                    "--windows-filenames",
                    "-N", str(cfg.yt_concurrent_fragments),
                    "--continue",
                    "--print", "after_move:AFTER_MOVE:%(filepath)s",
                    "-o", out_tmpl,
                ]
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
                files = self._run_ytdlp(job, cmd, dest_dir)
                with self._lock:
                    job["output_files"].extend(files)

                if job["target_video_id"] and job["mode"] == "chat_only" and target_stem:
                    cand = os.path.join(dest_dir, f"{target_stem}.live_chat.json")
                    if os.path.isfile(cand):
                        self.library.pair_chat(job["target_video_id"], os.path.relpath(cand, dest_dir))

            self.library.scan()
            with self._lock:
                job["status"] = "completed"
                job["phase"] = "Completed"
                job["progress"] = 100.0
                job["speed"] = ""
                job["eta"] = ""
                job["finished_at"] = int(time.time() * 1000)
        except InterruptedError:
            with self._lock:
                job["status"] = "cancelled"
                job["phase"] = "Cancelled"
                job["finished_at"] = int(time.time() * 1000)
        except Exception as exc:
            with self._lock:
                job["status"] = "failed"
                job["phase"] = "Failed"
                job["error"] = str(exc)
                job["finished_at"] = int(time.time() * 1000)
                job["logs"].append(f"Error: {exc}")

    def _update_progress(self, job: dict, pct: float, phase: str) -> None:
        with self._lock:
            job["progress"] = min(100.0, max(0.0, pct))
            job["phase"] = phase


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
        "-P", f"home:{dest_dir}",
        "-P", f"temp:{dest_dir}",
        "--windows-filenames",
        "-N", str(cfg.yt_concurrent_fragments),
        "--continue",
        "-o", "%(title)s [%(id)s].%(ext)s",
    ]
    if args.chat_only:
        cmd.extend(["--skip-download", "--write-subs", "--sub-langs", "live_chat"])
    else:
        cmd.extend(["-f", cfg.yt_quality, "--merge-output-format", cfg.yt_format, "--write-subs", "--sub-langs", "live_chat"])
    cmd.append(args.url)
    return subprocess.call(cmd, env=_ytdlp_env(dest_dir))


if __name__ == "__main__":
    raise SystemExit(cli_main())
