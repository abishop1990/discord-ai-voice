#!/usr/bin/env python3
"""
Discord AI Voice Bot
Joins voice → captures speech → whisper STT → Claude (streaming) → macOS TTS

Latency optimizations:
  1. SILENCE_SEC=0.7: trigger processing quickly after user stops talking
  2. ffmpeg pipes WAV to whisper in-memory (no temp file)
  3. Persistent aiohttp session (no per-request TCP handshake)
  4. Anthropic streaming: TTS starts on first sentence boundary (~100ms)
  5. FIFO-pipe TTS: say and Discord ffmpeg run concurrently, playback starts
     ~50ms after say starts (not after it finishes)

Discord E2EE (DAVE): handled transparently via the davey library.
"""

import asyncio
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time

import aiohttp
import davey
import discord
from discord.ext import voice_recv
from discord.opus import Decoder as OpusDecoder
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("discord-ai-voice")

# ── Config ────────────────────────────────────────────────────────────────────


def _require(key: str) -> str:
    v = os.environ.get(key)
    if not v:
        sys.exit(f"Error: {key} environment variable is required")
    return v


BOT_TOKEN        = _require("DISCORD_BOT_TOKEN")
OWNER_ID         = int(_require("DISCORD_OWNER_ID"))
ANTHROPIC_KEY    = _require("ANTHROPIC_API_KEY")
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
_speaking = threading.Event()   # set while bot is playing TTS
async_anthropic = AsyncAnthropic(api_key=ANTHROPIC_KEY)
_http_session: aiohttp.ClientSession | None = None   # persistent session
_fifo_counter = 0                                     # unique FIFO names

# ── Discord setup ─────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.voice_states = True
bot = discord.Client(intents=intents)


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
    file), then POST WAV bytes directly to whisper-server via the persistent
    aiohttp session.
    """
    global _http_session

    # ffmpeg: PCM stdin → WAV stdout (no disk I/O)
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
    Stream Anthropic response. For each sentence, open a named FIFO, start
    `say` writing to it, then hand the FIFO to discord's FFmpegPCMAudio.
    `say` and ffmpeg run concurrently → playback starts ~50ms after say starts
    rather than ~200ms after it finishes.
    """
    global conversation_history

    conversation_history.append({"role": "user", "content": transcript})
    if len(conversation_history) > MAX_HISTORY * 2:
        conversation_history = conversation_history[-(MAX_HISTORY * 2):]

    # fifo_queue: (fifo_path, say_proc) tuples, or None = end
    fifo_queue: asyncio.Queue[tuple[str, subprocess.Popen] | None] = asyncio.Queue()
    _speaking.set()

    player_task = asyncio.create_task(_play_fifo_queue(vc, fifo_queue))

    full_reply = ""
    buffer = ""
    t_first_token: float | None = None
    try:
        async with async_anthropic.messages.stream(
            model=MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=conversation_history,
        ) as stream:
            async for token in stream.text_stream:
                if t_first_token is None:
                    t_first_token = time.monotonic()
                buffer += token
                full_reply += token

                while True:
                    m = _SENTENCE_END.search(buffer)
                    if not m:
                        break
                    sentence = buffer[: m.start() + 1].strip()
                    buffer = buffer[m.end():]
                    if len(sentence) > 2:
                        item = await _tts_start(sentence)
                        if item:
                            await fifo_queue.put(item)

        tail = buffer.strip()
        if len(tail) > 2:
            item = await _tts_start(tail)
            if item:
                await fifo_queue.put(item)

        await fifo_queue.put(None)
        await player_task

        conversation_history.append({"role": "assistant", "content": full_reply.strip()})
        log.info("Reply: %r", full_reply.strip())

    except Exception:
        log.exception("respond() error")
        await fifo_queue.put(None)
        player_task.cancel()
    finally:
        _speaking.clear()


async def _tts_start(text: str) -> tuple[str, subprocess.Popen] | None:
    """
    Create a named FIFO and start `say` writing AIFF to it.
    Returns (fifo_path, say_proc) immediately — say runs in background.
    The caller passes fifo_path to FFmpegPCMAudio; when ffmpeg opens the FIFO
    for reading, say unblocks and streams audio concurrently.
    """
    global _fifo_counter
    _fifo_counter += 1
    fifo_path = f"/tmp/discord_voice_tts_{os.getpid()}_{_fifo_counter}.fifo"

    try:
        os.unlink(fifo_path)
    except OSError:
        pass
    os.mkfifo(fifo_path)

    # Start say immediately — it will block at open(fifo_path, O_WRONLY)
    # until FFmpegPCMAudio's ffmpeg subprocess opens the read end.
    say_proc = subprocess.Popen(
        ["say", "-v", TTS_VOICE, "--", text, "-o", fifo_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return (fifo_path, say_proc)


async def _play_fifo_queue(
    vc: voice_recv.VoiceRecvClient,
    q: asyncio.Queue[tuple[str, subprocess.Popen] | None],
) -> None:
    """Play FIFO-backed TTS items sequentially."""
    while True:
        item = await q.get()
        if item is None:
            break
        fifo_path, say_proc = item
        try:
            if not vc.is_connected():
                say_proc.kill()
                try:
                    os.unlink(fifo_path)
                except OSError:
                    pass
                continue

            if vc.is_playing():
                vc.stop()

            done = asyncio.Event()

            def _after(err, path=fifo_path, proc=say_proc):
                if err:
                    log.error("Playback error: %s", err)
                proc.wait()   # reap the say process
                try:
                    os.unlink(path)
                except OSError:
                    pass
                bot.loop.call_soon_threadsafe(done.set)

            # Opening the FIFO for reading unblocks say, which then streams
            # AIFF data through the FIFO into ffmpeg into Discord — all live.
            source = discord.FFmpegPCMAudio(fifo_path, before_options="-f aiff")
            vc.play(source, after=_after)
            await done.wait()
        except Exception:
            log.exception("_play_fifo_queue error")
            say_proc.kill()
            try:
                os.unlink(fifo_path)
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
    global _http_session
    _http_session = aiohttp.ClientSession()
    log.info("Logged in as %s (id=%d)", bot.user, bot.user.id)

    # Register SIGTERM handler inside the event loop
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

    # Use member.guild directly — works across guilds, no GUILD_ID needed
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

    # Greeting via FIFO pipe (same low-latency path)
    item = await _tts_start("Hey, I'm here.")
    if item:
        fifo_path, say_proc = item
        done = asyncio.Event()

        def _after(err, path=fifo_path, proc=say_proc):
            proc.wait()
            try:
                os.unlink(path)
            except OSError:
                pass
            bot.loop.call_soon_threadsafe(done.set)

        vc.play(discord.FFmpegPCMAudio(fifo_path, before_options="-f aiff"), after=_after)
        await done.wait()


# ── Entry ─────────────────────────────────────────────────────────────────────

log.info("Starting Discord AI voice bot...")
bot.run(BOT_TOKEN)
