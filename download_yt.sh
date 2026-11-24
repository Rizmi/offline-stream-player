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
    echo "Usage: download-yt <YouTube-URL> [Destination-Directory] [--chat-only]"
    echo ""
    echo "Examples:"
    echo "  download-yt 'https://www.youtube.com/watch?v=...'                    # Best Video + Chat"
    echo "  download-yt 'https://www.youtube.com/watch?v=...' --chat-only        # Chat Replay ONLY"
    echo "  download-yt 'https://www.youtube.com/watch?v=...' /mnt/Extras -c     # Chat Replay ONLY to /mnt/Extras"
    exit 1
fi

if [ ! -d "$DEST_DIR" ]; then
    echo "❌ Error: Destination directory '$DEST_DIR' does not exist."
    exit 1
fi

echo "=================================================="
if [ "$CHAT_ONLY" = true ]; then
    echo "💬 YouTube Live Chat ONLY Downloader"
else
    echo "🎬 YouTube Best Quality + Live Chat Downloader"
fi
echo "=================================================="
echo "🎯 URL:         $URL"
echo "📁 Destination: $DEST_DIR (All temp files & cache on HDD)"
if [ "$CHAT_ONLY" = true ]; then
    echo "⚡ Mode:        Chat Replay ONLY (Video download skipped)"
else
    echo "⚡ Max Quality: 1440p / 4K / Best Video + Best Audio"
    echo "💬 Live Chat:   Auto-download if available"
fi
echo "=================================================="

export TMPDIR="$DEST_DIR"

if [ "$CHAT_ONLY" = true ]; then
    yt-dlp \
        --no-cache-dir \
        -P "home:$DEST_DIR" \
        -P "temp:$DEST_DIR" \
        --windows-filenames \
        --skip-download \
        --write-subs \
        --sub-langs "live_chat" \
        -o "%(title)s [%(id)s].%(ext)s" \
        "$URL"
    echo ""
    echo "=================================================="
    echo "✅ Live Chat downloaded! File saved in: $DEST_DIR"
    echo "=================================================="
    exit 0
fi

# Full Video + Chat Download
yt-dlp \
    --no-cache-dir \
    -P "home:$DEST_DIR" \
    -P "temp:$DEST_DIR" \
    --windows-filenames \
    -f "bestvideo+bestaudio/best" \
    --merge-output-format mkv \
    -N 4 \
    --continue \
    --write-subs \
    --sub-langs "live_chat" \
    -o "%(title)s [%(id)s].%(ext)s" \
    "$URL" &
YTDL_PID=$!

(
    shown=0
    while kill -0 "$YTDL_PID" 2>/dev/null; do
        ffmpeg_pid=$(pgrep -P "$YTDL_PID" ffmpeg 2>/dev/null || true)
        if [ -n "$ffmpeg_pid" ]; then
            printf "\r⚙️  [ffmpeg] Merging best video + audio streams...   "
            shown=1
        fi
        sleep 1.5
    done
    if [ "$shown" -eq 1 ]; then
        printf "\r✅ [ffmpeg] Merge complete!                                \n"
    fi
) &
MONITOR_PID=$!

wait $YTDL_PID
wait $MONITOR_PID 2>/dev/null || true

echo ""
echo "=================================================="
echo "✅ Download complete! All files saved in: $DEST_DIR"
echo "=================================================="
