#!/bin/bash
set -e

CHAT_ONLY=false
DEST_DIR="/mnt/Extras"
URL=""

for arg in "$@"; do
    case "$arg" in
        --chat-only|--chat|-c)
            CHAT_ONLY=true
            ;;
        *)
            if [ -z "$URL" ]; then
                URL="$arg"
            else
                DEST_DIR="$arg"
            fi
            ;;
    esac
done

if [ -z "$URL" ]; then
    echo "Usage: download-kick <Kick-VOD-URL> [Destination-Directory] [--chat-only]"
    echo ""
    echo "Examples:"
    echo "  download-kick 'https://kick.com/maniya/videos/...'                    # Best Video + Chat"
    echo "  download-kick 'https://kick.com/maniya/videos/...' --chat-only        # Chat Replay ONLY"
    echo "  download-kick 'https://kick.com/maniya/videos/...' /mnt/Extras -c     # Chat Replay ONLY to /mnt/Extras"
    exit 1
fi

if [ ! -d "$DEST_DIR" ]; then
    echo "❌ Error: Destination directory '$DEST_DIR' does not exist."
    exit 1
fi

echo "=================================================="
if [ "$CHAT_ONLY" = true ]; then
    echo "💬 Kick Live Chat ONLY Downloader"
else
    echo "🎬 Kick Best Quality Video + Live Chat Downloader"
fi
echo "=================================================="
echo "🎯 URL:         $URL"
echo "📁 Destination: $DEST_DIR (All temp files & cache on HDD)"
if [ "$CHAT_ONLY" = true ]; then
    echo "⚡ Mode:        Chat Replay ONLY (Video download skipped)"
else
    echo "⚡ Max Quality: 1080p60 (Highest source bitrate)"
fi
echo "💬 Live Chat:   Full high-speed multi-threaded replay"
echo "=================================================="

export TMPDIR="$DEST_DIR"

# 1. Download Kick Chat Replay (Embedded Multi-Threaded Python)
echo "💬 [1/2] Fetching 100% of Kick Live Chat Replay..."
python3 - "$URL" "$DEST_DIR" << 'EOF_PYTHON'
import sys
import os
import re
import json
import time
import datetime
import urllib.request
import urllib.parse
import subprocess
import concurrent.futures
from datetime import timezone

vod_url = sys.argv[1].strip()
dest_dir = sys.argv[2] if len(sys.argv) > 2 else '/mnt/Extras'

print("🔍 Extracting VOD metadata via yt-dlp...")
try:
    cmd = ['yt-dlp', '-j', '--no-cache-dir', vod_url]
    raw_info = subprocess.check_output(cmd, stderr=subprocess.DEVNULL)
    info = json.loads(raw_info)
except Exception as e:
    print(f"⚠️ Warning: Could not fetch VOD metadata: {e}")
    sys.exit(1)

video_title = info.get('title', 'Kick_Stream')
video_id = info.get('id', 'vod')
chatroom_id = info.get('channel_id')
start_ts = info.get('timestamp')
duration_sec = info.get('duration', 0)

try:
    cmd_fn = ['yt-dlp', '--no-cache-dir', '--windows-filenames', '--print', 'filename', '-o', '%(title)s [%(id)s]', vod_url]
    raw_fn = subprocess.check_output(cmd_fn, stderr=subprocess.DEVNULL).decode('utf-8').strip()
    lines = [l.strip() for l in raw_fn.splitlines() if l.strip()]
    base_name = lines[-1] if lines else f"{video_title} [{video_id}]"
except Exception:
    safe_title = re.sub(r'[\\/*?:"<>|]', '_', video_title)
    base_name = f"{safe_title} [{video_id}]"

output_filename = f"{base_name}.chat.json"
output_path = os.path.join(dest_dir, output_filename)

if not chatroom_id or not start_ts or not duration_sec:
    print("⚠️ Warning: No chatroom ID, timestamp, or duration found for this VOD.")
    sys.exit(1)

start_dt = datetime.datetime.fromtimestamp(start_ts, tz=timezone.utc)
end_dt = start_dt + datetime.timedelta(seconds=duration_sec)
h = duration_sec // 3600
m = (duration_sec % 3600) // 60

