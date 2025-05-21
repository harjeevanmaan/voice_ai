"""
Minimal "fractional WAV" streaming test with ElevenLabs S2S.
Sends a 16-kHz mono PCM WAV header once, then 20-ms chunks (640 B) of audio.

Prereqs:
  pip install aiohttp
  Place a 16-kHz, 16-bit, mono WAV at sound_files/sample.wav
Play result:
  ffplay -f s16le -ar 16000 -ac 1 sound_files/el_output_wav_stream.pcm
"""
import asyncio, aiohttp, struct, os, logging, config as cfg
from pathlib import Path
from aiohttp.payload import AsyncIterablePayload
from typing import AsyncIterator

# INPUT_ULAW = Path("sound_files/sample_8khz_mono_TRUE_RAW_clipped.ulaw")  # raw µ-law 8 kHz
INPUT_ULAW = Path("twilio_ulaw_8k.pcm")  # raw µ-law 8 kHz
OUT_PCM    = Path("sound_files/el_output_wav_stream.pcm")
VOICE_ID   = cfg.ELEVENLABS_VOICE_ID or "XXhALD8N7SAICWXSC1Km"
MODEL_ID   = cfg.ELEVENLABS_MODEL_ID  or "eleven_multilingual_sts_v2"
API_KEY    = cfg.ELEVENLABS_API_KEY
CHUNK_MS   = 20                                             # real-time pacing
LOG_FMT    = "%(asctime)s | %(levelname)s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FMT)
log = logging.getLogger("wav_stream_test")

def mulaw_wav_header_unknown() -> bytes:
    """
    Build a 44-byte WAV header for:
        • μ-law (format-tag 7)
        • 8 kHz, mono, 8-bit
    """
    UNKNOWN = 0xFFFFFFFF
    byte_rate   = 8000 * 1 * 1          # sampleRate * blockAlign
    block_align = 1                     # mono * 1 byte/sample
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        UNKNOWN,       # ChunkSize (unknown length)
        b"WAVE",
        b"fmt ",            # fmt chunk
        16,                 # Subchunk1Size (PCM)
        7,                  # AudioFormat 7 = μ-law
        1,                  # NumChannels
        8000,               # SampleRate
        byte_rate,          # ByteRate
        block_align,        # BlockAlign
        8,                  # BitsPerSample (for μ-law always 8)
        b"data",
        UNKNOWN             # Subchunk2Size (unknown length)
    )

# ---------------------------------------------------------------------------#
#  Queue-based producer/consumer pair (upload side)
# ---------------------------------------------------------------------------#
async def _producer_read_file(path: Path, q: asyncio.Queue[bytes]) -> None:
    """
    Read the raw µ-law file and push:
        • one WAV header   (sentinel length = unknown)
        • 160-byte frames every 20 ms
    A final `None` sentinel marks EOF.
    """
    await q.put(mulaw_wav_header_unknown())

    with path.open("rb") as f:
        while (frame := f.read(160)):
            await q.put(frame)
            await asyncio.sleep(CHUNK_MS / 1000)

    await q.put(None)                      # tell consumer we're done


async def _queue_iterator(q: asyncio.Queue[bytes]) -> AsyncIterator[bytes]:
    """
    Async-generator consumed by `AsyncIterablePayload`.
    """
    while True:
        chunk = await q.get()
        if chunk is None:                  # EOF sentinel
            break
        yield chunk
        q.task_done()

# ---------------------------------------------------------------------------#
#  NEW: standalone coroutine that writes ElevenLabs' streamed response to disk
# ---------------------------------------------------------------------------#
async def _recv_and_save(resp: aiohttp.ClientResponse, out_path: Path) -> None:
    """
    Read chunks from `resp.content` (ulaw_8000) and append them to `out_path`.
    Runs independently of the upload coroutine.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    received = 0
    with out_path.open("wb") as out:
        async for chunk in resp.content.iter_chunked(4096):
            out.write(chunk)
            received += len(chunk)
    log.info("✓ wrote %d B ElevenLabs output → %s", received, out_path)

# ------------ main -----------------------------------------------------------
async def run():
    if not API_KEY:
        raise SystemExit("ELEVENLABS_API_KEY not configured")

    url = f"https://api.elevenlabs.io/v1/speech-to-speech/{VOICE_ID}/stream"
    params  = {"output_format": "ulaw_8000", "optimize_streaming_latency": 0}
    headers = {"Xi-Api-Key": API_KEY}

    # ---------------- upload queue & tasks ----------------
    upload_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
    producer_task = asyncio.create_task(_producer_read_file(INPUT_ULAW, upload_q))

    form = aiohttp.FormData()
    form.add_field(
        "audio",
        AsyncIterablePayload(_queue_iterator(upload_q)),
        filename="audio.wav",
        content_type="audio/wav"          # still a WAV container
    )
    # For WAV you normally *omit* file_format; leave only model_id
    form.add_field("model_id", MODEL_ID)

    log.info("POST %s ...", url)
    async with aiohttp.ClientSession() as s:
        async with s.post(url, params=params, data=form, headers=headers) as r:
            log.info("status %s", r.status)
            if r.status != 200:
                log.error(await r.text())
                return

            # launch download-side coroutine
            recv_task = asyncio.create_task(_recv_and_save(r, OUT_PCM))

            # wait for both producer (upload) and downloader to finish
            await asyncio.gather(producer_task, recv_task)

if __name__ == "__main__":
    print(f"Voice ID: {VOICE_ID}")
    asyncio.run(run()) 