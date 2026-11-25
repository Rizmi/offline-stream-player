"""Chat parsing, matching, and multi-part merging utilities."""

import json
import os
import re
from typing import Optional

VIDEO_EXTS = {".mkv", ".webm", ".mp4", ".m4v", ".mov"}
CHAT_SUFFIXES = (".live_chat.json", ".chat.json", ".json")
_TITLE_CLEAN_RE = re.compile(r"\s*\[[^\]]+\]\s*$")


def clean_display_title(filename: str) -> str:
    """Turn a downloaded filename like 'Stream Title [id].mkv' into a clean title."""
    stem = os.path.splitext(filename)[0]
    for suffix in (".live_chat", ".chat"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return _TITLE_CLEAN_RE.sub("", stem).strip() or stem


def find_matching_chat(video_abs_path: str) -> Optional[str]:
    """Find an adjacent .live_chat.json, .chat.json, or .json beside a video file."""
    base, _ = os.path.splitext(video_abs_path)
    for suffix in CHAT_SUFFIXES:
        candidate = base + suffix
        if os.path.isfile(candidate):
            return candidate
    return None


def parse_chat_file(file_path: str) -> list:
    """Parse raw YouTube NDJSON .live_chat.json or compact chat JSON array."""
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        first_char = ""
        while True:
            ch = f.read(1)
            if not ch:
                break
            if not ch.isspace():
                first_char = ch
                break
        f.seek(0)

        if first_char == "[":
            data = json.load(f)
            if isinstance(data, list):
                data.sort(key=lambda item: item.get("o", 0) if isinstance(item, dict) else 0)
                return data
            return []

        messages = []
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue

            act = d.get("replayChatItemAction")
            if not isinstance(act, dict):
                continue
            try:
                offset = int(act.get("videoOffsetTimeMsec", "0"))
            except (TypeError, ValueError):
                continue
            if offset < 0:
                continue

            for action in act.get("actions") or ():
                item = (action.get("addChatItemAction") or {}).get("item") or {}
                msg = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                if not isinstance(msg, dict):
                    continue

                author = (msg.get("authorName") or {}).get("simpleText") or "Anonymous"
                p_thumbs = (msg.get("authorPhoto") or {}).get("thumbnails") or ()
                photo = p_thumbs[0].get("url", "") if p_thumbs else ""

                is_member = False
                badge_icon = ""
                for b in msg.get("authorBadges") or ():
                    r = b.get("liveChatAuthorBadgeRenderer") or {}
                    if "member" in (r.get("tooltip") or "").lower():
                        is_member = True
                    b_thumbs = (r.get("customThumbnail") or {}).get("thumbnails") or ()
                    if b_thumbs:
                        badge_icon = b_thumbs[0].get("url", "")

                runs = []
                for r in (msg.get("message") or {}).get("runs") or ():
                    if r.get("text"):
                        runs.append({"t": r["text"]})
                    elif r.get("emoji"):
                        em = r["emoji"]
                        e_thumbs = (em.get("image") or {}).get("thumbnails") or ()
                        e_url = e_thumbs[0].get("url", "") if e_thumbs else ""
                        shortcuts = em.get("shortcuts") or ()
                        runs.append({
                            "e": shortcuts[0] if shortcuts else em.get("emojiId", ""),
                            "u": e_url,
                        })

                messages.append({
                    "o": offset,
                    "a": author,
                    "p": photo,
                    "m": is_member,
                    "bi": badge_icon,
                    "r": runs,
                })

        messages.sort(key=lambda item: item["o"])
        return messages


def merge_chat_files(part1_path: str, part2_path: str, offset_sec: float, output_path: str) -> dict:
    """Merge two chat replay files, shifting part2 timestamps forward by offset_sec."""
    part1 = parse_chat_file(part1_path)
    part2 = parse_chat_file(part2_path)
    offset_ms = int(round(offset_sec * 1000))

    merged = [dict(m) for m in part1]
    for msg in part2:
        shifted = dict(msg)
        shifted["o"] = max(0, int(msg.get("o", 0)) + offset_ms)
        merged.append(shifted)

    merged.sort(key=lambda item: item.get("o", 0))
    tmp_path = output_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, separators=(",", ":"))
    os.replace(tmp_path, output_path)

    return {
        "output_path": output_path,
        "count": len(merged),
        "part1_count": len(part1),
        "part2_count": len(part2),
        "offset_ms": offset_ms,
    }
