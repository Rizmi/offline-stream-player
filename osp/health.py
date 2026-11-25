"""System dependency and storage health checks."""

import os
import platform
import shutil
import subprocess

from osp.config import AppConfig


def _probe_tool_version(binary: str, flag: str) -> dict:
    path = shutil.which(binary)
    if not path:
        return {"name": binary, "ok": False, "version": "", "detail": f"{binary} not found in PATH"}
    try:
        out = subprocess.check_output([path, flag], stderr=subprocess.STDOUT, timeout=5, text=True)
        first_line = (out or "").strip().splitlines()[0]
        if binary in ("ffmpeg", "ffprobe") and "version " in first_line:
            version = first_line.split("version ", 1)[1].split()[0]
        else:
            version = first_line
        return {"name": binary, "ok": True, "version": version, "detail": path}
    except Exception as exc:
        return {"name": binary, "ok": False, "version": "", "detail": str(exc)}


def check_dependencies(cfg: AppConfig) -> dict:
    """Check Python, yt-dlp, ffmpeg, ffprobe, and DOWNLOAD_DIR writability."""
    dir_ok = os.path.isdir(cfg.download_dir) and os.access(cfg.download_dir, os.W_OK)
    checks = [
        {"name": "python", "ok": True, "version": platform.python_version(), "detail": "Python runtime"},
        _probe_tool_version("yt-dlp", "--version"),
        _probe_tool_version("ffmpeg", "-version"),
        _probe_tool_version("ffprobe", "-version"),
        {
            "name": "download_dir",
            "ok": dir_ok,
            "version": cfg.download_dir,
            "detail": "Writable" if dir_ok else "Directory missing or not writable",
        },
    ]
    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
    }
