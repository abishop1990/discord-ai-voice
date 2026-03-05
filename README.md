# discord-ai-voice

A Discord voice bot that listens, thinks, and speaks — using local Whisper STT,
any LLM via LiteLLM, and local TTS via pyttsx3. Works on macOS and Windows with
no code changes.

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
        ▼ streaming API (LiteLLM)
   Claude / GPT-4o / Gemini / Ollama / ...  →  text tokens
        │
        ▼ sentence boundaries → pyttsx3 (thread executor)
   WAV temp file
        │
        ▼ FFmpegPCMAudio
   Discord voice channel (playback)
```

Typical latency: ~120ms STT + ~300ms LLM first token + ~150ms TTS = **~600ms**
from end of speech to start of response. TTS generation for sentence N+1 runs
concurrently with playback of sentence N.

## Prerequisites

**macOS:**
- Python 3.11+
- `brew install ffmpeg`
- A running [whisper.cpp server](#whisper-server-setup)

**Windows:**
- Python 3.11+
- `winget install ffmpeg`
- `pip install pywin32` (required by pyttsx3 for SAPI)
- A running [whisper.cpp server](#whisper-server-setup)

Both: a Discord bot with Voice Activity permissions (see [Discord Bot Setup](#discord-bot-setup)).

## Quick Start

```bash
# 1. Clone
git clone https://github.com/abishop1990/discord-ai-voice.git
cd discord-ai-voice

# 2. Create virtualenv
python3 -m venv venv
source venv/bin/activate      # macOS/Linux
# venv\Scripts\activate       # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env — at minimum set DISCORD_BOT_TOKEN, DISCORD_OWNER_ID, and your LLM API key

# 5. (Optional) Customize persona
cp persona.example.txt persona.txt
# Edit persona.txt

# 6. Run
python bot.py
```

## Whisper Server Setup

The bot expects a [whisper.cpp](https://github.com/ggerganov/whisper.cpp) HTTP
server running locally.

```bash
# Clone and build
git clone https://github.com/ggerganov/whisper.cpp.git
cd whisper.cpp
make

# Download a model (base.en is fast and accurate enough for voice chat)
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
| `LLM_MODEL` | No | `claude-haiku-4-5-20251001` | LiteLLM model string (see table below) |
| `WHISPER_URL` | No | `http://127.0.0.1:8765/inference` | whisper.cpp server endpoint |
| `TTS_VOICE` | No | `Samantha` | Partial voice name match (case-insensitive) |
| `SILENCE_SEC` | No | `0.7` | Seconds of silence before processing audio |
| `MIN_DURATION_SEC` | No | `0.4` | Minimum audio duration (filters noise) |
| `PERSONA_FILE` | No | `persona.txt` | Path to persona file (loaded if it exists) |
| `SYSTEM_PROMPT` | No | built-in default | Inline system prompt (used if no persona file) |

### LLM Providers

Switch providers by changing `LLM_MODEL` and setting the matching API key env var:

| Provider | `LLM_MODEL` example | API key env var |
|---|---|---|
| **Anthropic Claude** | `claude-haiku-4-5-20251001` | `ANTHROPIC_API_KEY` |
| **OpenAI** | `gpt-4o` | `OPENAI_API_KEY` |
| **Google Gemini** | `gemini/gemini-2.0-flash` | `GEMINI_API_KEY` |
| **OpenRouter** | `openrouter/anthropic/claude-haiku-4-5-20251001` | `OPENROUTER_API_KEY` |
| **Ollama (local)** | `ollama/llama3` | none — set `OLLAMA_API_BASE=http://localhost:11434` |

LiteLLM picks up the API key from the environment automatically based on the model prefix.

## TTS Voices

The bot uses pyttsx3 for local TTS — no API key or internet required.

**List available voices on your system:**
```bash
python -c "import pyttsx3; e=pyttsx3.init(); [print(v.name) for v in e.getProperty('voices')]"
```

Set `TTS_VOICE` to any partial name from that list (case-insensitive match):
- macOS: `Samantha`, `Alex`, `Moira`, `Daniel`, etc.
- Windows: `Microsoft Zira Desktop`, `Microsoft David Desktop`, etc.

> **Note:** pyttsx3 generates the full audio for each sentence before playback begins.
> This adds ~100–200ms of latency compared to a streaming approach, but works
> identically on all platforms with no external dependencies.

## Persona Customization

Create `persona.txt` (or copy `persona.example.txt`) to set the bot's personality.
The file contents become the system prompt sent to the LLM.

Keep it concise — a few sentences about tone, style, and response length is enough.

## Running as a macOS LaunchAgent

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

## License

MIT
