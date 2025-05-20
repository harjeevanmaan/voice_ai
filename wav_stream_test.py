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

INPUT_ULAW = Path("sound_files/sample_8khz_mono_TRUE_RAW_clipped.ulaw")  # raw µ-law 8 kHz
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

async def wav_chunk_generator(path: Path):
    """
    Yield µ-law WAV header once, then 160-byte (20 ms) µ-law frames in real time.
    """
    yield mulaw_wav_header_unknown()

    with path.open("rb") as f:
        while (frame := f.read(160)):          # 160 B = 20 ms μ-law @8 kHz
            yield frame
            await asyncio.sleep(CHUNK_MS / 1000)

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

    form = aiohttp.FormData()
    form.add_field(
        "audio",
        AsyncIterablePayload(wav_chunk_generator(INPUT_ULAW)),
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

            # launch the response-processing coroutine in parallel
            recv_task = asyncio.create_task(_recv_and_save(r, OUT_PCM))

            # await completion of the response task before leaving the context
            await recv_task

if __name__ == "__main__":
    asyncio.run(run()) 