print(f"📹 Title:       {video_title}")
print(f"🆔 Chatroom ID: {chatroom_id}")
print(f"⏰ Start Time:  {start_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
print(f"⏳ Duration:    ~{h}h {m}m")
print(f"📥 Fetching all messages via multi-threaded cursor pagination...")

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json'
}

# 30-minute slices
slice_sec = 1800
slices = []
t = start_dt
while t < end_dt:
    t_next = min(t + datetime.timedelta(seconds=slice_sec), end_dt)
    slices.append((t, t_next))
    t = t_next

def fetch_slice(args):
    s_idx, s_start, s_end = args
    buf_end = s_end + datetime.timedelta(seconds=45)
    url = f"https://web.kick.com/api/v1/chat/{chatroom_id}/history?start_time={urllib.request.quote(buf_end.strftime('%Y-%m-%dT%H:%M:%S.000Z'))}"
    
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            d = json.loads(resp.read().decode('utf-8'))
    except Exception:
        return []

    cur = d.get('data', {}).get('cursor')
    msgs_out = []
    seen = set()
    s_start_iso = s_start.strftime('%Y-%m-%dT%H:%M:%S.000Z')
    s_end_iso = buf_end.strftime('%Y-%m-%dT%H:%M:%S.000Z')

    while cur:
        url = f"https://web.kick.com/api/v1/chat/{chatroom_id}/history?cursor={cur}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                d = json.loads(resp.read().decode('utf-8'))
        except Exception:
            break

        payload = d.get('data', {})
        msgs = payload.get('messages', [])
        cur = payload.get('cursor')
        if not msgs:
            break

        stop = False
        for m in msgs:
            mid = m.get('id')
            c_at = m.get('created_at', '')
            if c_at < s_start_iso:
                stop = True
                break
            if c_at <= s_end_iso and mid not in seen:
                seen.add(mid)
                msgs_out.append(m)
        if stop:
            break

    sys.stdout.write(f"\r📥 Downloaded segment {s_idx+1}/{len(slices)}...   ")
    sys.stdout.flush()
    return msgs_out

tasks = [(i, st, en) for i, (st, en) in enumerate(slices)]
all_raw_msgs = []
seen_global = set()

with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
    results = executor.map(fetch_slice, tasks)
    for res in results:
        for m in res:
            mid = m.get('id')
            if mid not in seen_global:
                seen_global.add(mid)
                all_raw_msgs.append(m)

print(f"\n📊 Processed {len(all_raw_msgs)} messages. Formatting schema...")

emote_re = re.compile(r'\[emote:(\d+):([^\]]+)\]')
formatted_msgs = []

for m in all_raw_msgs:
    created_str = m.get('created_at', '')
    try:
        msg_dt = datetime.datetime.fromisoformat(created_str.replace('Z', '+00:00'))
        offset_ms = max(0, int((msg_dt - start_dt).total_seconds() * 1000))
    except Exception:
        offset_ms = 0

    sender = m.get('sender', {})
    author_name = '@' + sender.get('username', 'Anonymous')
    ident = sender.get('identity', {})

    badge_icon = ''
    badge_title = ''
    is_member = False
    for b in ident.get('badges_v2', []):
        b_name = b.get('name', '')
        if any(k in b_name.lower() for k in ['sub', 'vip', 'mod', 'founder', 'broadcaster']):
            is_member = True
            badge_title = b_name
        if b.get('image_url'):
            badge_icon = b.get('image_url')

    content = m.get('content', '')
    runs = []
    last_pos = 0
    for match in emote_re.finditer(content):
        start_pos, end_pos = match.span()
        if start_pos > last_pos:
            runs.append({'t': content[last_pos:start_pos]})
        emote_id = match.group(1)
        emote_name = match.group(2)
        runs.append({'e': emote_name, 'u': f"https://files.kick.com/emotes/{emote_id}/fullsize"})
        last_pos = end_pos
    if last_pos < len(content):
        runs.append({'t': content[last_pos:]})

    formatted_msgs.append({
        'o': offset_ms,
        'a': author_name,
        'p': '',
        'm': is_member,
        'b': badge_title,
        'bi': badge_icon,
        'r': runs,
        'sc': ''
    })

