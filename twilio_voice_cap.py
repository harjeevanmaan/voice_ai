"""
Run:   python twilio_capture.py
Then:  ngrok http 8765   # copy the https URL, swap scheme to wss://, paste into the TwiML Bin above
Dial:  client.calls.create(url=<TwiML Bin>, ...)
Stop:  Ctrl-C, then play the file
       ffplay -f mulaw -ar 8000 -ac 1 twilio_ulaw_8k.pcm
"""
import asyncio, websockets, json, base64, logging, pathlib, datetime, aiohttp, struct, config as cfg
from aiohttp.payload import AsyncIterablePayload

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
INBOUND_QUEUE = asyncio.Queue(maxsize=200)          # back-pressure guard
OUT_PATH       = pathlib.Path("twilio_ulaw_8k.pcm")
SENTINEL = b""

# --- ElevenLabs settings (env-driven; fall back to hard-coded IDs) -----------
VOICE_ID = cfg.ELEVENLABS_VOICE_ID or "XXhALD8N7SAICWXSC1Km"
MODEL_ID = cfg.ELEVENLABS_MODEL_ID  or "eleven_multilingual_sts_v2"
API_KEY  = cfg.ELEVENLABS_API_KEY
OUT_PCM  = pathlib.Path("sound_files/el_output_wav_stream.pcm")

# ------------------------------------------------------------------ µ-law WAV header helper (copied from wav_stream_test)
def mulaw_wav_header_unknown() -> bytes:
    """
    44-byte WAV header for 8-kHz mono μ-law (fmt-tag 7).  Total length is
    left 0xFFFFFFFF so we can stream forever.
    """
    UNKNOWN = 0xFFFFFFFF
    byte_rate   = 8000 * 1 * 1      # sampleRate * blockAlign
    block_align = 1                 # mono * 1 byte/sample
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", UNKNOWN, b"WAVE",
        b"fmt ", 16, 7, 1, 8000, byte_rate, block_align, 8,
        b"data", UNKNOWN
    )

# ------------------------------------------------------------------ NEW: body-generator sent to ElevenLabs
async def _body_generator():
    """Async-iterable that yields one WAV header then live 160-B μ-law frames."""
    yield mulaw_wav_header_unknown()            # header first
    while True:
        frame = await INBOUND_QUEUE.get()
        if frame is SENTINEL:                   # poison pill → EOF
            break
        yield frame
        INBOUND_QUEUE.task_done()

# ------------------------------------------------------------------ NEW: save ElevenLabs streaming reply
async def _recv_and_save(resp: aiohttp.ClientResponse):
    OUT_PCM.parent.mkdir(parents=True, exist_ok=True)
    received = 0
    with OUT_PCM.open("wb") as out:
        async for chunk in resp.content.iter_chunked(4096):
            out.write(chunk)
            received += len(chunk)
    logging.info("✓ wrote %d B ElevenLabs output → %s", received, OUT_PCM)

# ------------------------------------------------------------------ NEW: full upload coroutine
async def _elevenlabs_upload():
    if not API_KEY:
        logging.error("ELEVENLABS_API_KEY not configured")
        return

    url     = f"https://api.elevenlabs.io/v1/speech-to-speech/{VOICE_ID}/stream"
    params  = {"output_format": "ulaw_8000", "optimize_streaming_latency": 0}
    headers = {"Xi-Api-Key": API_KEY}

    form = aiohttp.FormData()
    form.add_field(
        "audio",
        AsyncIterablePayload(_body_generator()),
        filename="audio.wav",
        content_type="audio/wav"                # still a WAV container
    )
    form.add_field("model_id", MODEL_ID)

    logging.info("POST %s …", url)
    async with aiohttp.ClientSession() as s:
        async with s.post(url, params=params, data=form, headers=headers) as r:
            logging.info("ElevenLabs status %s", r.status)
            if r.status != 200:
                logging.error(await r.text())
                return
            await _recv_and_save(r)             # stream → disk

# ------------------------------------------------------------------ WebSocket
async def media_handler(ws):
    async for msg in ws:                            # messages are JSON strings
        logging.info(msg)
        m = json.loads(msg)
        if m.get("event") == "media":               # Twilio 'media' frame
            frame = base64.b64decode(m["media"]["payload"])
            await INBOUND_QUEUE.put(frame)          # 160-byte chunk
        elif m.get("event") == "stop":
            await INBOUND_QUEUE.put(SENTINEL)      # poison pill
            break
    logging.info("Twilio stream ended")

# ------------------------------------------------------------------ Main
async def main():
    async with websockets.serve(media_handler, "", 8765):
        logging.info("🌐 WebSocket listening on ws://localhost:8765")
        await _elevenlabs_upload()              # blocks until Twilio sends stop

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("EL output file size: %d kB", OUT_PCM.stat().st_size // 1024)
