#!/usr/bin/env python3
"""
Discord AI Voice Bot
Joins voice → captures speech → Whisper STT → LLM (streaming) → TTS

Supports macOS and Windows. TTS uses pyttsx3 (local, no API key required):
  - macOS: NSSpeechSynthesizer (same voices as `say`)
  - Windows: SAPI5 voices
  - Linux: espeak

LLM uses LiteLLM — swap providers by changing LLM_MODEL in .env:
  - Claude:      claude-haiku-4-5-20251001  (set ANTHROPIC_API_KEY)
  - OpenAI:      gpt-4o                     (set OPENAI_API_KEY)
  - Gemini:      gemini/gemini-2.0-flash    (set GEMINI_API_KEY)
  - Ollama:      ollama/llama3              (set OLLAMA_API_BASE)
  - OpenRouter:  openrouter/...             (set OPENROUTER_API_KEY)

Discord E2EE (DAVE): handled transparently via the davey library.
"""

import asyncio
import logging
import os
import re
import signal
import sys
import tempfile
import threading
import time

import aiohttp
import davey
import discord
import litellm
import pyttsx3
from discord.ext import voice_recv
from discord.opus import Decoder as OpusDecoder
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("discord-ai-voice")
litellm.suppress_debug_info = True

# ── Config ────────────────────────────────────────────────────────────────────


def _require(key: str) -> str:
    v = os.environ.get(key)
    if not v:
        sys.exit(f"Error: {key} environment variable is required")
    return v


BOT_TOKEN        = _require("DISCORD_BOT_TOKEN")
OWNER_ID         = int(_require("DISCORD_OWNER_ID"))
# LLM API key is NOT required here — LiteLLM reads provider-specific env vars
# automatically (ANTHROPIC_API_KEY, OPENAI_API_KEY, etc.) at call time.
WHISPER_URL      = os.environ.get("WHISPER_URL", "http://127.0.0.1:8765/inference")
MODEL            = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")
TTS_VOICE        = os.environ.get("TTS_VOICE", "Samantha")
SILENCE_SEC      = float(os.environ.get("SILENCE_SEC", "0.7"))
MIN_DURATION_SEC = float(os.environ.get("MIN_DURATION_SEC", "0.4"))
MAX_HISTORY      = 10

_SENTENCE_END = re.compile(r'(?<=[.!?])\s+|(?<=[.!?])$')

DEFAULT_SYSTEM_PROMPT = """\
You are a helpful AI assistant in a Discord voice channel.

Keep responses SHORT: 1-3 sentences max. Speak naturally and conversationally. \
No markdown, no bullet points — just talk.\
"""

_persona_file = os.environ.get("PERSONA_FILE", "persona.txt")
if os.path.exists(_persona_file):
    SYSTEM_PROMPT = open(_persona_file).read().strip()
    log.info("Loaded persona from %s", _persona_file)
else:
    SYSTEM_PROMPT = os.environ.get("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT)

# ── State ─────────────────────────────────────────────────────────────────────

conversation_history: list = []
_speaking = threading.Event()          # set while bot is playing TTS
_tts_engine: pyttsx3.Engine | None = None
_tts_lock = threading.Lock()           # pyttsx3 is not thread-safe
_http_session: aiohttp.ClientSession | None = None   # persistent session

# ── Discord setup ─────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.voice_states = True
bot = discord.Client(intents=intents)


# ── TTS engine ────────────────────────────────────────────────────────────────

def _init_tts_engine() -> pyttsx3.Engine:
    engine = pyttsx3.init()
    voices = engine.getProperty("voices")
    for v in voices:
        name = (v.name or "").lower()
        vid = (v.id or "").lower()
        if TTS_VOICE.lower() in name or TTS_VOICE.lower() in vid:
            engine.setProperty("voice", v.id)
            log.info("TTS voice selected: %s", v.name)
            break
    else:
        log.warning("TTS voice %r not found; using system default", TTS_VOICE)
    return engine


async def _tts_start(text: str) -> str | None:
    """
    Generate TTS audio to a temp WAV file via pyttsx3.
    Runs in a thread executor so the event loop stays unblocked.
    Returns the temp file path when audio is ready, or None on error.
    """
    loop = asyncio.get_event_loop()
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".wav", prefix="discord_voice_tts_")
    os.close(tmp_fd)

    def _generate() -> None:
        with _tts_lock:
            _tts_engine.save_to_file(text, tmp_path)
            _tts_engine.runAndWait()

    try:
        await loop.run_in_executor(None, _generate)
        return tmp_path
    except Exception as e:
        log.error("TTS generation failed: %s", e)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None


# ── Audio sink ────────────────────────────────────────────────────────────────

