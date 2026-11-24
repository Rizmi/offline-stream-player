# Offline Stream Player

A lightweight web player and downloader utility for watching archived livestreams with synchronized chat replays completely offline.

## Highlights

- **Synchronized Chat Replay**: Aligns chat messages with video playback time and updates dynamically on seek.
- **Multi-Platform Support**: Includes scripts to download video and chat from YouTube and Kick.
- **Chat-Only Mode**: Fetch only chat replays (`--chat-only`) when the video file is already downloaded.
- **Edge-to-Edge Layout**: Theater-style view with video on the left and a resizable chat panel on the right.
- **Local Range Server**: Built-in HTTP server supporting byte-range requests (`206 Partial Content`) for fast seeking.

## Prerequisites

- **Python 3.9+**
- **ffmpeg** (required for video container formatting)
- **yt-dlp** (specified in `requirements.txt`)

Install python dependencies:
```bash
pip install -r requirements.txt
```

## Usage

### 1. Downloading Streams & Chat

To download both video and chat:
```bash
./download_yt.sh "<YOUTUBE_URL>" [DESTINATION_DIR]
./download_kick.sh "<KICK_URL>" [DESTINATION_DIR]
```

To download only the chat replay without the video:
```bash
./download_yt.sh "<YOUTUBE_URL>" --chat-only
./download_kick.sh "<KICK_URL>" --chat-only
```

### 2. Running the Player

Start the local server:
```bash
./start.sh
```

Then open `http://localhost:8765` in your browser:
1. Click **Choose Video** to select your `.mp4` or `.mkv` file.
2. Click **Choose Chat** to select the corresponding `.chat.json` or `.live_chat.json` file.
*(You can also drag and drop both files directly into the window).*

## Keyboard Shortcuts

- `Space` / `k`: Play or Pause
- `Left` / `Right`: Skip 5 seconds backward / forward
- `j` / `l`: Skip 10 seconds backward / forward
- `m`: Mute or Unmute audio
- `f`: Toggle Fullscreen
- `c`: Show or Hide chat panel

## License

This project is licensed under the MIT License.