formatted_msgs.sort(key=lambda x: x['o'])

tmp_dest = output_path + ".tmp"
with open(tmp_dest, 'w', encoding='utf-8') as f:
    json.dump(formatted_msgs, f, separators=(',', ':'))
os.replace(tmp_dest, output_path)

file_kb = os.path.getsize(output_path) / 1024
print(f"✅ Kick Chat saved successfully: {len(formatted_msgs)} messages ({file_kb:.1f} KB)")
EOF_PYTHON

if [ "$CHAT_ONLY" = true ]; then
    echo ""
    echo "=================================================="
    echo "✅ Kick Chat downloaded! File saved in: $DEST_DIR"
    echo "⚡ (Video download skipped due to --chat-only)"
    echo "=================================================="
    exit 0
fi

# 2. Download Video in Best Quality with Live Fixup Progress
echo ""
echo "🎥 [2/2] Downloading Kick Video in Best Quality..."

yt-dlp \
    --no-cache-dir \
    -P "home:$DEST_DIR" \
    -P "temp:$DEST_DIR" \
    --windows-filenames \
    -f "bestvideo+bestaudio/best" \
    -N 4 \
    --continue \
    -o "%(title)s [%(id)s].%(ext)s" \
    "$URL" &
YTDL_PID=$!

(
    shown=0
    src_file=""
    temp_file=""
    total_bytes=0
    while kill -0 "$YTDL_PID" 2>/dev/null; do
        ffmpeg_pid=$(pgrep -P "$YTDL_PID" ffmpeg 2>/dev/null || true)
        if [ -n "$ffmpeg_pid" ]; then
            if [ -z "$src_file" ]; then
                temp_file=$(ls -1 /proc/"$ffmpeg_pid"/fd/* 2>/dev/null | xargs readlink 2>/dev/null | grep '\.temp\.mp4$' | head -n 1 || true)
                src_file=$(ls -1 /proc/"$ffmpeg_pid"/fd/* 2>/dev/null | xargs readlink 2>/dev/null | grep -v '\.temp\.mp4$' | grep '\.mp4$' | head -n 1 || true)
                if [ -n "$src_file" ] && [ -f "$src_file" ]; then
                    total_bytes=$(stat -c %s "$src_file" 2>/dev/null || echo 0)
                fi
            fi

            if [ "$total_bytes" -gt 0 ]; then
                cur_bytes=0
                phase="Remuxing"
                if [ -f "$temp_file" ]; then
                    cur_bytes=$(stat -c %s "$temp_file" 2>/dev/null || echo 0)
                fi

                if [ "$cur_bytes" -ge "$((total_bytes * 95 / 100))" ]; then
                    fd_pos=$(grep '^pos:' /proc/"$ffmpeg_pid"/fdinfo/* 2>/dev/null | awk '{print $2}' | sort -n | tail -n 1 || echo 0)
                    if [ "$fd_pos" -gt 0 ] && [ "$fd_pos" -lt "$total_bytes" ]; then
                        cur_bytes=$fd_pos
                        phase="Faststart Shift"
                    fi
                fi

                pct=$((cur_bytes * 100 / total_bytes))
                [ "$pct" -gt 100 ] && pct=100
                cur_gb=$(awk "BEGIN {printf \"%.1f\", $cur_bytes/1073741824}")
                tot_gb=$(awk "BEGIN {printf \"%.1f\", $total_bytes/1073741824}")

                printf "\r⚙️  [FixupM3u8] %s: %s GiB / %s GiB (%d%%)...   " "$phase" "$cur_gb" "$tot_gb" "$pct"
                shown=1
            fi
        fi
        sleep 1.5
    done
    if [ "$shown" -eq 1 ]; then
        printf "\r✅ [FixupM3u8] Container fix complete!                           \n"
    fi
) &
MONITOR_PID=$!

wait $YTDL_PID
wait $MONITOR_PID 2>/dev/null || true

echo ""
echo "=================================================="
echo "✅ Done! Kick Video & Chat saved directly in $DEST_DIR"
echo "=================================================="