class VoiceSink(voice_recv.AudioSink):
    def __init__(self, vc: voice_recv.VoiceRecvClient):
        super().__init__()
        self._vc = vc
        self._opus_decoder = OpusDecoder()
        self._pcm_chunks: list[bytes] = []
        self._last_packet_time: float = 0.0
        self._processing = False
        self._lock = threading.Lock()

        self._checker = threading.Thread(
            target=self._silence_loop, daemon=True, name="silence-checker"
        )
        self._checker.start()

    def wants_opus(self) -> bool:
        return True  # we handle DAVE decrypt + Opus decode ourselves

    def write(self, user: discord.Member | None, data: voice_recv.VoiceData) -> None:
        if _speaking.is_set():
            return
        if user is None or user.id != OWNER_ID:
            return

        raw = data.opus
        if not raw:
            return

        # Handle Discord E2EE (DAVE protocol) transparently
        conn = getattr(self._vc, "_connection", None)
        dave_session = getattr(conn, "dave_session", None)
        if dave_session and dave_session.ready:
            try:
                raw = dave_session.decrypt(user.id, davey.MediaType.audio, raw)
            except Exception as e:
                log.debug("DAVE decrypt failed: %s", e)
                return

        try:
            pcm = self._opus_decoder.decode(raw, fec=False)
        except Exception as e:
            log.debug("Opus decode failed: %s", e)
            return

        with self._lock:
            self._pcm_chunks.append(pcm)
            self._last_packet_time = time.monotonic()

    def _silence_loop(self) -> None:
        while True:
            time.sleep(0.05)
            with self._lock:
                if not self._pcm_chunks or self._processing:
                    continue
                if time.monotonic() - self._last_packet_time < SILENCE_SEC:
                    continue
                chunks = self._pcm_chunks[:]
                self._pcm_chunks.clear()
                self._processing = True

            asyncio.run_coroutine_threadsafe(self._process(chunks), bot.loop)

    async def _process(self, chunks: list[bytes]) -> None:
        try:
            raw_pcm = b"".join(chunks)
            duration = len(raw_pcm) / (48000 * 2 * 2)

            if duration < MIN_DURATION_SEC:
                log.debug("Audio too short (%.2fs), skipping", duration)
                return

            t0 = time.monotonic()
            transcript = await transcribe(raw_pcm)
            stt_ms = (time.monotonic() - t0) * 1000
            if not transcript:
                log.info("Empty transcript, skipping")
                return

            log.info("[STT %.0fms] Heard: %r", stt_ms, transcript)
            await respond(transcript, self._vc)
        except Exception:
            log.exception("Error in _process")
        finally:
            with self._lock:
                self._processing = False

    def cleanup(self) -> None:
        pass


# ── Transcription ─────────────────────────────────────────────────────────────

