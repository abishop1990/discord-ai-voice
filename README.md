# discord-ai-voice

A Discord voice bot that listens, thinks, and speaks — using local Whisper STT,
Claude LLM, and macOS `say` TTS. Sub-second response latency via streaming and
FIFO-pipe audio.

Built to work with Discord's E2EE (DAVE protocol) introduced in March 2026, which
broke most existing voice bots that relied on pre-DAVE decryption paths.

## Architecture

```
Discord voice channel
        │
        ▼ (Opus packets, DAVE E2EE decrypted)
   VoiceSink (48kHz stereo PCM, buffered until silence)
        │
        ▼ ffmpeg pipe (in-memory, no temp file)
   16kHz mono WAV
        │
        ▼ HTTP POST
   whisper.cpp server  →  transcript text
        │
        ▼ streaming API
   Claude (Anthropic)  →  text tokens
        │
        ▼ sentence boundaries
   macOS say → AIFF via named FIFO
        │
        ▼ FFmpegPCMAudio
   Discord voice channel (playback)
```

Latency budget (typical): ~120ms STT + ~300ms LLM first token + ~50ms TTS = **~500ms**
from end of speech to start of response.

## Prerequisites

- **macOS** (uses `say` for TTS and named FIFOs)
- Python 3.11+
- `ffmpeg` (`brew install ffmpeg`)
- A running [whisper.cpp server](#whisper-server-setup)
- A Discord bot with Voice Activity permissions

## Quick Start

```bash
# 1. Clone
git clone https://github.com/abishop1990/discord-ai-voice.git
cd discord-ai-voice

# 2. Create virtualenv
python3 -m venv venv
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env with your credentials (see Environment Variables below)

# 5. (Optional) Customize persona
cp persona.example.txt persona.txt
# Edit persona.txt to set the bot's personality and style

# 6. Run
python bot.py
```

## Whisper Server Setup

The bot expects a [whisper.cpp](https://github.com/ggerganov/whisper.cpp) HTTP
server running locally.

```bash
# Clone and build whisper.cpp
git clone https://github.com/ggerganov/whisper.cpp.git
cd whisper.cpp
make

# Download a model (ggml-base.en is fast and accurate enough for voice chat)
bash ./models/download-ggml-model.sh base.en

# Start the server (default port 8765)
./server -m models/ggml-base.en.bin --port 8765
```

## Discord Bot Setup

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Create a new application → Bot → copy the token
3. Enable **Server Members Intent** and **Voice States** under Privileged Gateway Intents
4. Invite the bot with `bot` scope + `Connect`, `Speak`, `Use Voice Activity` permissions

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | — | Bot token from Discord Developer Portal |
| `DISCORD_OWNER_ID` | Yes | — | Your Discord user ID (right-click → Copy User ID) |
| `ANTHROPIC_API_KEY` | Yes | — | API key from console.anthropic.com |
| `WHISPER_URL` | No | `http://127.0.0.1:8765/inference` | whisper.cpp server endpoint |
| `LLM_MODEL` | No | `claude-haiku-4-5-20251001` | Anthropic model ID |
| `TTS_VOICE` | No | `Samantha` | macOS voice name (`say -v ?` to list) |
| `SILENCE_SEC` | No | `0.7` | Seconds of silence before processing audio |
| `MIN_DURATION_SEC` | No | `0.4` | Minimum audio duration to process (filters noise) |
| `PERSONA_FILE` | No | `persona.txt` | Path to persona file (loaded if it exists) |
| `SYSTEM_PROMPT` | No | built-in default | Inline system prompt (used if no persona file) |

The bot only listens to and responds to the owner (`DISCORD_OWNER_ID`). It joins
and leaves voice channels automatically as the owner does.

## Persona Customization

Create `persona.txt` (or copy `persona.example.txt`) to customize the bot's
personality. The file contents become the system prompt sent to Claude.

Keep it concise — Claude works better with focused instructions than long essays.
A few sentences about tone, style, and response length is usually enough.

## Running as a macOS LaunchAgent

To start the bot automatically at login, create a LaunchAgent plist. Replace
the placeholder paths with your actual paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.yourname.discord-ai-voice</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/venv/bin/python</string>
    <string>/path/to/discord-ai-voice/bot.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/path/to/discord-ai-voice</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>DISCORD_BOT_TOKEN</key>
    <string>your_bot_token_here</string>
    <key>DISCORD_OWNER_ID</key>
    <string>your_discord_user_id</string>
    <key>ANTHROPIC_API_KEY</key>
    <string>your_anthropic_api_key</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/discord-ai-voice.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/discord-ai-voice.log</string>
</dict>
</plist>
```

Save as `~/Library/LaunchAgents/com.yourname.discord-ai-voice.plist`, then:

```bash
launchctl load ~/Library/LaunchAgents/com.yourname.discord-ai-voice.plist
```

> **Note:** If using a LaunchAgent, you can put credentials in the plist's
> `EnvironmentVariables` dict instead of a `.env` file. Either approach works —
> never commit either one to git.

## License

MIT
