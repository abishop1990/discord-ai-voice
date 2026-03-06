# discord-ai-voice

A Discord voice bot that listens, thinks, and speaks — using local Whisper STT,
any LLM via LiteLLM, and local neural TTS via [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx).
Works on macOS, Linux, and Windows with no code changes.

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
        ▼ addressing gate (BOT_NAME + conversation window)
        │
        ▼ streaming API (LiteLLM)
   Claude / GPT-4o / Gemini / Ollama / ...  →  text tokens
        │
        ▼ sentence boundaries → kokoro-onnx (thread executor)
   WAV temp file
        │
        ▼ FFmpegPCMAudio
   Discord voice channel (playback)
```

Typical latency: ~120ms STT + ~300ms LLM first token + ~80ms TTS = **~500ms**
from end of speech to start of response. TTS generation for sentence N+1 runs
concurrently with playback of sentence N.

## Prerequisites

**All platforms:**
- Python 3.11+
- ffmpeg on PATH (`brew install ffmpeg` / `winget install ffmpeg` / `apt install ffmpeg`)
- espeak-ng (`brew install espeak-ng` / `apt install espeak-ng` / [Windows installer](https://github.com/espeak-ng/espeak-ng/releases))
- A running [whisper.cpp server](#whisper-server-setup)
- [Kokoro model files](#tts-kokoro-setup)

**Windows only:** `pip install pywin32`

Also: a Discord bot with Voice Activity permissions (see [Discord Bot Setup](#discord-bot-setup)).

## Quick Start

```bash
# 1. Clone
git clone https://github.com/your-username/discord-ai-voice.git
cd discord-ai-voice

# 2. Create virtualenv
python3 -m venv venv
source venv/bin/activate      # macOS/Linux
# venv\Scripts\activate       # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env — at minimum set DISCORD_BOT_TOKEN, DISCORD_OWNER_ID, LLM API key,
# and BOT_NAME (the name you'll say to address the bot)

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

## TTS (Kokoro) Setup

Kokoro is a local neural TTS model — no API key, runs fully on-device.

```bash
# Download model files (~300MB total)
mkdir -p ~/.cache/kokoro-onnx
curl -L -o ~/.cache/kokoro-onnx/kokoro-v1.0.int8.onnx \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v1.0.int8.onnx
curl -L -o ~/.cache/kokoro-onnx/voices-v1.0.bin \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices-v1.0.bin
```

Set `TTS_VOICE` to any of the built-in voices:

| Language | Voices |
|---|---|
| American English | `af_heart` (default), `af_bella`, `af_sarah`, `af_nicole`, `am_michael`, `am_adam`, `am_echo` |
| British English | `bf_emma`, `bf_isabella`, `bm_george`, `bm_lewis` |

## Discord Bot Setup

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Create a new application → Bot → copy the token
3. Enable **Server Members Intent** and **Voice States** under Privileged Gateway Intents
4. Invite the bot with `bot` scope + `Connect`, `Speak`, `Use Voice Activity` permissions

## Addressing the Bot

By default the bot only responds when you say its name in a voice utterance — it
won't respond to things you say that aren't directed at it.

- **Say the bot's name** (e.g. *"Hey Aria, what should I do about X?"*) → bot responds
  and opens a 30-second **conversation window**
- **Within the window** → follow-up utterances work without repeating the name
- **Outside the window + no name** → bot stays silent
- **Window resets** on each channel join (fresh session, no carry-over)

Set `BOT_NAME` in `.env` to whatever you call the bot in your Discord server.
Set `CONVERSATION_WINDOW` to tune how long the follow-up window stays open.

## Environment Variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | Yes | — | Bot token from Discord Developer Portal |
| `DISCORD_OWNER_ID` | Yes | — | Your Discord user ID (right-click → Copy User ID) |
| `LLM_MODEL` | No | `claude-haiku-4-5-20251001` | LiteLLM model string (see table below) |
| `BOT_NAME` | No | `bot` | Name to listen for in transcripts |
| `CONVERSATION_WINDOW` | No | `30` | Seconds of follow-up window after being named |
| `WHISPER_URL` | No | `http://127.0.0.1:8765/inference` | whisper.cpp server endpoint |
| `TTS_VOICE` | No | `af_heart` | Kokoro voice name |
| `TTS_SPEED` | No | `1.0` | TTS speech speed (0.5–2.0) |
| `KOKORO_MODEL` | No | `~/.cache/kokoro-onnx/kokoro-v1.0.int8.onnx` | Path to Kokoro ONNX model |
| `KOKORO_VOICES` | No | `~/.cache/kokoro-onnx/voices-v1.0.bin` | Path to Kokoro voices file |
| `ESPEAK_LIB` | No | — | Path to libespeak-ng if not on system PATH |
| `SILENCE_SEC` | No | `0.7` | Seconds of silence before processing audio |
| `MIN_DURATION_SEC` | No | `0.4` | Minimum audio duration (filters noise/clicks) |
| `STATUS_PORT` | No | `18795` | Port for `GET /status` health-check endpoint |
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
    <key>BOT_NAME</key>
    <string>YourBotName</string>
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