async def transcribe(raw_pcm: bytes) -> str | None:
    """
    Convert raw 48kHz stereo PCM → 16kHz mono WAV via ffmpeg pipe (no temp
    file), then POST WAV bytes to whisper-server via the persistent aiohttp session.
    """
    global _http_session

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y",
        "-f", "s16le", "-ar", "48000", "-ac", "2", "-i", "pipe:0",
        "-ar", "16000", "-ac", "1", "-f", "wav", "pipe:1",
        "-loglevel", "error",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    wav_bytes, _ = await proc.communicate(input=raw_pcm)

    if proc.returncode != 0 or not wav_bytes:
        log.error("ffmpeg failed: %d", proc.returncode)
        return None

    form = aiohttp.FormData()
    form.add_field("file", wav_bytes, filename="audio.wav", content_type="audio/wav")
    form.add_field("response_format", "json")

    try:
        async with _http_session.post(  # type: ignore[union-attr]
            WHISPER_URL, data=form, timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            if resp.status != 200:
                log.error("Whisper HTTP %d", resp.status)
                return None
            result = await resp.json()
            text = result.get("text", "").strip()
            return text if text else None
    except Exception as e:
        log.error("Whisper request failed: %s", e)
        return None


# ── LLM + streaming TTS pipeline ─────────────────────────────────────────────

async def respond(transcript: str, vc: voice_recv.VoiceRecvClient) -> None:
    """
    Stream LLM response via LiteLLM. For each sentence boundary, generate TTS
    audio to a temp file and queue it for playback. TTS generation runs in a
    thread executor so playback of the previous sentence continues concurrently.
    """
    global conversation_history

    conversation_history.append({"role": "user", "content": transcript})
    if len(conversation_history) > MAX_HISTORY * 2:
        conversation_history = conversation_history[-(MAX_HISTORY * 2):]

    tts_queue: asyncio.Queue[str | None] = asyncio.Queue()
    _speaking.set()

    player_task = asyncio.create_task(_play_file_queue(vc, tts_queue))

    full_reply = ""
    buffer = ""
    t_first_token: float | None = None
    try:
        llm_messages = [{"role": "system", "content": SYSTEM_PROMPT}] + conversation_history
        response = await litellm.acompletion(
            model=MODEL,
            messages=llm_messages,
            max_tokens=300,
            stream=True,
        )
        async for chunk in response:
            if t_first_token is None:
                t_first_token = time.monotonic()
            token = (chunk.choices[0].delta.content or "") if chunk.choices else ""
            buffer += token
            full_reply += token

            while True:
                m = _SENTENCE_END.search(buffer)
                if not m:
                    break
                sentence = buffer[: m.start() + 1].strip()
                buffer = buffer[m.end():]
                if len(sentence) > 2:
                    file_path = await _tts_start(sentence)
                    if file_path:
                        await tts_queue.put(file_path)

        tail = buffer.strip()
        if len(tail) > 2:
            file_path = await _tts_start(tail)
            if file_path:
                await tts_queue.put(file_path)

        await tts_queue.put(None)
        await player_task

        conversation_history.append({"role": "assistant", "content": full_reply.strip()})
        log.info("Reply: %r", full_reply.strip())

    except Exception:
        log.exception("respond() error")
        await tts_queue.put(None)
        player_task.cancel()
    finally:
        _speaking.clear()


async def _play_file_queue(
    vc: voice_recv.VoiceRecvClient,
    q: asyncio.Queue[str | None],
) -> None:
    """Play TTS audio files sequentially from the queue."""
    while True:
        item = await q.get()
        if item is None:
            break
        file_path = item
        try:
            if not vc.is_connected():
                try:
                    os.unlink(file_path)
                except OSError:
                    pass
                continue

            if vc.is_playing():
                vc.stop()

            done = asyncio.Event()

            def _after(err, path=file_path):
                if err:
                    log.error("Playback error: %s", err)
                try:
                    os.unlink(path)
                except OSError:
                    pass
                bot.loop.call_soon_threadsafe(done.set)

            vc.play(discord.FFmpegPCMAudio(file_path), after=_after)
            await done.wait()
        except Exception:
            log.exception("_play_file_queue error")
            try:
                os.unlink(file_path)
            except OSError:
                pass


# ── Shutdown ──────────────────────────────────────────────────────────────────

async def shutdown() -> None:
    log.info("Shutting down...")
    for vc in bot.voice_clients:
        try:
            await vc.disconnect()
        except Exception:
            pass
    if _http_session:
        await _http_session.close()
    await bot.close()


# ── Bot events ────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    global _http_session, _tts_engine
    _http_session = aiohttp.ClientSession()
    _tts_engine = _init_tts_engine()
    log.info("Logged in as %s (id=%d)", bot.user, bot.user.id)

    # SIGTERM handler — Unix only (Windows doesn't support add_signal_handler)
    if sys.platform != "win32":
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(shutdown()))


@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState,
) -> None:
    if member.id != OWNER_ID:
        return

    guild = member.guild
    vc: voice_recv.VoiceRecvClient | None = discord.utils.get(
        bot.voice_clients, guild=guild
    )  # type: ignore

    if after.channel and (not before.channel or before.channel != after.channel):
        log.info("Owner joined %s", after.channel.name)
        await join_channel(after.channel, vc)
    elif not after.channel and before.channel:
        log.info("Owner left voice")
        if vc:
            await vc.disconnect()


async def join_channel(
    channel: discord.VoiceChannel,
    existing_vc: voice_recv.VoiceRecvClient | None,
) -> None:
    global conversation_history
    conversation_history = []
    _speaking.clear()

    if existing_vc:
        if existing_vc.channel == channel:
            return
        await existing_vc.disconnect()

    log.info("Joining %s...", channel.name)
    vc: voice_recv.VoiceRecvClient = await channel.connect(cls=voice_recv.VoiceRecvClient)  # type: ignore

    sink = VoiceSink(vc)
    vc.listen(sink)

    await asyncio.sleep(0.5)

    # Greeting
    greeting_path = await _tts_start("Hey, I'm here.")
    if greeting_path:
        done = asyncio.Event()

        def _after(err, path=greeting_path):
            try:
                os.unlink(path)
            except OSError:
                pass
            bot.loop.call_soon_threadsafe(done.set)

        vc.play(discord.FFmpegPCMAudio(greeting_path), after=_after)
        await done.wait()


# ── Entry ─────────────────────────────────────────────────────────────────────

log.info("Starting Discord AI voice bot...")
bot.run(BOT_TOKEN)
